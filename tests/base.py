from copy import deepcopy
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from parquet_archive.models import ArchivePolicy
from tests.example.models import ApiRequest, Customer


API_REQUEST_CONFIG = {
    'rows_per_file': 2,
    'partition_field': 'requested_at',
    'filter': {'fields': ['requested_at'], 'keep_for': 3, 'unit': 'months'},
    'ordering': ['-requested_at'],
    'schema': {
        'id': 'String',
        'requested_at': 'Datetime',
        'url': 'String',
        'method': 'String',
        'body': 'String',
        'status_code': 'Int64',
        'amount': 'Decimal(14,2)',
        'success': 'Boolean',
        'reference_date': 'Date',
        'customer_id': 'Int64',
        'retry_of_id': 'String',
    },
}


def create_policy(model=ApiRequest, config=None, **kwargs):
    return ArchivePolicy.objects.create(
        content_type=ContentType.objects.get_for_model(model),
        config=deepcopy(config or API_REQUEST_CONFIG),
        **kwargs
    )


class ArchiveStorageMixin:
    """New in-memory storage for each test, used by the archive and by the query."""

    def setUp(self):
        super().setUp()
        storage_settings = self.settings(
            STORAGES={
                **settings.STORAGES,
                'parquet_archive_test': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
            },
            PARQUET_ARCHIVE_STORAGE='parquet_archive_test',
        )
        storage_settings.enable()
        self.addCleanup(storage_settings.disable)


class ArchivedRequestsMixin(ArchiveStorageMixin):
    """3 requests moved to the archive and 1 recent one that stays in the database."""

    archive_config = API_REQUEST_CONFIG

    def setUp(self):
        from parquet_archive.archive import archive_tables

        super().setUp()
        create_policy(config=self.archive_config)
        now = timezone.now()
        self.customer = Customer.objects.create(name='Alice', registered_at=date(2020, 1, 1))
        self.old_requests = [
            ApiRequest.objects.create(
                requested_at=now - relativedelta(months=6), method='post', url='https://api.alpha.example/bookings',
                status_code=200, body='{"document": "1"}', amount=Decimal('10.50'), customer=self.customer,
            ),
            ApiRequest.objects.create(
                requested_at=now - relativedelta(months=5), method='get', url='https://api.beta.example/orders',
                status_code=500, body='timeout error',
            ),
            ApiRequest.objects.create(
                requested_at=now - relativedelta(months=4), method='post', url='https://api.beta.example/orders',
                status_code=None, body='',
            ),
        ]
        self.recent = ApiRequest.objects.create(requested_at=now - relativedelta(days=1), method='get')
        archive_tables()
