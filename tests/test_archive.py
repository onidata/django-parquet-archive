import json
import posixpath
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

import polars as pl

from dateutil.relativedelta import relativedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from parquet_archive import archive
from parquet_archive.archive import archive_tables
from parquet_archive.conf import get_storage
from parquet_archive.models import ArchivePolicy
from parquet_archive.query import ArchiveFolder
from parquet_archive.schema import TIMESTAMPED_FILE_REGEX
from parquet_archive.tasks import archive_tables_task
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
        folders = [f'requested_at={timezone.localdate(request.requested_at)}' for request in self.old_requests]
        self.assertEqual(sorted(posixpath.dirname(file) for file in files), sorted(folders))
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
        self.assertEqual(ApiRequest.objects.count(), 3)

        archive_tables()

        files, data_frame = self._read_parquets()
        self.assertEqual(len(files), 3)
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
        self.assertEqual(len(deletes), 3)  # NOTE: One file per day

    def test_splits_the_day_into_files_of_rows_per_file(self):
        day = timezone.make_aware(datetime(2025, 1, 10, 12))
        for hours, request in enumerate(self.old_requests):
            ApiRequest.objects.filter(id=request.id).update(requested_at=day + timedelta(hours=hours))

        archive_tables()

        files, data_frame = self._read_parquets()
        self.assertEqual([posixpath.dirname(file) for file in files], ['requested_at=2025-01-10'] * 2)
        self.assertEqual(data_frame.height, 3)

    def test_folder_day_in_project_time_zone(self):
        ApiRequest.objects.filter(id=self.old_requests[0].id).update(
            requested_at=datetime(2025, 1, 10, 1, 30, tzinfo=dt_timezone.utc)
        )

        archive_tables()

        files, _ = self._read_parquets()
        self.assertIn('requested_at=2025-01-09', [posixpath.dirname(file) for file in files])

    def test_sorts_by_partition_field_before_the_configured_ordering(self):
        ArchivePolicy.objects.update(
            config={**API_REQUEST_CONFIG, 'rows_per_file': 10, 'ordering': ['status_code']}
        )
        day = timezone.make_aware(datetime(2025, 1, 10, 12))
        dates = (day, day - timedelta(days=1), day + timedelta(hours=1))
        for status_code, (request, requested_at) in enumerate(zip(self.old_requests, dates)):
            ApiRequest.objects.filter(id=request.id).update(status_code=status_code, requested_at=requested_at)

        archive_tables()

        files, _ = self._read_parquets()
        self.assertEqual(
            sorted(posixpath.dirname(file) for file in files),
            ['requested_at=2025-01-09', 'requested_at=2025-01-10']
        )

    def test_row_without_partition_date_goes_into_the_model_folder(self):
        ArchivePolicy.objects.update(
            config={**API_REQUEST_CONFIG, 'partition_field': 'reference_date'}
        )
        ApiRequest.objects.filter(id=self.old_requests[0].id).update(reference_date=date(2025, 1, 10))

        archive_tables()

        files, data_frame = self._read_parquets()
        self.assertEqual(sorted(posixpath.dirname(file) for file in files), ['', 'reference_date=2025-01-10'])
        self.assertEqual(data_frame.height, 3)

    def test_without_partition_field_writes_into_the_model_folder(self):
        ArchivePolicy.objects.update(config={**API_REQUEST_CONFIG, 'partition_field': None})

        archive_tables()

        files, data_frame = self._read_parquets()
        self.assertEqual([posixpath.dirname(file) for file in files], ['', ''])
        self.assertEqual(data_frame.height, 3)

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
