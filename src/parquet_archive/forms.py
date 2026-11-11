from django import forms
from django.conf import settings
from django.contrib.admin.widgets import FilteredSelectMultiple, get_select2_language
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext, gettext_lazy as _

from parquet_archive.models import ArchivePolicy
from parquet_archive.schema import date_field_paths, polars_type_for_field, relations_blocking_delete


class SearchableSelect(forms.Select):
    """Select with the admin select2 search, the same as autocomplete_fields, over the options on the page."""

    def build_attrs(self, base_attrs, extra_attrs=None):
        attrs = super().build_attrs(base_attrs, extra_attrs)
        return {**attrs, 'data-theme': 'admin-autocomplete', 'lang': get_select2_language()}

    @property
    def media(self):
        extra = '' if settings.DEBUG else '.min'
        language = get_select2_language()
        return forms.Media(
            js=(
                f'admin/js/vendor/jquery/jquery{extra}.js',
                f'admin/js/vendor/select2/select2.full{extra}.js',
                *([f'admin/js/vendor/select2/i18n/{language}.js'] if language else []),
                'admin/js/jquery.init.js',
            ),
            css={'screen': (f'admin/css/vendor/select2/select2{extra}.css', 'admin/css/autocomplete.css')},
        )


class ArchivePolicyForm(forms.ModelForm):
    """
    Builds the archive config dict from admin fields.

    The choices of date fields, ordering and saved columns come from the
    chosen model (on creation the admin reloads the form with
    ?content_type=<id> when the model changes) and the polars schema is derived
    from the field types. The primary key and the ordering field are always
    saved.
    """

    content_type = forms.ModelChoiceField(
        queryset=ContentType.objects.order_by('app_label', 'model'),
        widget=SearchableSelect,
        label=_('Model'),
        help_text=_('The selected rows are written to the archive and DELETED from this table.')
    )
    date_fields = forms.MultipleChoiceField(
        label=_('Date fields'),
        widget=FilteredSelectMultiple(_('date fields'), is_stacked=False),
        help_text=_('A row is archived when any of these fields is older than the configured age.')
    )
    keep_for = forms.IntegerField(
        min_value=1,
        label=_('Keep for'),
        help_text=_('Rows older than this are moved to the archive.')
    )
    unit = forms.ChoiceField(
        label=_('Unit'),
        choices=ArchivePolicy.TimeUnit.choices,
        initial=ArchivePolicy.TimeUnit.MONTHS
    )
    rows_per_file = forms.IntegerField(
        min_value=1,
        initial=3000,
        label=_('Rows per file'),
        help_text=_('Number of rows per parquet file. Ideally each file ends up close to 512MB.')
    )
    ordering = forms.ChoiceField(
        required=False,
        label=_('Ordering'),
        help_text=_(
            'Order of the rows in the parquet files, and the field is always saved. With a date field of the model '
            'that is not null (e.g. the creation date), the admin reads only the files of each page.'
        )
    )
    columns = forms.MultipleChoiceField(
        label=_('Saved columns'),
        widget=FilteredSelectMultiple(_('columns'), is_stacked=False),
        help_text=_('The primary key is always saved.')
    )

    class Meta:
        model = ArchivePolicy
        fields = ('content_type', 'active')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        model = self._get_selected_model()

        if self.instance.pk:
            config = self.instance.config
            pk = model._meta.pk.attname if model else None
            self.initial.update({
                'date_fields': config['filter']['fields'],
                'keep_for': config['filter']['keep_for'],
                'unit': config['filter']['unit'],
                'rows_per_file': config['rows_per_file'],
                'ordering': ','.join(config['ordering']),
                'columns': [field for field in self.instance.get_schema_fields() if field != pk],
            })

        if model is None:
            for field_name in ('date_fields', 'ordering', 'columns'):
                self.fields[field_name].help_text = _('Choose the model to list its fields.')
        else:
            self._build_choices(model)

    def _get_selected_model(self):
        if self.instance.pk:
            return self.instance.content_type.model_class()

        content_type = (
            self.data.get(self.add_prefix('content_type')) if self.is_bound else self.initial.get('content_type')
        )
        if isinstance(content_type, ContentType):
            return content_type.model_class()
        try:
            return ContentType.objects.get(pk=content_type).model_class() if content_type else None
        except (ContentType.DoesNotExist, ValueError, TypeError):
            return None

    def _build_choices(self, model):
        column_choices, unsupported = [], []
        for field in model._meta.concrete_fields:
            if field.primary_key:
                continue
            type_name = polars_type_for_field(field)
            if type_name:
                column_choices.append((field.attname, f'{field.attname} ({type_name})'))
            else:
                unsupported.append(field.attname)

        self.fields['columns'].choices = column_choices
        if unsupported:
            unsupported_text = gettext(
                'Not supported in the archive (encrypted fields, custom value objects...): %(fields)s.'
            ) % {'fields': ', '.join(unsupported)}
            self.fields['columns'].help_text = f"{self.fields['columns'].help_text} {unsupported_text}"
        if not self.instance.pk:
            # NOTE: On a new policy every supported column comes selected
            self.initial.setdefault('columns', [value for value, _ in column_choices])

        # NOTE: Paths already saved outside the choices (e.g. relations more than one level deep) stay available
        date_choices = {path: f'{path} ({description})' for path, description in date_field_paths(model)}
        for path in self.initial.get('date_fields') or []:
            date_choices.setdefault(path, path)
        self.fields['date_fields'].choices = list(date_choices.items())

        ordering_choices = {'': '---------'}
        for field in model._meta.concrete_fields:
            ordering_choices[field.attname] = gettext('%(field)s (ascending)') % {'field': field.attname}
            ordering_choices[f'-{field.attname}'] = gettext('%(field)s (descending)') % {'field': field.attname}
        saved_ordering = self.initial.get('ordering')
        if saved_ordering:
            ordering_choices.setdefault(saved_ordering, saved_ordering)
        self.fields['ordering'].choices = list(ordering_choices.items())

    def clean(self):
        cleaned_data = super().clean()

        # NOTE: In the admin the content type is readonly when editing and is not in the form
        content_type = cleaned_data.get('content_type') or getattr(self.instance, 'content_type', None)
        if content_type is None:
            return cleaned_data

        content_type_error_field = 'content_type' if 'content_type' in self.fields else None
        model = content_type.model_class()
        if model is None:
            self.add_error(content_type_error_field, gettext('Model not found in the project.'))
            return cleaned_data

        relations = relations_blocking_delete(model)
        if relations:
            self.add_error(
                content_type_error_field,
                gettext('Rows are deleted after archiving and %(model)s has relations that prevent it: %(relations)s.')
                % {'model': model._meta.label, 'relations': ', '.join(relations)}
            )

        ordering_field = (cleaned_data.get('ordering') or '').split(',')[0].removeprefix('-')
        cleaned_data['schema'] = self._build_schema(model, [ordering_field, *cleaned_data.get('columns', [])])
        return cleaned_data

    def _build_schema(self, model, columns):
        pk = model._meta.pk
        if polars_type_for_field(pk) is None:
            self.add_error(
                'columns',
                gettext('The primary key (%(field)s) has no type supported by the archive.') % {'field': pk.attname}
            )

        selected = {pk.attname, *columns}
        schema = {}
        for field in model._meta.concrete_fields:
            type_name = polars_type_for_field(field)
            if field.attname in selected and type_name:
                schema[field.attname] = type_name
        return schema

    def save(self, commit=True):
        self.instance.config = {
            'rows_per_file': self.cleaned_data['rows_per_file'],
            'filter': {
                'fields': self.cleaned_data['date_fields'],
                'keep_for': self.cleaned_data['keep_for'],
                'unit': self.cleaned_data['unit'],
            },
            'ordering': [item for item in self.cleaned_data['ordering'].split(',') if item],
            'schema': self.cleaned_data['schema'],
        }
        return super().save(commit=commit)
