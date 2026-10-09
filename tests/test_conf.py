from django.core.files.storage import InMemoryStorage, storages
from django.test import SimpleTestCase, override_settings

from parquet_archive.conf import get_storage


PROJECT_STORAGE = InMemoryStorage()


class GetStorageTest(SimpleTestCase):

    def test_uses_default_alias_without_setting(self):
        self.assertIs(get_storage(), storages['default'])

    @override_settings(PARQUET_ARCHIVE_STORAGE='tests.test_conf.PROJECT_STORAGE')
    def test_accepts_storage_object_path(self):
        self.assertIs(get_storage(), PROJECT_STORAGE)
