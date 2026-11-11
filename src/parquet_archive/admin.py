import json
import logging
from datetime import date, timedelta, timezone as dt_timezone
from functools import partial
from itertools import groupby
from operator import attrgetter

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin import FieldListFilter
from django.contrib.admin.options import IncorrectLookupParameters
from django.contrib.admin.utils import display_for_field, quote, unquote
from django.contrib.admin.views.main import ORDER_VAR, PAGE_VAR, ChangeList
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import models, transaction
from django.db.models import Count, Q, prefetch_related_objects
from django.db.models.constants import LOOKUP_SEP
from django.http import Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import date_format, localize
from django.utils.html import format_html
from django.utils.http import urlencode
from django.utils.text import capfirst
from django.utils.translation import gettext as _, gettext_lazy, ngettext

from parquet_archive.conf import get_max_files
from parquet_archive.forms import ArchivePolicyForm
from parquet_archive.models import ArchiveFile, ArchivePolicy
from parquet_archive.query import (
    READ_ERRORS, ArchiveFolder, ArchiveQuery, IndexedQuery, UnsupportedLookup, file_groups, index_range,
    period_bounds
)
from parquet_archive.schema import day_of, sort_newest_first, start_of_day
from parquet_archive.tasks import archive_tables_task, index_files_task

logger = logging.getLogger(__name__)

PERIOD_LOOKUPS = ('gt', 'gte', 'lt', 'lte')

SCHEMA_READ_ATTEMPTS = 5


def _start_of_day(field, day):
    """Start of the day in the field type: the date, or midnight in the project time zone (in UTC) for DateTimeField."""
    if not isinstance(field, models.DateTimeField):
        return day
    if not settings.USE_TZ:
        return start_of_day(day)
    # NOTE: polars does not compare the column written in UTC with a datetime in the project's named time zone
    return start_of_day(day).astimezone(dt_timezone.utc)


def _queue_indexing(policy):
    """Queues the indexing of the policy's archive files, releasing the request when the broker refuses it."""
    try:
        index_files_task.delay(model_names=[policy.content_type.model])
    except Exception:
        # NOTE: Each broker raises its own errors, and without the request the next page view queues it again
        logger.exception('The indexing of the archive files of %s could not be queued.', policy)
        ArchivePolicy.objects.filter(pk=policy.pk).update(indexing_requested_at=None)


def _covered_days(files):
    """Days (in the project time zone) that the ranges of the archive files cover, from newest to oldest."""
    days = set()
    for file in files:
        day, last_day = day_of(file.min_date), day_of(file.max_date)
        while day <= last_day:
            days.add(day)
            day += timedelta(days=1)
    return sorted(days, reverse=True)


def _is_period_only(field, lookups):
    """Whether the lookups only bound the date field, one value each, which the index counts without reading."""
    for lookup, values in lookups.items():
        values = values if isinstance(values, (list, tuple)) else [values]
        name, _separator, lookup_type = lookup.partition(LOOKUP_SEP)
        if values and (name != field.attname or lookup_type not in PERIOD_LOOKUPS or len(values) > 1):
            return False
    return True


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
    """Filter of the index field with the months and the days of the shown month that the archive files cover."""

    def __init__(self, field, request, params, model, model_admin, field_path):
        self.lookup_kwarg_since = f'{field_path}__gte'
        self.lookup_kwarg_until = f'{field_path}__lt'
        super().__init__(field, request, params, model, model_admin, field_path)
        self.dates = {key: values[-1] for key, values in self.used_parameters.items()}

    def expected_parameters(self):
        return [self.lookup_kwarg_since, self.lookup_kwarg_until]

    def _choice(self, changelist, since, until, display):
        dates = {
            self.lookup_kwarg_since: str(_start_of_day(self.field, since)),
            self.lookup_kwarg_until: str(_start_of_day(self.field, until)),
        }
        return {
            'selected': self.dates == dates,
            'query_string': changelist.get_query_string(dates, [f'{self.field_path}__']),
            'display': display,
        }

    def choices(self, changelist):
        yield {
            'selected': not self.dates,
            'query_string': changelist.get_query_string(remove=[f'{self.field_path}__']),
            'display': _('All'),
        }
        shown_day = changelist.archive_period and changelist.archive_period[1]
        shown_month = shown_day and (shown_day.year, shown_day.month)
        for month, days in groupby(changelist.archive_days, attrgetter('year', 'month')):
            first_day = date(*month, 1)
            yield self._choice(
                changelist, first_day, (first_day + timedelta(days=32)).replace(day=1),
                date_format(first_day, 'YEAR_MONTH_FORMAT')
            )
            if month != shown_month:
                continue
            for day in days:
                yield self._choice(changelist, day, day + timedelta(days=1), _short_date(day))


