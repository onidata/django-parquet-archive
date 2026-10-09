import io
import posixpath
import tempfile
from datetime import date, datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import polars as pl

from dateutil.relativedelta import relativedelta
from parameterized import parameterized

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from parquet_archive.archive import archive_tables
from parquet_archive.conf import get_storage
from parquet_archive.query import (
    FILE_COLUMN, ArchiveFolder, ArchiveQuery, UnsupportedLookup, _s3_storage_options, period_bounds
)
from tests.base import API_REQUEST_CONFIG, ArchivedRequestsMixin, ArchiveStorageMixin, create_policy
from tests.example.models import ApiRequest, Customer


class ArchiveQueryTest(ArchivedRequestsMixin, TestCase):

    def test_count_and_iteration_return_model_instances(self):
        query = ArchiveQuery(ApiRequest)

        rows = {row.id: row for row in query}

        self.assertEqual(query.count(), 3)
        self.assertEqual(set(rows), {request.id for request in self.old_requests})
        for request in self.old_requests:
            with self.subTest(id=request.id):
                self.assertIsInstance(rows[request.id], ApiRequest)
                self.assertEqual(rows[request.id].requested_at, request.requested_at)
                self.assertEqual(rows[request.id].status_code, request.status_code)
                self.assertEqual(rows[request.id].customer_id, request.customer_id)

    def test_also_reads_files_written_straight_into_the_model_folder(self):
        old = uuid4()
        file = io.BytesIO()
        pl.DataFrame({'id': [str(old)], 'status_code': [404], 'success': [False]}).write_parquet(file)
        get_storage().save(f'apirequest/{uuid4()}.parquet', ContentFile(file.getvalue()))

        rows = {row.id: row for row in ArchiveQuery(ApiRequest)}

        self.assertEqual(set(rows), {old, *(request.id for request in self.old_requests)})
        self.assertEqual(rows[old].status_code, 404)

    def test_fields_outside_the_archive_stay_empty_instead_of_the_model_default(self):
        row = ArchiveQuery(ApiRequest)[0]

        self.assertIsNone(row.headers)
        self.assertIsNone(row.external_code)

    @parameterized.expand([
        ('exact', {'method__exact': ['post']}, 2),
        ('values_with_or', {'method__exact': ['post', 'get']}, 3),
        ('lookups_with_and', {'method__exact': ['post'], 'status_code__exact': ['200']}, 1),
        ('gte', {'status_code__gte': ['400']}, 1),
        ('isnull', {'status_code__isnull': [True]}, 1),
        ('in', {'method__in': [['get', 'put']]}, 1),
        ('icontains', {'url__icontains': ['BETA']}, 2),
        ('iexact', {'method__iexact': ['POST']}, 2),
        ('contains', {'url__contains': ['beta']}, 2),
        ('contains_is_case_sensitive', {'url__contains': ['BETA']}, 0),
        ('startswith', {'url__startswith': ['https://api.alpha']}, 1),
        ('istartswith', {'url__istartswith': ['HTTPS://API.BETA']}, 2),
        ('endswith', {'url__endswith': ['/orders']}, 2),
        ('iendswith', {'url__iendswith': ['/BOOKINGS']}, 1),
        ('pk', {'pk': ['00000000-0000-0000-0000-000000000000']}, 0),
    ])
    def test_applies_admin_lookups(self, _, params, count):
        self.assertEqual(ArchiveQuery(ApiRequest).apply_lookups(params).count(), count)

    def test_filters_fk_by_the_attname_column(self):
        query = ArchiveQuery(ApiRequest)

        self.assertEqual(query.apply_lookups({'customer__id__exact': [str(self.customer.id)]}).count(), 1)
        self.assertEqual(query.apply_lookups({'customer__isnull': [True]}).count(), 2)

    def test_filters_date_with_text_value_from_date_field_list_filter(self):
        limit = timezone.localtime(timezone.now() - relativedelta(months=4, days=15))

        query = ArchiveQuery(ApiRequest).apply_lookups({'requested_at__lt': [str(limit)]})

        self.assertEqual(query.count(), 2)

    @parameterized.expand([
        ('headers__exact',), ('duration__isnull',), ('missing_field',), ('customer__name__exact',),
    ])
    def test_lookup_outside_the_archive_columns_raises_error(self, lookup):
        with self.assertRaises(UnsupportedLookup):
            ArchiveQuery(ApiRequest).apply_lookups({lookup: ['1']})

    def test_search_combines_terms_and_ignores_fields_outside_the_archive(self):
        query, ignored = ArchiveQuery(ApiRequest).search(
            ('headers', 'body', 'url', 'customer__name'), 'beta timeout'
        )

        self.assertEqual([row.id for row in query], [self.old_requests[1].id])
        self.assertEqual(ignored, ['headers', 'customer__name'])

    @parameterized.expand([
        ('istartswith', '^url', 'HTTPS://API.BETA', 2),
        ('iexact', '=method', 'POST', 2),
        ('icontains', '@body', 'TIMEOUT', 1),
    ])
    def test_search_with_search_fields_prefixes(self, _, search_field, term, count):
        query, ignored = ArchiveQuery(ApiRequest).search((search_field,), term)

        self.assertEqual(query.count(), count)
        self.assertEqual(ignored, [])

    def test_sorts_like_postgres(self):
        query = ArchiveQuery(ApiRequest)

        by_date = [row.id for row in query.sort(['-requested_at'])]
        by_status_code = [row.status_code for row in query.sort(['status_code'])]

        self.assertEqual(by_date, [request.id for request in reversed(self.old_requests)])
        self.assertEqual(by_status_code, [200, 500, None])

    def test_slicing_paginates_the_rows(self):
        query = ArchiveQuery(ApiRequest).sort(['requested_at'])

        self.assertEqual([row.id for row in query[1:3]], [self.old_requests[1].id, self.old_requests[2].id])
        self.assertEqual(query[0].id, self.old_requests[0].id)
        self.assertEqual((query[0].url, query[0].amount), (self.old_requests[0].url, self.old_requests[0].amount))

    def test_without_files_in_the_archive_returns_empty_query(self):
        query = ArchiveQuery(Customer).apply_lookups({'name__exact': ['Alice']})

        self.assertEqual(query.count(), 0)
        self.assertEqual(list(query), [])


