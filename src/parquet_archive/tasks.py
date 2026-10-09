from celery import shared_task

from parquet_archive.archive import archive_tables


@shared_task(name='parquet_archive.archive_tables')
def archive_tables_task(model_names=None):
    """
    Archive task. Scheduling (celery beat) and the queue are up to each
    project, pointing to the name 'parquet_archive.archive_tables'.
    """
    archive_tables(model_names)
