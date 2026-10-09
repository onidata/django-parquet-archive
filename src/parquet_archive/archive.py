import json
import tempfile
from uuid import UUID

import polars as pl

from django.core.files import File
from django.core.serializers.json import DjangoJSONEncoder

from parquet_archive.conf import get_storage
from parquet_archive.models import ArchivePolicy
from parquet_archive.schema import day_folder, model_folder, new_parquet_path


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
        storage.save(new_parquet_path(folder), File(temporary_file))


def _to_parquet_value(value, is_json):
    if isinstance(value, UUID):
        return str(value)
    if is_json and value is not None:
        return json.dumps(value, ensure_ascii=False, cls=DjangoJSONEncoder)
    return value


def _write_and_delete(storage, folder, info, columns, pks):
    _write_parquet(storage, folder, info, columns)
    # NOTE: Deletes exactly what was just written by the pks (and not by the filter): a failure halfway
    # does not duplicate the archive on the next run, and nothing that started to match the filter during
    # the run is deleted without being in a file
    info['model'].objects.filter(pk__in=pks).delete()


def archive_tables(model_names=None):
    """
    Writes the rows selected by each active policy to parquet files and
    deletes the rows of each file from the table right after writing it.
    Each file has up to rows_per_file rows and goes into the model folder in
    the storage. With partition_field, the rows of each file share the same
    day of that field and the file goes into the day subfolder.
    """
    storage = get_storage()
    for info in get_archive_infos(model_names):
        model = info['model']
        folder = model_folder(model)
        partition_field = info['partition_field']
        column_names = list(info['schema'])
        json_columns = set(info['json_columns'])
        pk = model._meta.pk.attname
        query_fields = column_names if pk in column_names else [*column_names, pk]

        # NOTE: Rows of the same day must come in sequence so the day is not spread across small files
        ordering = info['ordering']
        if partition_field and (not ordering or ordering[0].removeprefix('-') != partition_field):
            ordering = [f'-{partition_field}', *ordering]

        rows = model.objects.filter(
            info['filter']
        ).order_by(*ordering)

        columns, pks, file_folder = {column: [] for column in column_names}, [], None
        for row in rows.values(*query_fields).iterator(chunk_size=512):
            row_folder = day_folder(folder, partition_field, row[partition_field]) if partition_field else folder
            if pks and row_folder != file_folder:
                _write_and_delete(storage, file_folder, info, columns, pks)
                columns, pks = {column: [] for column in column_names}, []
            file_folder = row_folder

            for column in column_names:
                columns[column].append(_to_parquet_value(row[column], column in json_columns))
            pks.append(row[pk])

            if len(pks) == info['rows_per_file']:
                _write_and_delete(storage, file_folder, info, columns, pks)
                columns, pks = {column: [] for column in column_names}, []

        if pks:
            # NOTE: The loop ended without writing the last rows
            _write_and_delete(storage, file_folder, info, columns, pks)