class JsonDurationQueryTest(ArchiveStorageMixin, TestCase):

    def setUp(self):
        super().setUp()
        create_policy(config={
            **API_REQUEST_CONFIG,
            'schema': {**API_REQUEST_CONFIG['schema'], 'headers': 'Json', 'duration': 'Duration'},
        })
        self.request = ApiRequest.objects.create(
            requested_at=timezone.now() - relativedelta(months=6),
            headers={'Authorization': 'Bearer abc'},
            duration=timedelta(seconds=2),
        )
        ApiRequest.objects.create(requested_at=timezone.now() - relativedelta(months=5))
        archive_tables()

    def test_returns_json_as_dict_and_duration_as_timedelta(self):
        row = ArchiveQuery(ApiRequest).apply_lookups({'pk': [str(self.request.id)]})[0]

        self.assertEqual(row.headers, {'Authorization': 'Bearer abc'})
        self.assertEqual(row.duration, timedelta(seconds=2))

    def test_searches_json_and_filters_duration(self):
        query = ArchiveQuery(ApiRequest)

        found, ignored = query.search(('headers',), 'bearer')

        self.assertEqual([row.id for row in found], [self.request.id])
        self.assertEqual(ignored, [])
        self.assertEqual(query.apply_lookups({'duration__gte': ['00:00:01']}).count(), 1)


class FakeS3Storage:
    bucket_name = 'bucket'
    location = 'prefix'
    access_key = 'key'
    secret_key = 'secret'
    security_token = None
    endpoint_url = 'http://minio:9000'
    connection = SimpleNamespace(
        meta=SimpleNamespace(client=SimpleNamespace(meta=SimpleNamespace(region_name='us-east-1')))
    )
    FOLDERS = {
        'apirequest': (['requested_at=2026-01-01', 'requested_at=2025-01-01'], ['b.parquet', 'readme.txt']),
        'apirequest/requested_at=2026-01-01': ([], ['20260101T000000_a.parquet']),
        'apirequest/requested_at=2025-01-01': ([], ['20260926T000000_c.parquet']),
    }

    def listdir(self, folder):
        return self.FOLDERS[folder]


@patch('parquet_archive.query.pl.scan_parquet')
@patch('parquet_archive.query.get_storage', FakeS3Storage)
@patch('parquet_archive.query.S3Boto3Storage', FakeS3Storage)
class ArchiveFolderS3Test(TestCase):

    def test_s3_options_come_from_the_storage(self, scan_parquet):
        self.assertEqual(_s3_storage_options(FakeS3Storage()), {
            'aws_access_key_id': 'key',
            'aws_secret_access_key': 'secret',
            'aws_region': 'us-east-1',
            'aws_endpoint_url': 'http://minio:9000',
            'aws_allow_http': 'true',
        })

    def test_reads_the_parquets_of_the_folder_and_subfolders_straight_from_the_bucket(self, scan_parquet):
        ArchiveFolder(ApiRequest).load()

        scan_parquet.assert_called_once_with(
            [
                's3://bucket/prefix/apirequest/requested_at=2025-01-01/20260926T000000_c.parquet',
                's3://bucket/prefix/apirequest/requested_at=2026-01-01/20260101T000000_a.parquet',
                's3://bucket/prefix/apirequest/b.parquet',
            ],
            storage_options=_s3_storage_options(FakeS3Storage()),
            missing_columns='insert',
            extra_columns='ignore',
            include_file_paths=FILE_COLUMN,
        )

    def test_reads_only_the_subfolders_of_the_given_days(self, scan_parquet):
        ArchiveFolder(ApiRequest).load('requested_at', {date(2026, 1, 1)})

        expected = [
            's3://bucket/prefix/apirequest/requested_at=2026-01-01/20260101T000000_a.parquet',
            's3://bucket/prefix/apirequest/b.parquet',
        ]
        self.assertEqual(scan_parquet.call_args.args[0], expected)


