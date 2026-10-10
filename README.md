# django-parquet-archive

[![PyPI](https://img.shields.io/pypi/v/django-parquet-archive?logo=pypi&logoColor=white)](https://pypi.org/project/django-parquet-archive/)
[![Python](https://img.shields.io/pypi/pyversions/django-parquet-archive?logo=python&logoColor=white)](https://pypi.org/project/django-parquet-archive/)
[![Django](https://img.shields.io/pypi/frameworkversions/django/django-parquet-archive?logo=django&logoColor=white&label=django)](https://pypi.org/project/django-parquet-archive/)
[![CI](https://github.com/victoraugusto6/django-parquet-archive/actions/workflows/ci.yml/badge.svg)](https://github.com/victoraugusto6/django-parquet-archive/actions/workflows/ci.yml)
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
- **Daily folders by** (optional, strongly recommended from the first
  policy): a date field of the model itself that splits the files into one
  subfolder per day, so the admin only reads the days of the period being
  browsed. Files written without it stay in the model folder and are read by
  every query. Once set it cannot be changed; in a policy created by a
  migration, include the field in the `schema`.
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
        'partition_field': 'requested_at',
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

Each file has up to `rows_per_file` rows (with `partition_field`, all from the
same day), which are deleted from the database by their primary keys right
after the file is written. If a run fails halfway, the next one picks up what
is left without duplicating the archive, and nothing that started to match
the filter during the run is deleted without being in a file.

Files are stored as `<lowercase model name>/<UTC timestamp>_<uuid>.parquet`.
With `partition_field`, they are stored as
`<lowercase model name>/<partition_field>=<YYYY-MM-DD>/<UTC timestamp>_<uuid>.parquet`,
with the day in the project time zone (`TIME_ZONE`; changing it later shifts
the days of the rows already archived), and the task sorts the rows by the
`partition_field` before the configured ordering, so each day ends up in a few
full files. Rows without a `partition_field` value go straight into the model
folder, and every query reads them.

Queries read files from newest to oldest (by the timestamp in the file name),
and the schema of the first file applies to all of them: columns added to the
policy later show up, and are empty in older files.

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

In tables with `partition_field`, each page reads only the subfolders of one
period:

- the `partition_field` filter becomes a dedicated filter, with the months
  and, for the month shown, the days that exist in the archive (the shortcuts
  of the default date filter count from today, and the archive only has old
  rows);
- without a date filter, the changelist shows the most recent day in the
  archive;
- a period covers up to 31 days: a longer filter shows its 31 most recent
  days, with a warning;
- the link of each row carries the day (`?day=YYYY-MM-DD`), and the detail
  page reads only that day's subfolder and the files straight in the model
  folder.

Files straight in the model folder (rows without a date, or written before
`partition_field` was set) are read on every page and filtered by the period,
so rows without a date do not show up. Tables without `partition_field` read
every file, without a period.

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

Bump `version` in `pyproject.toml` and create the `v<version>` tag. The
`publish` workflow checks that the tag matches the version, builds and
publishes to PyPI with trusted publishing, with no token stored in the
repository, and creates the GitHub release with the commits since the
previous tag.

## License

[BSD-3-Clause](LICENSE).
