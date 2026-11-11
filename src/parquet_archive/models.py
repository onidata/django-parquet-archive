import operator
from functools import reduce

from dateutil.relativedelta import relativedelta

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from parquet_archive.schema import to_polars_schema


class ArchivePolicy(models.Model):
    """
    Table that is archived (parquet_archive.archive.archive_tables).

    Rows older than the configured age are written to apache parquet files in
    the archive storage and then deleted from the table. The config is the
    dict consumed by the task, in a serializable format:
    {
        'rows_per_file': 3000,
        'filter': {'fields': ['requested_at'], 'keep_for': 3, 'unit': 'months'},
        'ordering': ['-requested_at'],
        'schema': {'id': 'String', 'requested_at': 'Datetime', 'amount': 'Decimal(14,2)'}
    }
    """

    class TimeUnit(models.TextChoices):
        DAYS = 'days', _('Days')
        MONTHS = 'months', _('Months')
        YEARS = 'years', _('Years')

    created_at = models.DateTimeField(_('created at'), default=timezone.now, editable=False)
    updated_at = models.DateTimeField(_('updated at'), auto_now=True)
    content_type = models.OneToOneField(
        ContentType,
        on_delete=models.PROTECT,
        related_name='+',
        verbose_name=_('model'),
        help_text=_('Model whose old rows are moved to the archive.')
    )
    active = models.BooleanField(
        _('active'),
        default=True,
        help_text=_('Only active policies are run by the archive task.')
    )
    config = models.JSONField(
        _('configuration'),
        help_text=_('Dict consumed by the archive task: rows_per_file, filter, ordering and schema.')
    )
    indexed_at = models.DateTimeField(
        _('files indexed at'),
        null=True,
        blank=True,
        editable=False,
        help_text=_('Last run of the indexing of the archive files, which the admin needs to list all of them.')
    )

    class Meta:
        verbose_name = _('archive policy')
        verbose_name_plural = _('archive policies')

    def __str__(self):
        return f'{self.content_type.app_label}.{self.content_type.model}'

    def get_filter(self):
        """
        Rows where any of the date fields is before the cutoff. The cutoff is
        calculated when the archive runs.
        """
        row_filter = self.config['filter']
        if not row_filter['fields']:
            raise ValueError(f'Archive policy {self} has no date fields in the filter.')

        cutoff = timezone.now() - relativedelta(**{row_filter['unit']: row_filter['keep_for']})
        return reduce(operator.or_, (Q(**{f'{field}__lte': cutoff}) for field in row_filter['fields']))

    @classmethod
    def get_with_index_field(cls, model):
        """Policy of the model and its index field, or None for each of them."""
        policy = cls.objects.filter(content_type=ContentType.objects.get_for_model(model)).first()
        return policy, policy.get_index_field() if policy else None

    def get_index_field(self):
        """
        Field whose range the admin uses to pick the files of each page: the
        first of the ordering, when it is a date field of the model itself,
        not null and saved in the archive. The files are written sorted by it,
        so each one covers a short stretch of it.
        """
        ordering = self.config['ordering']
        if not ordering:
            return None

        name = ordering[0].removeprefix('-')
        try:
            field = self.content_type.model_class()._meta.get_field(name)
        except FieldDoesNotExist:
            return None
        if not isinstance(field, models.DateField) or field.null or field.attname not in self.config['schema']:
            return None
        return field.attname

    def get_schema_fields(self):
        """
        Schema fields in the order they are declared in the model. Postgres
        jsonb does not keep the key order, and this order defines the columns
        of the parquet files.
        """
        schema = self.config['schema']
        model_fields = [field.attname for field in self.content_type.model_class()._meta.concrete_fields]
        outside_model = [field for field in schema if field not in model_fields]
        return [field for field in model_fields if field in schema] + outside_model

    def get_archive_info(self):
        """Builds the dict in the format consumed by archive_tables."""
        schema = self.config['schema']
        return {
            'rows_per_file': self.config['rows_per_file'],
            'content_type': self.content_type,
            'index_field': self.get_index_field(),
            'model': self.content_type.model_class(),
            'filter': self.get_filter(),
            'ordering': self.config['ordering'],
            'schema': to_polars_schema({field: schema[field] for field in self.get_schema_fields()}),
            'json_columns': [field for field, type_name in schema.items() if type_name == 'Json'],
        }


class ArchiveFile(models.Model):
    """Parquet file of the archive, with its rows and the range of the policy's index field."""

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name='+', verbose_name=_('model'))
    path = models.CharField(_('path'), max_length=255, help_text=_('Path inside the model folder.'))
    rows = models.PositiveIntegerField(_('rows'))
    field = models.CharField(
        _('index field'),
        max_length=100,
        blank=True,
        help_text=_('Field of the first and last dates, empty when the policy has no index field.')
    )
    min_date = models.DateTimeField(_('first date'), null=True, blank=True)
    max_date = models.DateTimeField(_('last date'), null=True, blank=True)

    class Meta:
        verbose_name = _('archive file')
        verbose_name_plural = _('archive files')
        constraints = [
            models.UniqueConstraint(fields=('content_type', 'path'), name='parquet_archive_unique_file_path'),
        ]
        indexes = [
            models.Index(fields=('content_type', 'field', 'max_date'), name='parquet_archive_file_max_date'),
        ]

    def __str__(self):
        return self.path
