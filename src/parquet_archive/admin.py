import json
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from itertools import groupby
from operator import attrgetter

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin import FieldListFilter
from django.contrib.admin.options import IncorrectLookupParameters
from django.contrib.admin.utils import display_for_field, quote, unquote
from django.contrib.admin.views.main import ChangeList
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import models
from django.db.models import prefetch_related_objects
from django.http import Http404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html
from django.utils.http import urlencode
from django.utils.text import capfirst
from django.utils.translation import gettext as _, gettext_lazy

from parquet_archive.forms import ArchivePolicyForm
from parquet_archive.models import ArchivePolicy
from parquet_archive.query import ArchiveFolder, ArchiveQuery, UnsupportedLookup, period_bounds
from parquet_archive.schema import day_of
from parquet_archive.tasks import archive_tables_task

ARCHIVE_MAX_DAYS = 31


def _start_of_day(field, day):
    """Start of the day in the field type: the date, or midnight in the project time zone (in UTC) for DateTimeField."""
    if not isinstance(field, models.DateTimeField):
        return day
    midnight = datetime.combine(day, time.min)
    if not settings.USE_TZ:
        return midnight
    # NOTE: polars does not compare the column written in UTC with a datetime in the project's named time zone
    return timezone.make_aware(midnight, timezone.get_default_timezone()).astimezone(dt_timezone.utc)


def _short_date(day):
    return date_format(day, 'SHORT_DATE_FORMAT')


class DataSourceFilter(admin.SimpleListFilter):
    """
    Switches the changelist between the database rows and the archived rows.
    The actual switch is done by ArchiveAdminMixin.get_changelist.
    """

    title = gettext_lazy('data source')
    parameter_name = 'data_source'
    ARCHIVE = 'archive'

    def lookups(self, request, model_admin):
        return ((self.ARCHIVE, _('Archive')),)

    def choices(self, changelist):
        remove = [self.parameter_name]
        if self.value() == self.ARCHIVE:
            remove += [param for list_filter in changelist.filter_specs for param in list_filter.expected_parameters()]
        yield {
            'selected': self.value() is None,
            'query_string': changelist.get_query_string(remove=remove),
            'display': _('Database'),
        }
        yield {
            'selected': self.value() == self.ARCHIVE,
            'query_string': changelist.get_query_string({self.parameter_name: self.ARCHIVE}),
            'display': _('Archive'),
        }

    def queryset(self, request, queryset):
        return None


class ArchiveDayFilter(FieldListFilter):
    """Filter of the partition_field with the months and, for the month shown, the days that exist in the archive."""

    def __init__(self, field, request, params, model, model_admin, field_path):
        self.lookup_kwarg_since = f'{field_path}__gte'
        self.lookup_kwarg_until = f'{field_path}__lt'
        super().__init__(field, request, params, model, model_admin, field_path)
        self.dates = {key: values[-1] for key, values in self.used_parameters.items()}

    def expected_parameters(self):
        return [self.lookup_kwarg_since, self.lookup_kwarg_until]

    def _choice(self, changelist, since, until, display, selected=False):
        dates = {
            self.lookup_kwarg_since: str(_start_of_day(self.field, since)),
            self.lookup_kwarg_until: str(_start_of_day(self.field, until)),
        }
        return {
            'selected': selected or self.dates == dates,
            'query_string': changelist.get_query_string(dates, [f'{self.field_path}__']),
            'display': display,
        }

    def choices(self, changelist):
        period = changelist.archive_period
        shown_month = period and (period[1].year, period[1].month)
        for month, days in groupby(changelist.archive_folder.days(self.field_path), attrgetter('year', 'month')):
            first_day = date(*month, 1)
            yield self._choice(
                changelist, first_day, (first_day + timedelta(days=32)).replace(day=1),
                date_format(first_day, 'YEAR_MONTH_FORMAT')
            )
            if month != shown_month:
                continue
            for day in days:
                # NOTE: Without a date filter the changelist shows the most recent day
                yield self._choice(
                    changelist, day, day + timedelta(days=1), _short_date(day),
                    selected=not self.dates and period == (day, day)
                )


