import io
import json
import posixpath
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

import polars as pl

from dateutil.relativedelta import relativedelta

from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from parquet_archive import archive
from parquet_archive.archive import archive_tables, index_files
from parquet_archive.conf import get_storage
from parquet_archive.models import ArchiveFile, ArchivePolicy
from parquet_archive.query import ArchiveFolder
from parquet_archive.schema import TIMESTAMPED_FILE_REGEX
from parquet_archive.tasks import archive_tables_task, index_files_task
from tests.base import API_REQUEST_CONFIG, ArchiveStorageMixin, create_policy
from tests.example.models import ApiRequest, Customer


class ArchiveTablesTest(ArchiveStorageMixin, TestCase):

    def setUp(self):
        super().setUp()
        create_policy()
        now = timezone.now()
        self.customer = Customer.objects.create(name='Alice')
        self.old_requests = [
            ApiRequest.objects.create(
                requested_at=now - relativedelta(months=months), method='post', amount=Decimal('10.50'),
                customer=self.customer,
            )
            for months in (4, 5, 6)
        ]
        self.recent = ApiRequest.objects.create(requested_at=now - relativedelta(months=3) + relativedelta(hours=1))

    def _read_parquets(self):
        storage = get_storage()
        files = ArchiveFolder(ApiRequest).paths()
        frames = []
        for file in files:
            with storage.open(f'apirequest/{file}', 'rb') as parquet_file:
                frames.append(pl.read_parquet(parquet_file))
        return files, pl.concat(frames)

    def test_writes_parquets_and_deletes_old_rows(self):
        archive_tables()

        files, data_frame = self._read_parquets()
        self.assertEqual([posixpath.dirname(file) for file in files], ['', ''])
        for file in files:
            self.assertRegex(posixpath.basename(file), TIMESTAMPED_FILE_REGEX)
        self.assertEqual(sorted(data_frame['id'].to_list()), sorted(str(request.id) for request in self.old_requests))
        self.assertEqual(data_frame['amount'].to_list(), [Decimal('10.50')] * 3)
        self.assertEqual(data_frame['customer_id'].to_list(), [self.customer.id] * 3)
        self.assertEqual(list(ApiRequest.objects.values_list('id', flat=True)), [self.recent.id])

    def test_writes_json_as_text_and_duration(self):
        ArchivePolicy.objects.update(config={
            **API_REQUEST_CONFIG,
            'schema': {**API_REQUEST_CONFIG['schema'], 'headers': 'Json', 'duration': 'Duration'},
        })
        ApiRequest.objects.filter(id=self.old_requests[0].id).update(
            headers={'Authorization': 'Bearer abc', 'attempts': 2}, duration=timedelta(seconds=1, milliseconds=500)
        )

        archive_tables()

        _, data_frame = self._read_parquets()
        row = data_frame.filter(pl.col('id') == str(self.old_requests[0].id)).row(0, named=True)
        self.assertEqual(data_frame.schema['duration'], pl.Duration(time_unit='us'))
        self.assertEqual(json.loads(row['headers']), {'Authorization': 'Bearer abc', 'attempts': 2})
        self.assertEqual(row['duration'], timedelta(seconds=1, milliseconds=500))

    def test_failure_halfway_does_not_duplicate_the_archive(self):
        write_parquet = archive._write_parquet
        calls = []

        def fail_on_second_file(*args):
            calls.append(args)
            if len(calls) == 2:
                raise ConnectionError('S3 unavailable')
            return write_parquet(*args)

        with patch('parquet_archive.archive._write_parquet', side_effect=fail_on_second_file), \
                self.assertRaises(ConnectionError):
            archive_tables()

        # NOTE: The rows of the first file were already deleted, those of the second are still in the database
        self.assertEqual(ApiRequest.objects.count(), 2)

        archive_tables()

        files, data_frame = self._read_parquets()
        self.assertEqual(len(files), 2)
        self.assertEqual(sorted(ArchiveFile.objects.values_list('path', flat=True)), sorted(files))
        self.assertEqual(sorted(data_frame['id'].to_list()), sorted(str(request.id) for request in self.old_requests))
        self.assertEqual(list(ApiRequest.objects.values_list('id', flat=True)), [self.recent.id])

    def test_does_not_delete_row_that_starts_matching_the_filter_during_the_run(self):
        write_parquet = archive._write_parquet
        created_during = []

        def create_row_during_the_run(*args):
            if not created_during:
                created_during.append(ApiRequest.objects.create(requested_at=timezone.now() - relativedelta(years=1)))
            return write_parquet(*args)

        with patch('parquet_archive.archive._write_parquet', side_effect=create_row_during_the_run):
            archive_tables()

        _, data_frame = self._read_parquets()
        self.assertNotIn(str(created_during[0].id), data_frame['id'].to_list())
        self.assertTrue(ApiRequest.objects.filter(id=created_during[0].id).exists())

    def test_deletes_one_batch_per_file(self):
        with CaptureQueriesContext(connection) as queries:
            archive_tables()

        deletes = [
            query for query in queries.captured_queries if query['sql'].startswith('DELETE FROM "example_apirequest"')
        ]
        self.assertEqual(len(deletes), 2)

    def test_registers_each_file_with_its_rows_and_the_range_of_the_index_field(self):
        archive_tables()

        files, _ = self._read_parquets()
        newest, oldest = (request.requested_at for request in (self.old_requests[0], self.old_requests[2]))
        registered = ArchiveFile.objects.order_by('-max_date').values_list(
            'path', 'rows', 'field', 'min_date', 'max_date'
        )
        self.assertEqual(sorted(path for path, *_ in registered), sorted(files))
        self.assertEqual([values for _, *values in registered], [
            [2, 'requested_at', self.old_requests[1].requested_at, newest],
            [1, 'requested_at', oldest, oldest],
        ])

    def test_without_index_field_registers_the_file_without_range(self):
        ArchivePolicy.objects.update(config={**API_REQUEST_CONFIG, 'ordering': ['status_code']})

        archive_tables()

        self.assertEqual(
            sorted(ArchiveFile.objects.values_list('rows', 'field', 'min_date', 'max_date')),
            [(1, '', None, None), (2, '', None, None)]
        )

    def test_deletes_by_primary_key_even_without_it_in_the_columns(self):
        schema = {field: type_name for field, type_name in API_REQUEST_CONFIG['schema'].items() if field != 'id'}
        ArchivePolicy.objects.update(config={**API_REQUEST_CONFIG, 'schema': schema})

        archive_tables()

        _, data_frame = self._read_parquets()
        self.assertNotIn('id', data_frame.columns)
        self.assertEqual(data_frame.height, 3)
        self.assertEqual(list(ApiRequest.objects.values_list('id', flat=True)), [self.recent.id])

    def test_ignores_inactive_policy(self):
        ArchivePolicy.objects.update(active=False)

        archive_tables()

        self.assertEqual(ApiRequest.objects.count(), 4)

    def test_runs_only_the_given_tables(self):
        archive_tables(model_names=['other'])
        self.assertEqual(ApiRequest.objects.count(), 4)

        archive_tables(model_names=['apirequest'])
        self.assertEqual(ApiRequest.objects.count(), 1)

    def test_task_runs_the_archive(self):
        archive_tables_task.apply(kwargs={'model_names': ['apirequest']})

        self.assertEqual(ApiRequest.objects.count(), 1)
        self.assertEqual(archive_tables_task.name, 'parquet_archive.archive_tables')


