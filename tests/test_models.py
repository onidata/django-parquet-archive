from datetime import timedelta

import polars as pl

from dateutil.relativedelta import relativedelta
from freezegun import freeze_time
from parameterized import parameterized

from django.test import TestCase
from django.utils import timezone

from tests.base import API_REQUEST_CONFIG, create_policy
from tests.example.models import ApiRequest


class ArchivePolicyTest(TestCase):

    def test_get_archive_info_converts_config_to_archive_format(self):
        info = create_policy().get_archive_info()

        self.assertIs(info['model'], ApiRequest)
        self.assertEqual(info['rows_per_file'], 2)
        self.assertEqual(info['index_field'], 'requested_at')
        self.assertEqual(info['ordering'], ['-requested_at'])
        self.assertEqual(info['schema']['id'], pl.String)
        self.assertEqual(info['schema']['amount'], pl.Decimal(precision=14, scale=2))
        self.assertEqual(info['schema']['reference_date'], pl.Date)

    def test_get_archive_info_keeps_columns_in_model_order(self):
        # NOTE: Postgres jsonb does not keep the key order and it defines the parquet columns
        shuffled_schema = dict(reversed(API_REQUEST_CONFIG['schema'].items()))
        policy = create_policy(config={**API_REQUEST_CONFIG, 'schema': shuffled_schema})

        self.assertEqual(list(policy.get_archive_info()['schema']), list(API_REQUEST_CONFIG['schema']))

    @freeze_time('2026-06-15 12:00:00')
    def test_get_filter_selects_rows_up_to_the_cutoff_calculated_at_run_time(self):
        policy = create_policy()
        cutoff = timezone.now() - relativedelta(months=3)
        at_cutoff = ApiRequest.objects.create(requested_at=cutoff)
        ApiRequest.objects.create(requested_at=cutoff + timedelta(seconds=1))

        ids = list(ApiRequest.objects.filter(policy.get_filter()).values_list('id', flat=True))

        self.assertEqual(ids, [at_cutoff.id])

    def test_get_filter_without_date_fields_raises_error(self):
        policy = create_policy()
        policy.config['filter']['fields'] = []

        # NOTE: An empty Q() would select (and delete) the whole table
        with self.assertRaises(ValueError):
            policy.get_filter()

    @parameterized.expand([
        ('ascending', ['requested_at', 'status_code']),
        ('descending', ['-requested_at']),
    ])
    def test_index_field_is_the_date_field_of_the_ordering(self, _, ordering):
        policy = create_policy(config={**API_REQUEST_CONFIG, 'ordering': ordering})

        self.assertEqual(policy.get_index_field(), 'requested_at')

    @parameterized.expand([
        ('without_ordering', []),
        ('not_a_date', ['status_code']),
        ('date_that_accepts_null', ['reference_date']),
        ('date_only_after_another_field', ['status_code', '-requested_at']),
        ('date_of_a_relation', ['customer__registered_at']),
        ('missing_field', ['-gone_at']),
    ])
    def test_without_index_field(self, _, ordering):
        policy = create_policy(config={**API_REQUEST_CONFIG, 'ordering': ordering})

        self.assertIsNone(policy.get_index_field())

    def test_index_field_must_be_saved_in_the_archive(self):
        policy = create_policy()
        del policy.config['schema']['requested_at']

        self.assertIsNone(policy.get_index_field())