class ArchiveChangeList(ChangeList):
    """
    ChangeList that lists the archived rows (parquet files) instead of the
    database. Field filters, search and ordering are translated by
    ArchiveQuery, and whatever is not in the archive columns is ignored with a
    warning.
    """

    def get_queryset(self, request, exclude_parameters=None):
        # NOTE: Facets and date_hierarchy run aggregations on the database
        self.add_facets = self.is_facets_optional = False
        (
            self.filter_specs,
            self.has_filters,
            remaining_lookup_params,
            _filters_may_have_duplicates,
            self.has_active_filters,
        ) = self.get_filters(request)
        self.date_hierarchy = None

        self.archive_folder = ArchiveFolder(self.model)
        self.partition_field = ArchivePolicy.get_partition_field(self.model)
        ignored_filters = []
        try:
            query = self.root_queryset = self._period_query(request, remaining_lookup_params)
            for list_filter in self.filter_specs:
                if not isinstance(list_filter, FieldListFilter):
                    continue
                try:
                    query = query.apply_lookups(list_filter.used_parameters)
                except UnsupportedLookup:
                    ignored_filters.append(str(list_filter.title))

            # NOTE: URL lookups were already validated against the model by get_filters
            for lookup, values in remaining_lookup_params.items():
                try:
                    query = query.apply_lookups({lookup: values})
                except UnsupportedLookup:
                    ignored_filters.append(lookup)

            query = query.sort(self.get_ordering(request, self.model._default_manager.none()))
            query, ignored_search_fields = query.search(self.model_admin.get_search_fields(request), self.query)
        except (ValidationError, ValueError, OverflowError) as e:
            raise IncorrectLookupParameters(e)

        if ignored_filters:
            messages.warning(
                request,
                _('Filters unavailable in the archive were ignored: %(filters)s.')
                % {'filters': ', '.join(ignored_filters)}
            )
        if ignored_search_fields:
            messages.warning(
                request,
                _('The archive search does not include the fields: %(fields)s.')
                % {'fields': ', '.join(ignored_search_fields)}
            )

        self.clear_all_filters_qs = self.get_query_string(
            new_params={**remaining_lookup_params, DataSourceFilter.parameter_name: DataSourceFilter.ARCHIVE},
            remove=self.get_filters_params(),
        )
        return query

    def _period_query(self, request, remaining_lookup_params):
        """
        Query with only the files and rows of the partition_field period: the
        filter's, up to ARCHIVE_MAX_DAYS days, or the most recent day when the
        filter has no date. Without day subfolders, it reads every file.
        """
        self.archive_period = None
        days = self.archive_folder.days(self.partition_field) if self.partition_field else []
        if not days:
            return ArchiveQuery(self.model, self.archive_folder)

        field = self.model._meta.get_field(self.partition_field)
        lookups = dict(remaining_lookup_params)
        for list_filter in self.filter_specs:
            if isinstance(list_filter, FieldListFilter):
                lookups.update(list_filter.used_parameters)
        start, end = period_bounds(field, lookups)

        if start is None and end is None:
            start = end = days[0]
            messages.info(
                request,
                _('Showing %(day)s, the most recent day in the archive. Filter by %(field)s to see other days.')
                % {'day': _short_date(days[0]), 'field': field.verbose_name}
            )
        else:
            start = date.min if start is None else start
            end = days[0] if end is None else min(end, days[0])
            if (end - start).days >= ARCHIVE_MAX_DAYS:
                start = end - timedelta(days=ARCHIVE_MAX_DAYS - 1)
                messages.warning(
                    request,
                    _('The archive shows up to %(max_days)s days at a time: showing %(start)s to %(end)s.')
                    % {'max_days': ARCHIVE_MAX_DAYS, 'start': _short_date(start), 'end': _short_date(end)}
                )
        self.archive_period = start, end

        period_days = {day for day in days if start <= day <= end}
        frame = self.archive_folder.load(self.partition_field, period_days)
        query = ArchiveQuery(self.model, self.archive_folder, frame)
        # NOTE: Files straight in the model folder have no day, so their rows are filtered too
        period_lookups = {
            f'{self.partition_field}__gte': [_start_of_day(field, start)],
            f'{self.partition_field}__lt': [_start_of_day(field, end + timedelta(days=1))],
        }
        try:
            return query.apply_lookups(period_lookups)
        except UnsupportedLookup:
            return query

    def get_results(self, request):
        super().get_results(request)

        # NOTE: Replaces select_related, which does not exist for instances read from parquet
        relations = []
        for field_name in self.list_display:
            try:
                field = self.lookup_opts.get_field(field_name)
            except FieldDoesNotExist:
                continue
            if field.many_to_one and field.attname in self.queryset.columns:
                relations.append(field_name)
        prefetch_related_objects(self.result_list, *relations)

    def url_for_result(self, result):
        url = reverse(
            'admin:%s_%s_archive' % (self.opts.app_label, self.opts.model_name),
            args=(quote(getattr(result, self.pk_attname)),),
            current_app=self.model_admin.admin_site.name,
        )
        # NOTE: With the day the detail reads only its subfolder
        day = day_of(getattr(result, self.partition_field)) if self.partition_field else None
        return f'{url}?day={day.isoformat()}' if day else url


