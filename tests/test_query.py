import io
import posixpath
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import polars as pl

from dateutil.relativedelta import relativedelta
from parameterized import parameterized

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from parquet_archive.archive import archive_tables, index_files
from parquet_archive.conf import get_storage
from parquet_archive.models import ArchiveFile
from parquet_archive.query import (
    FILE_COLUMN, ArchiveFolder, ArchiveQuery, FileGroup, IndexedQuery, UnsupportedLookup, _s3_storage_options,
    file_groups, period_bounds
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
            schema=None,
            missing_columns='insert',
            extra_columns='ignore',
            include_file_paths=FILE_COLUMN,
        )

    def test_reads_with_the_given_schema(self, scan_parquet):
        schema = {'id': pl.String, 'status_code': pl.Int64}
        folder = ArchiveFolder(ApiRequest, schema)

        folder.read(folder.sources(['b.parquet']))

        self.assertEqual(scan_parquet.call_args.args[0], ['s3://bucket/prefix/apirequest/b.parquet'])
        self.assertEqual(scan_parquet.call_args.kwargs['schema'], schema)


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

    def test_paths_of_the_model_folder_and_of_its_subfolders(self):
        storage = get_storage()
        storage.save('apirequest/old.parquet', ContentFile(b''))
        storage.save('apirequest/requested_at=2025-01-01/20250101T000000_x.parquet', ContentFile(b''))

        paths = ArchiveFolder(ApiRequest).paths()

        self.assertEqual(sorted(posixpath.dirname(path) for path in paths), ['', '', '', 'requested_at=2025-01-01'])

    def test_files_without_columns_of_the_schema_get_them_empty(self):
        folder = ArchiveFolder(ApiRequest, {'id': pl.String, 'status_code': pl.Int64, 'gone': pl.String})
        paths = folder.paths()

        frame = folder.read(folder.sources(paths)).collect()

        self.assertEqual(frame.columns, ['id', 'status_code', 'gone', FILE_COLUMN])
        self.assertEqual(frame['gone'].to_list(), [None] * 3)
        self.assertEqual(sorted(frame['id'].to_list()), sorted(str(request.id) for request in self.old_requests))

    def test_reading_no_file_returns_an_empty_frame(self):
        self.assertEqual(ArchiveFolder(ApiRequest).read([]).collect().height, 0)


def utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


class PeriodBoundsTest(SimpleTestCase):

    @parameterized.expand([
        ('date_gte_lt', 'reference_date',
         {'reference_date__gte': ['2026-06-10'], 'reference_date__lt': ['2026-06-16']},
         (utc(2026, 6, 10, 3), utc(2026, 6, 16, 3))),
        ('date_exact_with_several_values', 'reference_date', {'reference_date': ['2026-06-12', '2026-06-10']},
         (utc(2026, 6, 10, 3), utc(2026, 6, 13, 3))),
        ('date_lte_and_gt', 'reference_date',
         {'reference_date__lte': ['2026-06-15'], 'reference_date__gt': ['2026-06-01']},
         (utc(2026, 6, 2, 3), utc(2026, 6, 16, 3))),
        ('datetime_gte_lt', 'requested_at',
         {'requested_at__gte': ['2026-06-10 00:00:00-03:00'], 'requested_at__lt': ['2026-06-16 10:00:00+00:00']},
         (utc(2026, 6, 10, 3), utc(2026, 6, 16, 10))),
        ('datetime_lte_and_gt_take_the_next_microsecond', 'requested_at',
         {'requested_at__gt': ['2026-06-10 03:00:00+00:00'], 'requested_at__lte': ['2026-06-16 10:00:00+00:00']},
         (utc(2026, 6, 10, 3, 0, 0, 1), utc(2026, 6, 16, 10, 0, 0, 1))),
        ('naive_datetime_in_the_current_time_zone', 'requested_at', {'requested_at__gte': ['2026-06-10']},
         (utc(2026, 6, 10, 3), None)),
        ('ignores_other_fields_and_lookups', 'reference_date',
         {'status_code__gte': ['1'], 'reference_date__isnull': ['True'], 'reference_date__year': ['2026']},
         (None, None))]
    )
    def test_period_bounds(self, _, field, lookups, bounds):
        self.assertEqual(period_bounds(ApiRequest._meta.get_field(field), lookups), bounds)


