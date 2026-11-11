from datetime import date, datetime, timezone as dt_timezone

import polars as pl

from parameterized import parameterized

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from parquet_archive.schema import (
    TIMESTAMPED_FILE_REGEX, date_field_paths, day_of, index_value, model_folder, new_parquet_path,
    polars_type_for_field, relations_blocking_delete, sort_newest_first, to_polars_schema
)
from tests.example.models import ApiRequest, Customer, Order


class PolarsTypeForFieldTest(SimpleTestCase):

    def test_maps_supported_fields(self):
        expected = {
            'id': 'String',
            'requested_at': 'Datetime',
            'url': 'String',
            'status_code': 'Int64',
            'amount': 'Decimal(14,2)',
            'success': 'Boolean',
            'reference_date': 'Date',
            'customer': 'Int64',
            'retry_of': 'String',
        }
        for field_name, type_name in expected.items():
            with self.subTest(field=field_name):
                self.assertEqual(polars_type_for_field(ApiRequest._meta.get_field(field_name)), type_name)

    def test_json_is_written_as_text_and_duration_as_duration(self):
        self.assertEqual(polars_type_for_field(ApiRequest._meta.get_field('headers')), 'Json')
        self.assertEqual(polars_type_for_field(ApiRequest._meta.get_field('duration')), 'Duration')

    def test_does_not_support_other_fields_with_from_db_value(self):
        self.assertIsNone(polars_type_for_field(ApiRequest._meta.get_field('external_code')))


class SchemaTest(SimpleTestCase):

    def test_converts_saved_schema_to_polars_types(self):
        schema = to_polars_schema({
            'id': 'String', 'amount': 'Decimal(14,2)', 'at': 'Datetime', 'headers': 'Json', 'duration': 'Duration'
        })

        self.assertEqual(schema, {
            'id': pl.String,
            'amount': pl.Decimal(precision=14, scale=2),
            'at': pl.Datetime,
            'headers': pl.String,
            'duration': pl.Duration(time_unit='us'),
        })

    def test_file_name_starts_with_the_timestamp(self):
        folder, file = new_parquet_path('apirequest').split('/')

        self.assertEqual(folder, 'apirequest')
        self.assertRegex(file, TIMESTAMPED_FILE_REGEX)
        self.assertTrue(file.endswith('.parquet'))

    def test_sorts_timestamped_files_newest_first_and_then_the_others(self):
        files = [
            'b1c2.parquet', '20260101T000000_x.parquet', 'a9f8.parquet', '20260926T120000_y.parquet',
        ]

        # NOTE: A generator, as it is called when reading the parquet files
        self.assertEqual(sort_newest_first(file for file in files), [
            '20260926T120000_y.parquet', '20260101T000000_x.parquet', 'a9f8.parquet', 'b1c2.parquet',
        ])

    def test_sorts_by_file_name_regardless_of_the_day_subfolder(self):
        files = [
            'date=2026-09-01/20260101T000000_x.parquet', 'a9f8.parquet', 'date=2025-01-01/20260926T120000_y.parquet',
        ]

        expected = [
            'date=2025-01-01/20260926T120000_y.parquet', 'date=2026-09-01/20260101T000000_x.parquet', 'a9f8.parquet',
        ]
        self.assertEqual(sort_newest_first(files), expected)

    @parameterized.expand([
        ('date', date(2026, 6, 28), date(2026, 6, 28)),
        ('datetime_in_project_time_zone', datetime(2026, 6, 28, 1, 30, tzinfo=dt_timezone.utc), date(2026, 6, 27)),
        ('naive_datetime', datetime(2026, 6, 28, 1, 30), date(2026, 6, 28))]
    )
    def test_day_of(self, _, value, day):
        self.assertEqual(day_of(value), day)

    def test_day_uses_project_time_zone_and_not_the_active_one(self):
        with timezone.override('UTC'):
            day = day_of(datetime(2026, 6, 28, 1, 30, tzinfo=dt_timezone.utc))

        self.assertEqual(day, date(2026, 6, 27))

    @parameterized.expand([
        ('date_becomes_midnight_in_project_time_zone', date(2026, 6, 28),
         datetime(2026, 6, 28, 3, tzinfo=dt_timezone.utc)),
        ('datetime_stays', datetime(2026, 6, 28, 1, 30, tzinfo=dt_timezone.utc),
         datetime(2026, 6, 28, 1, 30, tzinfo=dt_timezone.utc)),
        ('none', None, None)]
    )
    def test_index_value(self, _, value, expected):
        self.assertEqual(index_value(value), expected)

    def test_index_value_without_time_zone_support_is_naive(self):
        with self.settings(USE_TZ=False):
            self.assertEqual(index_value(date(2026, 6, 28)), datetime(2026, 6, 28))

    def test_model_folder_is_the_lowercase_class_name(self):
        self.assertEqual(model_folder(ApiRequest), 'apirequest')

    def test_date_paths_include_direct_fks(self):
        paths = [path for path, _ in date_field_paths(ApiRequest)]

        self.assertEqual(
            paths,
            ['requested_at', 'reference_date', 'customer__registered_at', 'retry_of__requested_at',
             'retry_of__reference_date']
        )


class RelationsBlockingDeleteTest(SimpleTestCase):

    def test_model_without_blocking_relations(self):
        # NOTE: retry_of is SET_NULL and does not block the delete
        self.assertEqual(relations_blocking_delete(ApiRequest), [])

    def test_fk_with_protect(self):
        self.assertEqual(relations_blocking_delete(Customer), ['example.ApiRequest.customer (PROTECT)'])

    def test_many_to_many_and_cascade_wrapped_like_polymorphic(self):
        self.assertEqual(
            sorted(relations_blocking_delete(Order)),
            ['example.Attachment.order (CASCADE)', 'example.Tag (relation tags)']
        )


class MigrationsTest(TestCase):

    def test_library_migrations_are_up_to_date(self):
        call_command('makemigrations', 'parquet_archive', '--check', '--dry-run', verbosity=0)
