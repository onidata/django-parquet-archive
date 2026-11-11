import json
import logging
import tempfile
from datetime import datetime, timezone as dt_timezone
from uuid import UUID

import polars as pl

from django.conf import settings
from django.core.files import File
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from parquet_archive.conf import get_storage
from parquet_archive.models import ArchiveFile, ArchivePolicy
from parquet_archive.query import FILE_COLUMN, READ_ERRORS, ArchiveFolder
from parquet_archive.schema import index_value, model_folder, new_parquet_path

logger = logging.getLogger(__name__)

INDEX_BATCH_SIZE = 100


def get_archive_infos(model_names=None):
    """
    Active policies in the format consumed by the archive. model_names limits
    them by the lowercase model name (ContentType.model), e.g. ['apirequest'].
    """
    policies = ArchivePolicy.objects.filter(active=True).select_related('content_type')
    if model_names:
        policies = policies.filter(content_type__model__in=model_names)
    return [policy.get_archive_info() for policy in policies]


def _write_parquet(storage, folder, info, columns):
    data_frame = pl.DataFrame(columns, schema=info['schema'])
    # NOTE: The file from storage.open('wb') does not follow the write interface polars expects
    # in every storage, while save() is implemented by all of them (on S3 the upload is streamed)
    with tempfile.TemporaryFile() as temporary_file:
        data_frame.write_parquet(temporary_file)
        temporary_file.seek(0)
        return storage.save(new_parquet_path(folder), File(temporary_file))


def _to_parquet_value(value, is_json):
    if isinstance(value, UUID):
        return str(value)
    if is_json and value is not None:
        return json.dumps(value, ensure_ascii=False, cls=DjangoJSONEncoder)
    return value


def _write_and_delete(storage, folder, info, columns, pks):
    name = _write_parquet(storage, folder, info, columns)
    dates = [value for value in columns[info['index_field']] if value is not None] if info['index_field'] else []
    ArchiveFile.objects.update_or_create(
        content_type=info['content_type'],
        path=name.removeprefix(f'{folder}/'),
        defaults={
            'rows': len(pks),
            'field': info['index_field'] or '',
            'min_date': index_value(min(dates)) if dates else None,
            'max_date': index_value(max(dates)) if dates else None,
        },
    )
    # NOTE: Deletes exactly what was just written by the pks (and not by the filter): a failure halfway
    # does not duplicate the archive on the next run, and nothing that started to match the filter during
    # the run is deleted without being in a file
    info['model'].objects.filter(pk__in=pks).delete()


def archive_tables(model_names=None):
    """
    Writes the rows selected by each active policy to parquet files and
    deletes the rows of each file from the table right after writing it.
    Each file has up to rows_per_file rows, goes into the model folder in the
    storage and is registered in ArchiveFile.
    """
    storage = get_storage()
    for info in get_archive_infos(model_names):
        model = info['model']
        folder = model_folder(model)
        column_names = list(info['schema'])
        json_columns = set(info['json_columns'])
        pk = model._meta.pk.attname
        query_fields = column_names if pk in column_names else [*column_names, pk]

        rows = model.objects.filter(
            info['filter']
        ).order_by(*info['ordering'])

        columns, pks = {column: [] for column in column_names}, []
        for row in rows.values(*query_fields).iterator(chunk_size=512):
            for column in column_names:
                columns[column].append(_to_parquet_value(row[column], column in json_columns))
            pks.append(row[pk])

            if len(pks) == info['rows_per_file']:
                _write_and_delete(storage, folder, info, columns, pks)
                columns, pks = {column: [] for column in column_names}, []

        if pks:
            # NOTE: The loop ended without writing the last rows
            _write_and_delete(storage, folder, info, columns, pks)


def index_files(model_names=None):
    """
    Registers in ArchiveFile the archive files that are not there yet (e.g.
    written before the index existed) and removes those that are no longer in
    the storage. Files indexed by another field (e.g. before the ordering
    changed) are read again.
    """
    policies = ArchivePolicy.objects.select_related('content_type')
    if model_names:
        policies = policies.filter(content_type__model__in=model_names)
    for policy in policies:
        _index_policy_files(policy)


def _index_policy_files(policy):
    model = policy.content_type.model_class()
    index_field = policy.get_index_field()
    files = ArchiveFile.objects.filter(content_type=policy.content_type)
    # NOTE: The index is read before listing the storage, so a file that the archive registers in between is not
    # taken for removed
    indexed = dict(files.values_list('path', 'field'))
    folder = ArchiveFolder(model)
    paths = folder.paths()

    removed = sorted(set(indexed) - set(paths))
    for start in range(0, len(removed), INDEX_BATCH_SIZE):
        files.filter(path__in=removed[start:start + INDEX_BATCH_SIZE]).delete()

    missing = [path for path in paths if indexed.get(path) != (index_field or '')]
    for start in range(0, len(missing), INDEX_BATCH_SIZE):
        batch = missing[start:start + INDEX_BATCH_SIZE]
        archive_files = _read_archive_files(folder, policy.content_type, index_field, batch)
        with transaction.atomic():
            files.filter(path__in=batch).delete()
            ArchiveFile.objects.bulk_create(archive_files, ignore_conflicts=True)

    policy.indexed_at = timezone.now()
    policy.save(update_fields=['indexed_at'])


def _read_archive_files(folder, content_type, index_field, paths):
    """ArchiveFile of each path with its rows and range, skipping (with a log) the files that cannot be read."""
    try:
        return _read_file_stats(folder, content_type, index_field, paths)
    except READ_ERRORS:
        if len(paths) > 1:
            # NOTE: One bad file fails the whole batch, so the files are read one by one to skip only that one
            return [
                archive_file
                for path in paths
                for archive_file in _read_archive_files(folder, content_type, index_field, [path])
            ]
        logger.exception('Archive file %s/%s could not be indexed.', folder.folder, paths[0])
        return []


def _read_file_stats(folder, content_type, index_field, paths):
    sources = folder.sources(paths)
    path_of = dict(zip(sources, paths))
    frame = folder.read(sources)
    aggregations = [pl.len().alias('rows')]
    # NOTE: The batch has the columns of its newest file, and older files without the index field get no range (the
    # admin warns that they are not listed)
    if index_field and index_field in frame.collect_schema():
        aggregations += [pl.col(index_field).min().alias('min_date'), pl.col(index_field).max().alias('max_date')]

    stats = frame.group_by(FILE_COLUMN).agg(aggregations).collect()
    return [
        ArchiveFile(
            content_type=content_type,
            path=path_of[row[FILE_COLUMN]],
            rows=row['rows'],
            field=index_field or '',
            min_date=_stats_value(row.get('min_date')),
            max_date=_stats_value(row.get('max_date')),
        )
        for row in stats.iter_rows(named=True)
    ]


def _stats_value(value):
    if settings.USE_TZ and isinstance(value, datetime) and timezone.is_naive(value):
        # NOTE: Like ArchiveQuery, a datetime column without time zone holds UTC
        value = value.replace(tzinfo=dt_timezone.utc)
    return index_value(value)