class ArchiveChangeList(ChangeList):
    """ChangeList that lists the archived rows (parquet files) instead of the database."""

    def get_queryset(self, request, exclude_parameters=None):
        """
        With an index field (ArchivePolicy.get_index_field), the files come
        from ArchiveFile: without other filters, search or ordering, it counts
        by the index and reads only the files of the page (IndexedQuery).
        Otherwise it reads the files of the period, up to get_max_files(), the
        most recent ones first. Without an index field it reads every file.

        Field filters, search and ordering are translated by ArchiveQuery, and
        whatever is not in the archive columns is ignored with a warning.
        """
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

        policy, self.index_field = ArchivePolicy.get_with_index_field(self.model)
        self.archive_period, self.archive_days = None, []
        field_filters = [list_filter for list_filter in self.filter_specs if isinstance(list_filter, FieldListFilter)]
        lookups = dict(remaining_lookup_params)
        for list_filter in field_filters:
            lookups.update(list_filter.used_parameters)
        ordering = self.get_ordering(request, self.model._default_manager.none())
        has_ordering = ORDER_VAR in self.params or self.model_admin.get_ordering(request) or self.lookup_opts.ordering
        if self.index_field and not has_ordering:
            # NOTE: Without an ordering of the admin or of the model, the archive is listed by the index field, which
            # is how the files are written
            ordering = [f'-{self.index_field}', *ordering]

        try:
            if self.index_field:
                query = self._indexed_query(request, policy, lookups, ordering)
            else:
                query = self.root_queryset = ArchiveQuery(self.model)
            if isinstance(query, ArchiveQuery):
                query = self._filter(request, query, field_filters, remaining_lookup_params, ordering)
        except (ValidationError, ValueError, OverflowError) as e:
            raise IncorrectLookupParameters(e)
        except READ_ERRORS:
            query = self._unreadable_files(request)

        self.clear_all_filters_qs = self.get_query_string(
            new_params={**remaining_lookup_params, DataSourceFilter.parameter_name: DataSourceFilter.ARCHIVE},
            remove=self.get_filters_params(),
        )
        return query

    def _filter(self, request, query, field_filters, remaining_lookup_params, ordering):
        ignored_filters = []
        for list_filter in field_filters:
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

        query = query.sort(ordering)
        query, ignored_search_fields = query.search(self.model_admin.get_search_fields(request), self.query)

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
        return query

    def _indexed_query(self, request, policy, lookups, ordering):
        field = self.model._meta.get_field(self.index_field)
        table_files = ArchiveFile.objects.filter(content_type=policy.content_type)
        self._check_the_index(request, policy, table_files, field)
        files = list(
            table_files.filter(field=field.attname, min_date__isnull=False)
            .order_by('-max_date')
            .values_list('path', 'rows', 'min_date', 'max_date', named=True)
        )
        self.archive_days = _covered_days(files)
        # NOTE: Each page reads only a few files, and the newest one has every column (old files get the new ones empty)
        schema = self._newest_schema(files)
        self.archive_folder = ArchiveFolder(self.model, schema)
        self.root_queryset = IndexedQuery(self.model, self.archive_folder, files)

        start, end = period_bounds(field, lookups)
        if start is not None or end is not None:
            self.archive_period = (
                start and day_of(start), end and day_of(end - timedelta(microseconds=1))
            )
        period_files = [
            file for file in files
            if (start is None or file.max_date >= start) and (end is None or file.min_date < end)
        ]

        sorts_by_the_index = isinstance(ordering[0], str) and ordering[0].removeprefix('-') == field.attname
        if _is_period_only(field, lookups) and not self.query and sorts_by_the_index:
            return IndexedQuery(self.model, self.archive_folder, period_files, (start, end), lookups, ordering)

        paths = self._limit_files(request, field, period_files)
        return ArchiveQuery(self.model, self.archive_folder, self.archive_folder.read(self.archive_folder.sources(
            sort_newest_first(paths)
        )))

    def _check_the_index(self, request, policy, table_files, field):
        """
        Queues the indexing of the archive files, without a manual step, when
        the table was never indexed (e.g. right after the upgrade) or has files
        indexed by another field (the ordering changed), and warns about the
        files left out of the listing.
        """
        left_out = table_files.aggregate(
            other_field=Count('pk', filter=~Q(field=field.attname)),
            without_date=Count('pk', filter=Q(field=field.attname, min_date__isnull=True)),
        )
        if policy.indexed_at is None or left_out['other_field']:
            if policy.claim_indexing():
                # NOTE: After the commit, so the worker does not run before the request is saved
                transaction.on_commit(partial(_queue_indexing, policy))
            messages.warning(request, _(
                'The archive files of this table are being indexed, so the listing may be missing some of them for '
                'a few minutes.'
            ))
        if left_out['without_date']:
            messages.warning(request, ngettext(
                '%(count)s archive file of this table has no %(field)s and is not listed.',
                '%(count)s archive files of this table have no %(field)s and are not listed.',
                left_out['without_date'],
            ) % {'count': left_out['without_date'], 'field': field.verbose_name})

    def _newest_schema(self, files):
        """Columns of the newest archive file that can be read."""
        if not files:
            return None

        folder = ArchiveFolder(self.model)
        for file_path in sort_newest_first(file.path for file in files)[:SCHEMA_READ_ATTEMPTS]:
            try:
                return folder.read_schema(file_path)
            except READ_ERRORS as error:
                # NOTE: A file removed from the storage stays in the index until the next indexing, and only the
                # pages that read it show the error
                read_error = error
        raise read_error

    def _unreadable_files(self, request):
        """Empty query instead of the rows, with an error message, when an archive file could not be read."""
        logger.exception('Archive files of %s could not be read.', self.opts.label)
        if self.index_field:
            messages.error(request, _(
                'Some archive files of this table could not be read, e.g. because they were removed from the storage. '
                'In that case, index the files again in the archive policies.'
            ))
        else:
            messages.error(request, _('Some archive files of this table could not be read.'))

        folder = ArchiveFolder(self.model)
        empty = ArchiveQuery(self.model, folder, folder.read([]))
        # NOTE: The total of the index does not read the files and stays right
        if not isinstance(getattr(self, 'root_queryset', None), IndexedQuery):
            self.root_queryset = empty
        return empty

    def _limit_files(self, request, field, files):
        """
        Paths of the files of the period, or of its most recent groups up to
        get_max_files(), with a link to continue before them.
        """
        max_files = get_max_files()
        if len(files) <= max_files:
            return [file.path for file in files]

        paths = []
        for group in file_groups(files):
            paths += [file.path for file in group.files]
            if len(paths) >= max_files:
                break
        # NOTE: Groups do not overlap, so the files left out only have rows before the oldest one read. A date
        # field saves its days as their midnight
        before = group.min_date if isinstance(field, models.DateTimeField) else day_of(group.min_date)
        continue_url = self.get_query_string({
            f'{field.attname}__lt': str(before), f'{field.attname}__lte': None, PAGE_VAR: None,
        })
        since = localize(timezone.template_localtime(group.min_date))
        messages.warning(request, format_html(
            _('The period has more archive files than a page reads: showing the rows since {since}. '
              '<a href="{url}">Continue before {since}</a>.'),
            since=since, url=continue_url,
        ))
        return paths

    def get_results(self, request):
        try:
            super().get_results(request)
        except READ_ERRORS:
            # NOTE: On S3 the files are only read here, when the page is counted and sliced
            self.queryset = self._unreadable_files(request)
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
        # NOTE: With the date of the row the detail reads only the files whose range has it
        value = getattr(result, self.index_field) if self.index_field else None
        return f'{url}?{urlencode({"at": value.isoformat()})}' if value else url


