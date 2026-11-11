# django-parquet-archive

[![PyPI](https://img.shields.io/pypi/v/django-parquet-archive?logo=pypi&logoColor=white)](https://pypi.org/project/django-parquet-archive/)
[![Python](https://img.shields.io/pypi/pyversions/django-parquet-archive?logo=python&logoColor=white)](https://pypi.org/project/django-parquet-archive/)
[![Django](https://img.shields.io/pypi/frameworkversions/django/django-parquet-archive?logo=django&logoColor=white&label=django)](https://pypi.org/project/django-parquet-archive/)
[![CI](https://github.com/onidata/django-parquet-archive/actions/workflows/ci.yml/badge.svg)](https://github.com/onidata/django-parquet-archive/actions/workflows/ci.yml)
[![License](https://img.shields.io/pypi/l/django-parquet-archive)](LICENSE)

Archive old rows of Django tables: they are written to parquet files (S3 or
any Django storage) and deleted from the database. Tables are configured in
the admin, and the archived rows can be browsed in the model's own changelist.

The archive runs as a celery task.

## Supported versions

| Django | Python       |
|--------|--------------|
| 5.2    | 3.10 to 3.14 |
| 6.0    | 3.12 to 3.14 |
| 6.1    | 3.12 to 3.14 |

## Installation

```bash
pip install "django-parquet-archive[s3]"
```

The `s3` extra installs `django-storages[s3]`. Without it the library works
with any Django storage, but reads the parquet files by downloading them
through the storage instead of querying the bucket directly.

## Configuration

```python
INSTALLED_APPS = [
    # ...
    'parquet_archive',
]

# A STORAGES alias (default 'default') or the import path of a storage object of the project
PARQUET_ARCHIVE_STORAGE = 'parquet_archive'
STORAGES['parquet_archive'] = {
    'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage',
    'OPTIONS': {'bucket_name': 'my-archive-bucket'},
}

# Optional: files an admin page with a search or other filters reads at most (default 200)
PARQUET_ARCHIVE_MAX_FILES = 200
```

Schedule it with celery beat (the queue and the frequency are up to the project):

```python
CELERY_BEAT_SCHEDULE = {
    'Archive old table rows': {
        'task': 'parquet_archive.archive_tables',
        'schedule': crontab(day_of_month='5', hour='0', minute='0'),
    },
}
```

Then run `python manage.py migrate parquet_archive`.

## Archived tables

Each table is an `ArchivePolicy`, created in the admin under
*Parquet archive → Archive policies*. The form lists the fields of the chosen
model:

- **Date fields**: a row is archived when any of them is older than the
  configured age. Includes fields of direct FKs (e.g. `order__cancelled_at`).
- **Keep for**: age in days, months or years.
- **Ordering**: the order of the rows in the files, and the field is always
  saved. Ordering by a date field of the model itself that is not null (e.g.
  `-requested_at`) makes it the **index field**: each file covers a short
  stretch of it, and the admin reads only the files of each page (see
  *Browsing in the admin*). In a policy created by a migration, include the
  field in the `schema`.
- **Saved columns**: the parquet columns. The primary key is always saved.
  JSON is written as text and `DurationField` as a duration. Fields with
  `from_db_value` (encrypted fields, custom value objects...) are not offered:
  the value read from the database is a Python object, and an encrypted
  value would be archived decrypted.

Only superusers create, change, delete or run policies, because they define
which rows are deleted from the database. Other users with the view
permission see the saved policy. Every change made in the admin is recorded
in the admin's own history, with the user and the changed fields.

The form rejects models that other models reference with `CASCADE`,
`PROTECT` or `RESTRICT`, or that have a `ManyToMany`: deleting the rows after
archiving them would delete data that is not archived, or would fail after
the parquet files were written.

To create the initial policies with a migration in the project:

```python
def create_policies(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    ArchivePolicy = apps.get_model('parquet_archive', 'ArchivePolicy')
    content_type, _ = ContentType.objects.get_or_create(app_label='core', model='apirequest')
    ArchivePolicy.objects.get_or_create(content_type=content_type, defaults={'config': {
        'rows_per_file': 3000,
        'filter': {'fields': ['requested_at'], 'keep_for': 3, 'unit': 'months'},
        'ordering': ['-requested_at'],
        'schema': {'id': 'String', 'requested_at': 'Datetime', 'status_code': 'Int64'},
    }})


class Migration(migrations.Migration):
    dependencies = [('parquet_archive', '0001_initial')]
    operations = [migrations.RunPython(create_policies, migrations.RunPython.noop)]
```

The `schema` types are `String`, `Int64`, `Float64`, `Boolean`, `Date`,
`Datetime`, `Duration`, `Json` and `Decimal(precision,scale)`.

Each file has up to `rows_per_file` rows, which are deleted from the database
by their primary keys right after the file is written. If a run fails halfway,
the next one picks up what is left without duplicating the archive, and
nothing that started to match the filter during the run is deleted without
being in a file.

Files are stored as `<lowercase model name>/<UTC timestamp>_<uuid>.parquet`,
and each one is registered in the index of the archive files right after it
is written.

Queries read files from newest to oldest (by the timestamp in the file name),
and the schema of the first file applies to all of them: columns added to the
policy later show up, and are empty in older files.

## Index of the archive files

The admin lists the archive from an index in the database (`ArchiveFile`):
the rows of each file and the first and last value of the index field. The
archive task registers every file it writes. Files written before the index
existed (by version 0.1, or by another process into the same folders) are
registered by the indexing, which reads each one once.

There is no manual step: the first time the archive of a table is listed in
the admin, if the table was never indexed (e.g. right after the upgrade) or
has files indexed by another field (its ordering changed), the admin queues
the indexing task (`parquet_archive.index_files`, which needs a celery worker)
and warns that the listing may be missing files for a few minutes. It is
queued once; a request pending for more than an hour is queued again. The
indexing can also run at any time:

- in the admin, *Parquet archive → Archive policies*, action *Index the
  archive files of the selected tables* (superusers only);
- or the celery task `parquet_archive.index_files`, which also takes
  `model_names` and can be scheduled to pick up files changed in the storage
  by other processes.

The indexing also removes the files that are no longer in the storage. A file
that cannot be read is skipped with an error in the log. Files without the
index field (written before it was saved) are left out of the listing, with a
warning.

A file removed from the storage outside the library is still in the index
until the next indexing: the admin pages that read it show an error message
instead of the rows, and the detail of its rows goes back to the list with
the message.

## Browsing in the admin

```python
from parquet_archive.admin import ArchiveAdminMixin


class ApiRequestAdmin(ArchiveAdminMixin, admin.ModelAdmin):
    ...
```

The mixin adds the **Data source** filter (Database / Archive). In the
archive, the changelist reads the parquet files and keeps `list_display`,
pagination, column sorting, field filters (FKs included) and search. The
link of each row opens a read-only detail page.

Disabled in the archive: actions, custom filters (`SimpleListFilter`),
facets and `date_hierarchy`. Filters and search fields that do not exist in
the parquet files are ignored with a warning on the page.

In tables with an index field, each page reads only the files it needs:

- the filter of the index field becomes a dedicated filter, with the months
  and, for the month shown, the days that the archive files cover (the
  shortcuts of the default date filter count from today, and the archive only
  has old rows). In tables with few rows per day a file covers several days,
  and days without rows may show up;
- without other filters, search or ordering by another column, the count
  comes from the index and each page reads only its files, in any period.
  Without an `ordering` in the `ModelAdmin` or in the model, the archive is
  listed by the index field, newest first;
- with a search, other filters or ordering by another column, the page reads
  the files of the period. When they are more than `PARQUET_ARCHIVE_MAX_FILES`,
  it shows the rows of the most recent ones, with a link to continue before
  them;
- the link of each row carries its date (`?at=...`), and the detail page reads
  only the files whose range has it.

Tables without an index field read every file on every page.

## Upgrading from 0.1

The daily folders (`partition_field`) were replaced by the index of the
archive files: new files go straight into the model folder, and the files
already in daily folders keep being read. After upgrading, run
`python manage.py migrate parquet_archive`. The files already archived are
indexed the first time the archive is listed in the admin (see *Index of the
archive files*). The `partition_field` key left in a
policy config is ignored, and saving the policy in the admin removes it: for
the admin to keep reading only the files of each page, start the ordering of
the policy with that field (it must not accept null).

## Translations

The admin texts are in English, with a Brazilian Portuguese (`pt_BR`)
translation included.

## Development

```bash
uv sync
uv run pytest
# Minimum supported Django version
uv run --with 'django~=5.2.0' pytest
```

CI runs the suite on every supported combination of Python and Django, and a
Django deprecation caused by the library code fails the suite.

### Releasing

Bump `version` in `pyproject.toml` and publish a GitHub release with the
`v<version>` tag. The `publish` workflow checks that the tag matches the
version, builds and publishes to PyPI with trusted publishing, with no token
stored in the repository, and attaches the distributions to the release.

## License

[BSD-3-Clause](LICENSE).
