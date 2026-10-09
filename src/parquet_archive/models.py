import operator
from functools import reduce

from dateutil.relativedelta import relativedelta

from django.contrib.contenttypes.models import ContentType
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
        'partition_field': 'requested_at',
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
    def get_partition_field(cls, model):
        """partition_field of the model's policy, or None without a policy or without day subfolders."""
        policy = cls.objects.filter(content_type=ContentType.objects.get_for_model(model)).first()
        if policy is None:
            return None
        return policy.config.get('partition_field')

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
        partition_field = self.config.get('partition_field')
        if partition_field and partition_field not in schema:
            # NOTE: The admin filters the rows of the period and builds the detail link from the partition column
            raise ValueError(f'Archive policy {self} has the partition field ({partition_field}) outside the schema.')
        return {
            'rows_per_file': self.config['rows_per_file'],
            'partition_field': partition_field,
            'model': self.content_type.model_class(),
            'filter': self.get_filter(),
            'ordering': self.config['ordering'],
            'schema': to_polars_schema({field: schema[field] for field in self.get_schema_fields()}),
            'json_columns': [field for field, type_name in schema.items() if type_name == 'Json'],
        }
