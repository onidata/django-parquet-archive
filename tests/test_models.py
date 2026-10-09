from datetime import timedelta

import polars as pl

from dateutil.relativedelta import relativedelta
from freezegun import freeze_time

from django.test import TestCase
from django.utils import timezone

from tests.base import API_REQUEST_CONFIG, create_policy
from tests.example.models import ApiRequest


class ArchivePolicyTest(TestCase):

    def test_get_archive_info_converts_config_to_archive_format(self):
        info = create_policy().get_archive_info()

        self.assertIs(info['model'], ApiRequest)
        self.assertEqual(info['rows_per_file'], 2)
        self.assertEqual(info['partition_field'], 'requested_at')
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

    def test_partition_field_outside_schema_raises_error(self):
        policy = create_policy()
        del policy.config['schema']['requested_at']

        with self.assertRaises(ValueError):
            policy.get_archive_info()

    def test_partition_field_is_optional(self):
        policy = create_policy()
        del policy.config['partition_field']

        self.assertIsNone(policy.get_archive_info()['partition_field'])