class ArchiveAdminMixin:
    """Adds the "Data source" filter, which lists the archived rows read-only (see ArchiveChangeList)."""

    def is_archive(self, request):
        return request.GET.get(DataSourceFilter.parameter_name) == DataSourceFilter.ARCHIVE

    def get_list_filter(self, request):
        list_filter = list(super().get_list_filter(request))
        if not self.is_archive(request):
            return [DataSourceFilter, *list_filter]

        # NOTE: Custom filters only know how to filter database querysets
        list_filter = [item for item in list_filter if not callable(item)]
        _policy, index_field = ArchivePolicy.get_with_index_field(self.model)
        if index_field is None:
            return [DataSourceFilter, *list_filter]

        # NOTE: The shortcuts of the default date filter (today, this month...) count from today, and the archive
        # only has old rows
        other_filters = [
            item for item in list_filter if (item if isinstance(item, str) else item[0]) != index_field
        ]
        return [DataSourceFilter, (index_field, ArchiveDayFilter), *other_filters]

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
        policy, index_field = ArchivePolicy.get_with_index_field(self.model)
        changelist_params = {DataSourceFilter.parameter_name: DataSourceFilter.ARCHIVE}
        if index_field:
            # NOTE: The changelist link carries the date of the row, and the files whose range has it are the only
            # ones read
            date_field = self.model._meta.get_field(index_field)
            try:
                at, _end = index_range(date_field.to_python(request.GET['at']))
                day = day_of(at)
                changelist_params[f'{index_field}__gte'] = str(_start_of_day(date_field, day))
                changelist_params[f'{index_field}__lt'] = str(_start_of_day(date_field, day + timedelta(days=1)))
            except (KeyError, TypeError, ValidationError, ValueError, OverflowError):
                raise Http404(_('Invalid or missing date of the row (at).'))
            paths = ArchiveFile.objects.filter(
                content_type=policy.content_type, field=index_field, min_date__lte=at, max_date__gte=at
            ).values_list('path', flat=True)
        else:
            paths = None

        changelist_url = reverse(
            'admin:%s_%s_changelist' % (self.opts.app_label, self.opts.model_name),
            current_app=self.admin_site.name
        )
        try:
            frame = folder.load() if paths is None else folder.read(folder.sources(sort_newest_first(paths)))
            query = ArchiveQuery(self.model, folder, frame).apply_lookups({'pk': [unquote(object_id)]})
            rows = query[:1]
        except READ_ERRORS:
            # NOTE: Like the admin with an object that no longer exists, it goes back to the list with the message
            logger.exception('Archive file of a row of %s could not be read.', self.opts.label)
            if index_field:
                messages.error(request, _(
                    'The archive file of the row could not be read, e.g. because it was removed from the storage. '
                    'In that case, index the files again in the archive policies.'
                ))
            else:
                messages.error(request, _('Some archive files of this table could not be read.'))
            return redirect(f'{changelist_url}?{urlencode(changelist_params)}')
        if not rows:
            raise Http404(_('Row %(pk)s not found in the archive.') % {'pk': object_id})

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
    list_display = ('__str__', 'active', 'get_keep_for', 'get_date_fields', 'updated_at', 'indexed_at')
    list_filter = ('active',)
    ordering = ('content_type__app_label', 'content_type__model')
    readonly_fields = ('get_config', 'indexed_at')
    actions = ('archive_tables', 'index_files')
    fieldsets = (
        (None, {'fields': ('content_type', 'active')}),
        (gettext_lazy('Row selection'), {'fields': ('date_fields', 'keep_for', 'unit')}),
        (gettext_lazy('Parquet files'), {'fields': ('rows_per_file', 'ordering', 'columns')}),
        (gettext_lazy('Saved configuration'), {'fields': ('get_config', 'indexed_at')}),
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
            return ((None, {'fields': ('content_type', 'active', 'get_config', 'indexed_at')}),)
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

    @admin.action(description=gettext_lazy('Index the archive files of the selected tables'), permissions=['change'])
    def index_files(self, request, queryset):
        model_names = list(queryset.values_list('content_type__model', flat=True))
        index_files_task.delay(model_names=model_names)
        self.message_user(
            request, _('Indexing the archive files of the tables: %(tables)s') % {'tables': ', '.join(model_names)}
        )


admin.site.register(ArchivePolicy, ArchivePolicyAdmin)