class ArchiveAdminMixin:
    """
    Adds the "Data source" filter to the changelist to list the database rows
    or the archived rows (parquet files written by archive_tables).

    In the archive the field filters, search and ordering are applied to the
    columns that exist in the parquet files. Custom filters (SimpleListFilter),
    actions, facets and date_hierarchy are disabled and the row detail is
    read-only. With day subfolders (partition_field), the field filter becomes
    ArchiveDayFilter and each page reads only the days of the period.
    """

    def is_archive(self, request):
        return request.GET.get(DataSourceFilter.parameter_name) == DataSourceFilter.ARCHIVE

    def get_list_filter(self, request):
        list_filter = list(super().get_list_filter(request))
        if not self.is_archive(request):
            return [DataSourceFilter, *list_filter]

        # NOTE: Custom filters only know how to filter database querysets
        list_filter = [item for item in list_filter if not callable(item)]
        partition_field = ArchivePolicy.get_partition_field(self.model)
        if partition_field is None:
            return [DataSourceFilter, *list_filter]

        # NOTE: The shortcuts of the default date filter (today, this month...) count from today, and the archive
        # only has old rows
        other_filters = [
            item for item in list_filter if (item if isinstance(item, str) else item[0]) != partition_field
        ]
        return [DataSourceFilter, (partition_field, ArchiveDayFilter), *other_filters]

    def get_changelist(self, request, **kwargs):
        if self.is_archive(request):
            return ArchiveChangeList
        return super().get_changelist(request, **kwargs)

    # NOTE: Django 6.1 only passes action_location when it is in the signature, and before it the parameter does not
    # exist
    def get_actions(self, request, action_location=None):
        if self.is_archive(request):
            return {}
        if action_location is None:
            return super().get_actions(request)
        return super().get_actions(request, action_location=action_location)

    def changelist_view(self, request, extra_context=None):
        if self.is_archive(request):
            self.message_user(request, _('Showing archived rows (parquet files), read-only.'), messages.INFO)
        return super().changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        info = self.opts.app_label, self.opts.model_name
        return [
            path(
                'parquet-archive/<path:object_id>/',
                self.admin_site.admin_view(self.archive_view),
                name='%s_%s_archive' % info
            ),
        ] + super().get_urls()

    def archive_view(self, request, object_id):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied

        folder = ArchiveFolder(self.model)
        partition_field = ArchivePolicy.get_partition_field(self.model)
        changelist_params = {DataSourceFilter.parameter_name: DataSourceFilter.ARCHIVE}
        days = ()
        if partition_field:
            # NOTE: The changelist link carries the day, and without it only the files straight in the model folder
            # are searched
            date_field = self.model._meta.get_field(partition_field)
            try:
                day = date.fromisoformat(request.GET['day']) if 'day' in request.GET else None
                if day:
                    next_day = day + timedelta(days=1)
                    changelist_params[f'{partition_field}__gte'] = str(_start_of_day(date_field, day))
                    changelist_params[f'{partition_field}__lt'] = str(_start_of_day(date_field, next_day))
            except (ValueError, OverflowError):
                raise Http404(_('Invalid day: %(day)s.') % {'day': request.GET['day']})
            days = {day} if day else set()

        frame = folder.load(partition_field, days)
        query = ArchiveQuery(self.model, folder, frame).apply_lookups({'pk': [unquote(object_id)]})
        rows = query[:1]
        if not rows:
            raise Http404(_('Row %(pk)s not found in the archive.') % {'pk': object_id})

        changelist_url = reverse(
            'admin:%s_%s_changelist' % (self.opts.app_label, self.opts.model_name),
            current_app=self.admin_site.name
        )
        context = {
            **self.admin_site.each_context(request),
            'opts': self.opts,
            'title': _('%(name)s from the archive') % {'name': capfirst(self.opts.verbose_name)},
            'subtitle': object_id,
            'fields': [
                (field.verbose_name, self._archive_display_value(rows[0], field))
                for field in self.opts.concrete_fields if field.attname in query.columns
            ],
            'changelist_url': f'{changelist_url}?{urlencode(changelist_params)}',
        }
        return TemplateResponse(request, 'admin/parquet_archive/detail.html', context)

    def _archive_display_value(self, row, field):
        value = getattr(row, field.attname)
        if field.is_relation and value is not None:
            try:
                return str(getattr(row, field.name))
            except ObjectDoesNotExist:
                return value
        return display_for_field(value, field, self.get_empty_value_display())


