import json
import operator
import posixpath
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
from functools import reduce

import polars as pl

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.db.models.constants import LOOKUP_SEP
from django.utils import timezone
from django.utils.text import smart_split, unescape_string_literal

from parquet_archive.conf import get_storage
from parquet_archive.schema import (
    index_value, model_folder, polars_type_for_field, sort_newest_first, to_polars_schema
)

try:
    from storages.backends.s3boto3 import S3Boto3Storage
except ImportError:  # NOTE: django-storages is optional (the "s3" extra)
    S3Boto3Storage = None


def _exact(column, value):
    return column.is_null() if value is None else column == value


def _isnull(column, value):
    return column.is_null() if value else column.is_not_null()


def _contains(column, value):
    return column.str.contains(value, literal=True)


def _startswith(column, value):
    return column.str.starts_with(value)


def _endswith(column, value):
    return column.str.ends_with(value)


OPERATORS = {
    'exact': _exact,
    'gt': operator.gt,
    'gte': operator.ge,
    'lt': operator.lt,
    'lte': operator.le,
    'in': pl.Expr.is_in,
    'isnull': _isnull,
}

# NOTE: (function, ignore_case) pairs: the "i" lookups compare the column and the value in lowercase
TEXT_OPERATORS = {
    'iexact': (operator.eq, True),
    'contains': (_contains, False),
    'icontains': (_contains, True),
    'startswith': (_startswith, False),
    'istartswith': (_startswith, True),
    'endswith': (_endswith, False),
    'iendswith': (_endswith, True),
}

# NOTE: Same search_fields prefixes as ModelAdmin.get_search_results ('@' becomes icontains)
SEARCH_PREFIXES = {'^': 'istartswith', '=': 'iexact', '@': 'icontains'}

# NOTE: Source file of each row, so pagination rereads only the files of the page
FILE_COLUMN = '_parquet_archive_file'


class UnsupportedLookup(Exception):
    """Admin lookup that cannot be applied to the archive columns."""


class ArchiveFolder:
    """Archive parquet files of a model: those in its folder and in its subfolders (daily folders of version 0.1)."""

    def __init__(self, model, schema=None):
        self.model = model
        self.storage = get_storage()
        self.folder = model_folder(model)
        self.is_s3 = S3Boto3Storage is not None and isinstance(self.storage, S3Boto3Storage)
        # NOTE: Columns of every read: files without some of them get them empty, instead of following the first file
        self.schema = schema

    def paths(self):
        """Parquet files of the folder and of its subfolders, from newest to oldest."""
        # NOTE: On S3 the folder does not really exist (exists is always false) and listdir of an empty prefix does
        # not fail
        if not self.is_s3 and not self.storage.exists(self.folder):
            return []

        subfolders, files = self.storage.listdir(self.folder)
        paths = list(files)
        for subfolder in subfolders:
            _, files = self.storage.listdir(f'{self.folder}/{subfolder}')
            paths += [f'{subfolder}/{file}' for file in files]
        return sort_newest_first(path for path in paths if path.endswith('.parquet'))

    def sources(self, paths):
        """What read() takes for each path: on S3 the URL in the bucket, otherwise the path itself."""
        if not self.is_s3:
            return list(paths)
        return [
            f's3://{self.storage.bucket_name}/{posixpath.join(self.storage.location, self.folder, path)}'
            for path in paths
        ]

    def load(self):
        """LazyFrame with every file of the folder."""
        return self.read(self.sources(self.paths()))

    def read_schema(self, path):
        """Columns of one file, read from its footer."""
        source = self.sources([path])[0]
        if self.is_s3:
            return pl.scan_parquet(source, storage_options=_s3_storage_options(self.storage)).collect_schema()
        with self.storage.open(f'{self.folder}/{source}', 'rb') as parquet_file:
            return pl.read_parquet_schema(parquet_file)

    def read(self, sources):
        """
        LazyFrame of the files, with the source of each row in FILE_COLUMN. On
        S3 polars reads straight from the bucket (the source is the URL), only
        the parts the query needs. Other storages (e.g. in tests) open the
        files through the Django storage.
        """
        if not sources:
            return pl.LazyFrame(schema={**(self.schema or _model_schema(self.model)), FILE_COLUMN: pl.String})
        if self.is_s3:
            return pl.scan_parquet(
                sources,
                storage_options=_s3_storage_options(self.storage),
                schema=self.schema,
                missing_columns='insert',
                extra_columns='ignore',
                include_file_paths=FILE_COLUMN,
            )

        frames = []
        for source in sources:
            with self.storage.open(f'{self.folder}/{source}', 'rb') as parquet_file:
                frame = pl.read_parquet(parquet_file)
            if self.schema is not None:
                frame = frame.select(
                    pl.col(name) if name in frame.columns else pl.lit(None, dtype).alias(name)
                    for name, dtype in self.schema.items()
                )
            frames.append(frame.with_columns(pl.lit(source).alias(FILE_COLUMN)))
        return pl.concat(frames, how='diagonal_relaxed').lazy()


