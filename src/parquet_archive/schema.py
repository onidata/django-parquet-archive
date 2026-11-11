import posixpath
import re
from datetime import datetime, time, timezone as dt_timezone
from uuid import uuid4

import polars as pl

from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.db import models
from django.db.models.constants import LOOKUP_SEP
from django.utils import timezone
from django.utils.translation import gettext as _


POLARS_TYPES = {
    'String': pl.String,
    'Int64': pl.Int64,
    'Float64': pl.Float64,
    'Boolean': pl.Boolean,
    'Date': pl.Date,
    'Datetime': pl.Datetime,
    'Duration': pl.Duration(time_unit='us'),
    # NOTE: JSON is written as text
    'Json': pl.String,
}

DECIMAL_TYPE_REGEX = re.compile(r'^Decimal\((?P<precision>\d+),\s*(?P<scale>\d+)\)$')

POLARS_TYPE_BY_FIELD = {
    'CharField': 'String',
    'TextField': 'String',
    'SlugField': 'String',
    'UUIDField': 'String',
    'GenericIPAddressField': 'String',
    'FileField': 'String',
    'AutoField': 'Int64',
    'BigAutoField': 'Int64',
    'SmallAutoField': 'Int64',
    'IntegerField': 'Int64',
    'BigIntegerField': 'Int64',
    'SmallIntegerField': 'Int64',
    'PositiveIntegerField': 'Int64',
    'PositiveBigIntegerField': 'Int64',
    'PositiveSmallIntegerField': 'Int64',
    'FloatField': 'Float64',
    'BooleanField': 'Boolean',
    'DateField': 'Date',
    'DateTimeField': 'Datetime',
    'DurationField': 'Duration',
    'JSONField': 'Json',
}

DATE_FIELD_TYPES = ('DateField', 'DateTimeField')

BLOCKING_ON_DELETE = (models.CASCADE, models.PROTECT, models.RESTRICT)


TIMESTAMPED_FILE_REGEX = re.compile(r'^\d{8}T\d{6}_')


def model_folder(model):
    """Folder of the model's parquet files in the storage: the class name in lowercase."""
    return model.__name__.lower()


def day_of(value):
    """Day of a DateField or DateTimeField value, in the project time zone (TIME_ZONE)."""
    if isinstance(value, datetime):
        # NOTE: The default time zone and not the one active in the request, otherwise the days of the date filter
        # would change with the user's time zone
        return timezone.localdate(value, timezone.get_default_timezone()) if timezone.is_aware(value) else value.date()
    return value


def start_of_day(day):
    """Midnight of the day in the project time zone, aware when USE_TZ is on."""
    midnight = datetime.combine(day, time.min)
    return timezone.make_aware(midnight, timezone.get_default_timezone()) if settings.USE_TZ else midnight


def index_value(value):
    """
    Value of the index field as saved in ArchiveFile, which only has
    datetimes: a date becomes its midnight in the project time zone.
    """
    if value is None or isinstance(value, datetime):
        return value
    return start_of_day(value)


def new_parquet_path(folder):
    """Path of a new parquet file, with the (UTC) timestamp at the start of the name."""
    return f'{folder}/{datetime.now(dt_timezone.utc):%Y%m%dT%H%M%S}_{uuid4()}.parquet'


def sort_newest_first(files):
    """
    Timestamped files from newest to oldest, followed by files without a
    timestamp in the name, by file name and regardless of the day subfolder.
    Reading uses the schema of the first file, so columns added to the policy
    later still show up in queries.
    """
    files = sorted(files, key=posixpath.basename)
    timestamped = [file for file in reversed(files) if TIMESTAMPED_FILE_REGEX.match(posixpath.basename(file))]
    others = [file for file in files if not TIMESTAMPED_FILE_REGEX.match(posixpath.basename(file))]
    return timestamped + others


def to_polars_schema(schema):
    """
    Converts the schema saved in the archive policy ({'field': 'String'}) to
    polars types. Decimals are saved as 'Decimal(precision,scale)'.
    """
    return {field: _to_polars_type(type_name) for field, type_name in schema.items()}


def _to_polars_type(type_name):
    decimal = DECIMAL_TYPE_REGEX.match(type_name)
    if decimal:
        return pl.Decimal(precision=int(decimal['precision']), scale=int(decimal['scale']))
    return POLARS_TYPES[type_name]


def polars_type_for_field(field):
    """
    Name of the polars type for a concrete model field, or None when it is not
    supported. ForeignKeys use the type of the target primary key and JSON
    becomes text. Other fields with from_db_value (encrypted fields, custom
    value objects...) are not supported because values() returns Python
    objects polars cannot write (and encrypted values would be archived
    decrypted).
    """
    if field.is_relation:
        return polars_type_for_field(field.target_field)

    internal_type = field.get_internal_type()
    if internal_type == 'JSONField':
        return 'Json'
    if hasattr(field, 'from_db_value'):
        return None
    if internal_type == 'DecimalField':
        return f'Decimal({field.max_digits},{field.decimal_places})'
    return POLARS_TYPE_BY_FIELD.get(internal_type)


def date_field_paths(model):
    """
    Date fields of the model and of its direct FKs (e.g. order__cancelled_at),
    as (path, description) pairs for the archive filter choices.
    """
    paths = []
    for field in model._meta.concrete_fields:
        if field.is_relation:
            paths += [
                (f'{field.name}{LOOKUP_SEP}{related.name}', f'{field.verbose_name} → {related.verbose_name}')
                for related in field.related_model._meta.concrete_fields
                if not related.is_relation and related.get_internal_type() in DATE_FIELD_TYPES
            ]
        elif field.get_internal_type() in DATE_FIELD_TYPES:
            paths.append((field.name, field.verbose_name))
    return paths


def relations_blocking_delete(model):
    """
    Relations that make it unsafe to delete the model's rows after archiving them:
    - FKs from other models with CASCADE: would delete rows that are not archived;
    - FKs with PROTECT/RESTRICT: the delete would fail after the parquet files were written;
    - ManyToMany and GenericRelation: the related rows would be deleted along with them.
    """
    relations = []
    for field in model._meta.get_fields(include_hidden=True):
        if field.many_to_many or isinstance(field, GenericRelation):
            relations.append(
                _('%(model)s (relation %(field)s)') % {'model': field.related_model._meta.label, 'field': field.name}
            )
        elif field.auto_created and not field.concrete:
            # NOTE: FKs of ManyToMany through tables are already listed as a relation above
            if field.related_model._meta.auto_created:
                continue
            # NOTE: django-polymorphic wraps on_delete in a PolymorphicGuard
            on_delete = getattr(field.on_delete, 'action', field.on_delete)
            if on_delete in BLOCKING_ON_DELETE:
                relations.append(f'{field.related_model._meta.label}.{field.field.name} ({on_delete.__name__})')
    return relations