class ArchivePolicyAdmin(admin.ModelAdmin):
    form = ArchivePolicyForm
    list_display = ('__str__', 'active', 'get_keep_for', 'get_date_fields', 'updated_at')
    list_filter = ('active',)
    ordering = ('content_type__app_label', 'content_type__model')
    readonly_fields = ('get_config',)
    actions = ('archive_tables',)
    fieldsets = (
        (None, {'fields': ('content_type', 'active')}),
        (gettext_lazy('Row selection'), {'fields': ('date_fields', 'keep_for', 'unit')}),
        (gettext_lazy('Parquet files'), {'fields': ('partition_field', 'rows_per_file', 'ordering', 'columns')}),
        (gettext_lazy('Saved configuration'), {'fields': ('get_config',)}),
    )

    class Media:
        js = ('parquet_archive/js/admin.js',)

    # NOTE: The policy defines which tables have rows deleted, so only superusers change or run it
    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_fieldsets(self, request, obj=None):
        if obj and not self.has_change_permission(request, obj):
            return ((None, {'fields': ('content_type', 'active', 'get_config')}),)
        return super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request, obj=None):
        # NOTE: Changing the model of an existing policy would leave the schema inconsistent
        return self.readonly_fields + (('content_type',) if obj else ())

    @admin.display(description=gettext_lazy('Keep for'))
    def get_keep_for(self, obj):
        row_filter = obj.config['filter']
        unit = ArchivePolicy.TimeUnit(row_filter['unit']).label.lower()
        return f"{row_filter['keep_for']} {unit}"

    @admin.display(description=gettext_lazy('Date fields'))
    def get_date_fields(self, obj):
        return ', '.join(obj.config['filter']['fields'])

    @admin.display(description=gettext_lazy('Configuration'))
    def get_config(self, obj):
        if not obj.pk:
            return '-'
        return format_html('<pre>{}</pre>', json.dumps(obj.config, indent=2, ensure_ascii=False))

    @admin.action(description=gettext_lazy('Archive the selected tables now'), permissions=['change'])
    def archive_tables(self, request, queryset):
        model_names = list(queryset.filter(active=True).values_list('content_type__model', flat=True))
        if not model_names:
            # NOTE: An empty list would make the task archive every table
            self.message_user(request, _('No active policy selected.'), messages.WARNING)
            return

        archive_tables_task.delay(model_names=model_names)
        self.message_user(request, _('Archiving the tables: %(tables)s') % {'tables': ', '.join(model_names)})


admin.site.register(ArchivePolicy, ArchivePolicyAdmin)