RANGE_LOOKUPS = ('', 'exact', 'gt', 'gte', 'lt', 'lte')


def index_range(value):
    """
    Start (inclusive) and end (exclusive) of a date or datetime value, as
    saved in ArchiveFile: a whole day for a date, a microsecond for a datetime.
    """
    if isinstance(value, datetime):
        low = timezone.make_aware(value) if settings.USE_TZ and timezone.is_naive(value) else value
        return low, low + timedelta(microseconds=1)
    return index_value(value), index_value(value + timedelta(days=1))


def period_bounds(field, lookups):
    """
    Start (inclusive) and end (exclusive) of the period that the admin
    lookups on the date field let through, as saved in ArchiveFile (see
    index_value), or None for a bound they do not restrict. Several values
    become the period that covers all of them.
    """
    start = end = None
    for lookup, values in lookups.items():
        name, _, lookup_type = lookup.partition(LOOKUP_SEP)
        if name != field.attname or lookup_type not in RANGE_LOOKUPS:
            continue

        for value in values if isinstance(values, (list, tuple)) else [values]:
            value = field.to_python(value)
            if value is None:
                continue

            low, high = index_range(value)
            if lookup_type in ('', 'exact', 'gte', 'gt'):
                bound = high if lookup_type == 'gt' else low
                start = bound if start is None else min(start, bound)
            if lookup_type in ('', 'exact', 'lt', 'lte'):
                bound = low if lookup_type == 'lt' else high
                end = bound if end is None else max(end, bound)
    return start, end


@dataclass
class FileGroup:
    """Archive files whose ranges of the index field overlap."""

    files: list
    rows: int
    min_date: datetime
    max_date: datetime


def file_groups(files):
    """
    Groups of the files (ArchiveFile rows, from the newest max_date), from
    newest to oldest. Groups do not overlap, so the order of the rows follows
    the order of the groups and a page only reads the groups it shows.
    """
    groups = []
    for file in files:
        if groups and file.max_date >= groups[-1].min_date:
            group = groups[-1]
            group.files.append(file)
            group.rows += file.rows
            group.min_date = min(group.min_date, file.min_date)
        else:
            groups.append(FileGroup([file], file.rows, file.min_date, file.max_date))
    return groups


def _model_schema(model):
    return to_polars_schema({
        field.attname: polars_type_for_field(field) or 'String' for field in model._meta.concrete_fields
    })


def _s3_storage_options(storage):
    options = {
        'aws_access_key_id': storage.access_key,
        'aws_secret_access_key': storage.secret_key,
        'aws_session_token': storage.security_token,
        'aws_region': storage.connection.meta.client.meta.region_name,
        'aws_endpoint_url': storage.endpoint_url,
    }
    if storage.endpoint_url and storage.endpoint_url.startswith('http://'):
        options['aws_allow_http'] = 'true'
    return {key: value for key, value in options.items() if value}


