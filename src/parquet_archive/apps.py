from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ParquetArchiveConfig(AppConfig):
    name = 'parquet_archive'
    verbose_name = _('Parquet archive')
    # NOTE: Fixed so the library migrations do not depend on each project's DEFAULT_AUTO_FIELD
    default_auto_field = 'django.db.models.BigAutoField'