@patch('parquet_archive.query._s3_storage_options', return_value=None)
@patch('parquet_archive.query.get_storage', FakeS3Storage)
@patch('parquet_archive.query.S3Boto3Storage', FakeS3Storage)
class S3PageTest(SimpleTestCase):
    """Reading through the S3 branch (scan_parquet), over local files."""

    def test_page_keeps_new_columns_when_mixing_old_files(self, _):
        hours = [datetime(2025, 1, 1, hour, tzinfo=dt_timezone.utc) for hour in range(1, 7)]
        with tempfile.TemporaryDirectory() as directory:
            old, new = f'{directory}/20250101T000000_old.parquet', f'{directory}/20260101T000000_new.parquet'
            pl.DataFrame({'id': [str(uuid4()) for _ in range(3)], 'requested_at': hours[0::2]}).write_parquet(old)
            pl.DataFrame({
                'id': [str(uuid4()) for _ in range(3)], 'requested_at': hours[1::2], 'status_code': [200, 404, 500]}
            ).write_parquet(new)
            folder = ArchiveFolder(ApiRequest)
            query = ArchiveQuery(ApiRequest, folder, folder.read([new, old])).sort(['requested_at'])

            for _ in range(10):
                self.assertEqual([row.status_code for row in query[0:6]], [None, 200, None, 404, None, 500])


class QueryWithoutPrimaryKeyTest(ArchiveStorageMixin, TestCase):

    def setUp(self):
        super().setUp()
        schema = {field: type_name for field, type_name in API_REQUEST_CONFIG['schema'].items() if field != 'id'}
        create_policy(config={**API_REQUEST_CONFIG, 'schema': schema})
        for months, status_code in ((6, 200), (5, 404), (4, 500)):
            ApiRequest.objects.create(
                requested_at=timezone.now() - relativedelta(months=months), status_code=status_code
            )
        archive_tables()

    def test_paginates_even_without_the_primary_key_in_the_columns(self):
        query = ArchiveQuery(ApiRequest).sort(['requested_at'])

        self.assertEqual([row.status_code for row in query[1:3]], [404, 500])


class ArchiveFolderTest(ArchivedRequestsMixin, TestCase):

    def test_subfolder_days_from_newest_to_oldest(self):
        folder = ArchiveFolder(ApiRequest)

        days = [timezone.localdate(request.requested_at) for request in reversed(self.old_requests)]
        self.assertEqual(folder.days('requested_at'), days)
        self.assertEqual(folder.days('reference_date'), [])

    def test_paths_of_the_given_days_subfolders_and_of_the_model_folder(self):
        storage = get_storage()
        storage.save('apirequest/old.parquet', ContentFile(b''))
        storage.save('apirequest/reference_date=2025-01-01/20250101T000000_x.parquet', ContentFile(b''))
        day = timezone.localdate(self.old_requests[1].requested_at)

        paths = ArchiveFolder(ApiRequest).paths('requested_at', {day})

        self.assertEqual(
            sorted(posixpath.dirname(path) for path in paths),
            ['', 'reference_date=2025-01-01', f'requested_at={day}']
        )


class PeriodBoundsTest(SimpleTestCase):

    @parameterized.expand([
        ('date_gte_lt', 'reference_date',
         {'reference_date__gte': ['2026-06-10'], 'reference_date__lt': ['2026-06-16']},
         (date(2026, 6, 10), date(2026, 6, 15))),
        ('date_exact_with_several_values', 'reference_date', {'reference_date': ['2026-06-12', '2026-06-10']},
         (date(2026, 6, 10), date(2026, 6, 12))),
        ('date_lte', 'reference_date', {'reference_date__lte': ['2026-06-15']}, (None, date(2026, 6, 15))),
        ('datetime_midnight', 'requested_at',
         {'requested_at__gte': ['2026-06-10 00:00:00-03:00'], 'requested_at__lt': ['2026-06-16 00:00:00-03:00']},
         (date(2026, 6, 10), date(2026, 6, 15))),
        ('datetime_lt_with_time', 'requested_at', {'requested_at__lt': ['2026-06-16 10:00:00-03:00']},
         (None, date(2026, 6, 16))),
        ('datetime_in_project_time_zone', 'requested_at', {'requested_at__gte': ['2026-06-10 01:00:00+00:00']},
         (date(2026, 6, 9), None)),
        ('ignores_other_fields_and_lookups', 'reference_date',
         {'status_code__gte': ['1'], 'reference_date__isnull': ['True']}, (None, None))]
    )
    def test_period_bounds(self, _, field, lookups, bounds):
        self.assertEqual(period_bounds(ApiRequest._meta.get_field(field), lookups), bounds)