class ArchiveQuery:
    """
    Lazy (polars) query over the archive parquet files of a model.

    Translates the admin lookups, search and ordering into polars expressions
    and implements the minimal QuerySet interface used by ChangeList and
    Paginator: count(), slicing and iteration returning (unsaved) model
    instances. Without a frame, it reads every file of the model folder.
    """

    # NOTE: Avoids the Paginator warning about unordered pagination
    ordered = True

    def __init__(self, model, folder=None, frame=None, columns=None):
        self.model = model
        self.folder = ArchiveFolder(model) if folder is None else folder
        self.frame = self.folder.load() if frame is None else frame
        self.columns = self.frame.collect_schema() if columns is None else columns
        self._count = None

    def _new(self, frame):
        return ArchiveQuery(self.model, self.folder, frame, self.columns)

    def _resolve_lookup(self, lookup):
        """
        Resolves an admin lookup (e.g. 'customer__id__exact') to the parquet
        column, the model field and the lookup type.
        """
        parts = lookup.split(LOOKUP_SEP)
        lookup_type = 'exact'
        if len(parts) > 1 and (parts[-1] in OPERATORS or parts[-1] in TEXT_OPERATORS):
            lookup_type = parts.pop()

        field_name = parts.pop(0)
        try:
            field = self.model._meta.pk if field_name == 'pk' else self.model._meta.get_field(field_name)
        except FieldDoesNotExist:
            raise UnsupportedLookup(lookup)

        # NOTE: FKs are saved by attname (e.g. customer_id), so 'customer__id' is the column itself
        is_fk = field.is_relation and getattr(field, 'concrete', False)
        if is_fk and parts and parts[0] in (field.target_field.name, 'pk'):
            parts.pop(0)

        if parts or not getattr(field, 'concrete', False) or field.attname not in self.columns:
            raise UnsupportedLookup(lookup)
        return field.attname, field, lookup_type

    def _to_column_value(self, column, field, value):
        value = field.to_python(value)
        if value is None:
            return None

        column_type = self.columns[column]
        if column_type == pl.String:
            return str(value)
        if isinstance(column_type, pl.Date) and isinstance(value, datetime):
            return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
        if isinstance(column_type, pl.Datetime) and isinstance(value, date):
            if not isinstance(value, datetime):
                value = datetime(value.year, value.month, value.day)
            if column_type.time_zone is None and timezone.is_aware(value):
                return value.astimezone(dt_timezone.utc).replace(tzinfo=None)
            if column_type.time_zone is not None and timezone.is_naive(value):
                return timezone.make_aware(value)
        return value

    def _condition(self, column, field, lookup_type, value):
        if lookup_type in TEXT_OPERATORS:
            function, ignore_case = TEXT_OPERATORS[lookup_type]
            expression, value = pl.col(column).cast(pl.String), str(value)
            if ignore_case:
                expression, value = expression.str.to_lowercase(), value.lower()
            return function(expression, value)
        if lookup_type == 'isnull':
            return OPERATORS[lookup_type](pl.col(column), value)
        if lookup_type == 'in':
            value = [self._to_column_value(column, field, item) for item in value]
        else:
            value = self._to_column_value(column, field, value)
        return OPERATORS[lookup_type](pl.col(column), value)

    def apply_lookups(self, params):
        """
        Applies lookups in the admin format: {lookup: [values]}. Values of the
        same lookup are combined with OR and the lookups with each other with AND.
        """
        conditions = []
        for lookup, values in params.items():
            if not isinstance(values, (list, tuple)):
                values = [values]
            if not values:
                continue

            column, field, lookup_type = self._resolve_lookup(lookup)
            conditions.append(reduce(operator.or_, (
                self._condition(column, field, lookup_type, value) for value in values
            )))

        if not conditions:
            return self
        return self._new(self.frame.filter(*conditions))

    def search(self, search_fields, term):
        """
        Search in the same format as ModelAdmin.get_search_results. Returns the
        query and the search fields ignored because they are not in the archive.
        """
        if not search_fields or not term:
            return self, []

        lookups, ignored = [], []
        for search_field in search_fields:
            lookup_type = SEARCH_PREFIXES.get(search_field[0], 'icontains')
            lookup = search_field[1:] if search_field[0] in SEARCH_PREFIXES else search_field
            if lookup.split(LOOKUP_SEP)[-1] not in {**OPERATORS, **TEXT_OPERATORS}:
                lookup = f'{lookup}{LOOKUP_SEP}{lookup_type}'

            try:
                lookups.append(self._resolve_lookup(lookup))
            except UnsupportedLookup:
                ignored.append(search_field)

        if not lookups:
            return self._new(self.frame.filter(pl.lit(False))), ignored

        conditions = []
        for bit in smart_split(term):
            if bit.startswith(('"', "'")) and bit[0] == bit[-1]:
                bit = unescape_string_literal(bit)
            conditions.append(reduce(operator.or_, (
                self._condition(column, field, lookup_type, bit) for column, field, lookup_type in lookups
            )))
        return self._new(self.frame.filter(*conditions)), ignored

    def sort(self, ordering):
        """Sorts by the fields that exist in the archive, ignoring expressions and relations."""
        columns, descending = [], []
        for item in ordering:
            if not isinstance(item, str):
                continue
            try:
                column, _, _ = self._resolve_lookup(item.removeprefix('-'))
            except UnsupportedLookup:
                continue

            if column not in columns:
                columns.append(column)
                descending.append(item.startswith('-'))

        if not columns:
            return self
        # NOTE: Same as postgres: nulls last in ascending order and first in descending order
        return self._new(self.frame.sort(columns, descending=descending, nulls_last=[not d for d in descending]))

    @staticmethod
    def _from_parquet_value(field, value):
        if value is not None and field.get_internal_type() == 'JSONField':
            return json.loads(value)
        return field.to_python(value)

    def _instances(self, data_frame):
        # NOTE: Fields outside the archive stay None, otherwise the model would fill in the default (e.g. timezone.now)
        fields = self.model._meta.concrete_fields
        return [
            self.model(**{
                field.attname: self._from_parquet_value(field, row[field.attname]) if field.attname in row else None
                for field in fields
            })
            for row in data_frame.iter_rows(named=True)
        ]

    def count(self):
        if self._count is None:
            self._count = self.frame.select(pl.len()).collect(engine='streaming').item()
        return self._count

    def __len__(self):
        return self.count()

    def __iter__(self):
        return iter(self._instances(self.frame.collect(engine='streaming')))

    def __getitem__(self, index):
        if not isinstance(index, slice):
            return self[index:index + 1][0]

        start = index.start or 0
        length = None if index.stop is None else index.stop - start
        pk = self.model._meta.pk.attname
        if pk not in self.columns or FILE_COLUMN not in self.columns:
            return self._instances(self.frame.slice(start, length).collect(engine='streaming'))

        # NOTE: Sorting with every column would load the whole period into memory, so the page is picked
        # with only the pk and the file of each row, and the full rows come only from the page's files
        keys = self.frame.select(pk, FILE_COLUMN).slice(start, length).collect(engine='streaming')
        if keys.is_empty():
            return []
        # NOTE: Reading uses the schema of the first file, so the newest comes first, as in the first step
        sources = sort_newest_first(keys[FILE_COLUMN].unique())
        rows = self.folder.read(sources).filter(pl.col(pk).is_in(keys[pk].implode())).collect(engine='streaming')
        return self._instances(keys.join(rows, on=[pk, FILE_COLUMN], how='left', maintain_order='left'))

    def _clone(self):
        # NOTE: ChangeList uses _clone() to show every result at once
        return list(self)