def archive_file(path, min_date, max_date, rows=1):
    return SimpleNamespace(path=path, rows=rows, min_date=min_date, max_date=max_date)


class FileGroupsTest(SimpleTestCase):

    def test_groups_the_files_whose_ranges_overlap(self):
        files = [
            archive_file('a', utc(2026, 6, 9), utc(2026, 6, 10)),
            archive_file('b', utc(2026, 6, 5), utc(2026, 6, 9)),
            archive_file('c', utc(2026, 6, 6), utc(2026, 6, 7), rows=2),
            archive_file('d', utc(2026, 6, 1), utc(2026, 6, 4)),
        ]

        self.assertEqual(file_groups(files), [
            FileGroup(files[:3], 4, utc(2026, 6, 5), utc(2026, 6, 10)),
            FileGroup(files[3:], 1, utc(2026, 6, 1), utc(2026, 6, 4)),
        ])


class IndexedQueryTest(ArchiveStorageMixin, TestCase):
    """9 requests in 5 files of 2 rows, plus a late file that overlaps two of them."""

    def setUp(self):
        super().setUp()
        create_policy()
        start = timezone.now() - relativedelta(months=4)
        for days in range(9):
            ApiRequest.objects.create(requested_at=start - timedelta(days=days * 3), status_code=days)
        archive_tables()
        self.late = uuid4()
        late_file = io.BytesIO()
        pl.DataFrame({
            'id': [str(self.late)], 'requested_at': [start - timedelta(days=7)], 'status_code': [100],
            'success': [False],
        }).write_parquet(late_file)
        get_storage().save('apirequest/20250101T000000_late.parquet', ContentFile(late_file.getvalue()))
        index_files()
        self.files = list(
            ArchiveFile.objects.order_by('-max_date').values_list('path', 'rows', 'min_date', 'max_date', named=True)
        )
        newest = sorted(file.path for file in self.files)[-1]
        self.folder = ArchiveFolder(ApiRequest, ArchiveFolder(ApiRequest).read_schema(newest))

    def _ids(self, rows):
        return [row.id for row in rows]

    @parameterized.expand([('descending', ['-requested_at', '-pk']), ('ascending', ['requested_at', 'pk'])])
    def test_pages_follow_the_order_of_every_row(self, _, ordering):
        expected = self._ids(ArchiveQuery(ApiRequest).sort(ordering))
        query = IndexedQuery(ApiRequest, self.folder, self.files, ordering=ordering)

        self.assertEqual(query.count(), 10)
        self.assertIn(2, [len(group.files) for group in file_groups(self.files)])
        for start in range(0, 12, 3):
            with self.subTest(start=start):
                self.assertEqual(self._ids(query[start:start + 3]), expected[start:start + 3])
        self.assertEqual(self._ids(query), expected)

    def test_counts_by_the_index_and_reads_only_the_files_of_the_page(self):
        query = IndexedQuery(ApiRequest, self.folder, self.files, ordering=['-requested_at', '-pk'])

        with patch.object(ArchiveFolder, 'read', autospec=True, side_effect=ArchiveFolder.read) as read:
            self.assertEqual(query.count(), 10)
            read.assert_not_called()
            query[0:2]

        sources = {source for call in read.call_args_list for source in call.args[1]}
        self.assertEqual(sources, {self.files[0].path})

    def test_period_counts_the_groups_on_its_edges_by_reading_them(self):
        lookups = {
            'requested_at__gte': [str(self.files[2].min_date)], 'requested_at__lt': [str(self.files[0].max_date)]
        }
        period = period_bounds(ApiRequest._meta.get_field('requested_at'), lookups)
        files = [file for file in self.files if file.max_date >= period[0] and file.min_date < period[1]]
        expected = self._ids(ArchiveQuery(ApiRequest).apply_lookups(lookups).sort(['-requested_at', '-pk']))

        query = IndexedQuery(ApiRequest, self.folder, files, period, lookups, ['-requested_at', '-pk'])

        self.assertEqual(query.count(), len(expected))
        self.assertEqual(self._ids(query), expected)
