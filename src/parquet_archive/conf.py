from django.conf import settings
from django.core.files.storage import storages
from django.utils.module_loading import import_string


def get_storage():
    """
    Storage that holds the archive parquet files.

    The PARQUET_ARCHIVE_STORAGE setting accepts a STORAGES alias (default
    'default') or the import path of a storage object that already exists in
    the project (e.g. 'my_project.storages.archive_storage'). It is resolved on
    every call so override_settings works in tests.
    """
    storage = getattr(settings, 'PARQUET_ARCHIVE_STORAGE', 'default')
    if storage in settings.STORAGES:
        return storages[storage]
    return import_string(storage)


def get_max_files():
    """
    Maximum number of archive files that an admin page with a search, a
    filter other than the date or an ordering by another column reads
    (PARQUET_ARCHIVE_MAX_FILES, default 200). A longer period shows its most
    recent files, with a link to continue before them.
    """
    return getattr(settings, 'PARQUET_ARCHIVE_MAX_FILES', 200)