class IndexedQuery:
    """Archive rows sorted by the index field, read only from the groups of files (file_groups) of each page."""

    ordered = True

    def __init__(self, model, folder, files, period=(None, None), lookups=None, ordering=()):
        """
        Counts the groups inside the period by the rows saved in the index
        and reads only those on its edges. The lookups must be on the index
        field alone and the ordering must start with it. Implements the same
        minimal QuerySet interface as ArchiveQuery.
        """
        self.model = model
        self.folder = folder
        self.period = period
        self.lookups = lookups or {}
        self.ordering = list(ordering)
        self.columns = {**(folder.schema or {}), FILE_COLUMN: pl.String}
        groups = file_groups(files)
        self.groups = groups if not ordering or ordering[0].startswith('-') else groups[::-1]
        self._counts = None

    def _query(self, group):
        sources = sort_newest_first(self.folder.sources(file.path for file in group.files))
        query = ArchiveQuery(self.model, self.folder, self.folder.read(sources), self.columns)
        return query.apply_lookups(self.lookups).sort(self.ordering)

    def _is_inside_the_period(self, group):
        start, end = self.period
        return (start is None or group.min_date >= start) and (end is None or group.max_date < end)

    def _group_counts(self):
        if self._counts is None:
            self._counts = [
                group.rows if self._is_inside_the_period(group) else self._query(group).count()
                for group in self.groups
            ]
        return self._counts

    def count(self):
        return sum(self._group_counts())

    def __len__(self):
        return self.count()

    def __iter__(self):
        return iter(self[0:None])

    def __getitem__(self, index):
        if not isinstance(index, slice):
            return self[index:index + 1][0]

        start = index.start or 0
        stop = self.count() if index.stop is None else index.stop
        rows, offset = [], 0
        for group, count in zip(self.groups, self._group_counts()):
            if offset >= stop:
                break
            if offset + count > start:
                rows += self._query(group)[max(start - offset, 0):min(stop, offset + count) - offset]
            offset += count
        return rows

    def _clone(self):
        # NOTE: ChangeList uses _clone() to show every result at once
        return list(self)