class IndexFilesTest(ArchiveStorageMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.policy = create_policy()
        now = timezone.now()
        self.old_requests = [
            ApiRequest.objects.create(requested_at=now - relativedelta(months=months)) for months in (4, 5, 6)
        ]
        archive_tables()
        self.registered = self._registered()

    def _registered(self):
        return sorted(ArchiveFile.objects.values_list('path', 'rows', 'field', 'min_date', 'max_date'))

    def _save_parquet(self, name, columns):
        file = io.BytesIO()
        pl.DataFrame(columns).write_parquet(file)
        get_storage().save(f'apirequest/{name}', ContentFile(file.getvalue()))

    def test_registers_the_files_written_before_the_index(self):
        ArchiveFile.objects.all().delete()

        index_files()

        self.assertEqual(self._registered(), self.registered)
        self.policy.refresh_from_db()
        self.assertIsNotNone(self.policy.indexed_at)

    def test_removes_the_files_no_longer_in_the_storage(self):
        ArchiveFile.objects.create(content_type=self.policy.content_type, path='gone.parquet', rows=1)

        index_files()

        self.assertEqual(self._registered(), self.registered)

    def test_reads_again_the_files_indexed_by_another_field(self):
        ArchiveFile.objects.update(field='status_code', min_date=None, max_date=None)

        index_files()

        self.assertEqual(self._registered(), self.registered)

    def test_also_indexes_the_daily_folders_of_older_versions(self):
        requested_at = datetime(2025, 1, 10, 12, tzinfo=dt_timezone.utc)
        self._save_parquet('requested_at=2025-01-10/20250410T000000_x.parquet', {
            'id': ['x'], 'requested_at': [requested_at]
        })

        index_files()

        self.assertIn(
            ('requested_at=2025-01-10/20250410T000000_x.parquet', 1, 'requested_at', requested_at, requested_at),
            self._registered()
        )

    def test_file_without_the_index_field_is_registered_without_range(self):
        self._save_parquet('20250101T000000_old.parquet', {'id': ['x', 'y']})

        index_files()

        self.assertIn(('20250101T000000_old.parquet', 2, 'requested_at', None, None), self._registered())

    def test_datetime_without_time_zone_is_utc(self):
        self._save_parquet('20250101T000000_naive.parquet', {'id': ['x'], 'requested_at': [datetime(2025, 1, 10, 12)]})

        index_files()

        requested_at = datetime(2025, 1, 10, 12, tzinfo=dt_timezone.utc)
        self.assertIn(
            ('20250101T000000_naive.parquet', 1, 'requested_at', requested_at, requested_at), self._registered()
        )

    def test_skips_and_logs_the_file_that_cannot_be_read(self):
        get_storage().save('apirequest/20250101T000000_broken.parquet', ContentFile(b'not a parquet file'))
        ArchiveFile.objects.all().delete()

        with self.assertLogs('parquet_archive.archive', level='ERROR') as logs:
            index_files()

        self.assertEqual(self._registered(), self.registered)
        self.assertIn('apirequest/20250101T000000_broken.parquet could not be indexed', logs.output[0])

    def test_indexes_only_the_given_tables(self):
        ArchiveFile.objects.all().delete()

        index_files(model_names=['other'])
        self.assertEqual(self._registered(), [])

        index_files_task.apply(kwargs={'model_names': ['apirequest']})
        self.assertEqual(self._registered(), self.registered)
        self.assertEqual(index_files_task.name, 'parquet_archive.index_files')
