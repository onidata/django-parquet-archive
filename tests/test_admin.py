import html
import io
import re
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from unittest.mock import patch
from uuid import uuid4

import polars as pl

from django.contrib.admin import DateFieldListFilter
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.admin.utils import quote
from django.contrib.admin.views.main import ChangeList
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.http import urlencode

from parquet_archive.admin import ArchiveChangeList, ArchiveDayFilter, DataSourceFilter, _start_of_day
from parquet_archive.archive import archive_tables, index_files
from parquet_archive.conf import get_storage
from parquet_archive.models import ArchiveFile, ArchivePolicy
from parquet_archive.query import ArchiveFolder, ArchiveQuery, IndexedQuery
from tests.base import API_REQUEST_CONFIG, ArchivedRequestsMixin, ArchiveStorageMixin, create_policy
from tests.example.admin import ApiRequestAdmin
from tests.example.models import ApiRequest, Order, Payment


ARCHIVE = {DataSourceFilter.parameter_name: DataSourceFilter.ARCHIVE}


def short_date(day):
    return date_format(day, 'SHORT_DATE_FORMAT')


class SuperuserMixin:

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser('admin', 'admin@example.com', '123456'))


class ArchivePolicyAdminTest(SuperuserMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.content_type = ContentType.objects.get_for_model(ApiRequest)
        self.add_url = reverse('admin:parquet_archive_archivepolicy_add')
        self.changelist_url = reverse('admin:parquet_archive_archivepolicy_changelist')

    def test_creation_lists_fields_of_the_model_given_in_the_url(self):
        response = self.client.get(self.add_url, {'content_type': self.content_type.pk})

        form = response.context['adminform'].form
        self.assertEqual(response.status_code, 200)
        self.assertIn('requested_at', [value for value, _ in form.fields['columns'].choices])
        self.assertContains(response, 'parquet_archive/js/admin.js')
        self.assertContains(response, 'admin/js/SelectFilter2.js')
        self.assertContains(response, 'admin/js/vendor/select2/select2.full.min.js')
        self.assertContains(response, 'data-theme="admin-autocomplete"')

    def test_model_search_in_the_project_language(self):
        with self.settings(LANGUAGE_CODE='pt-br'):
            response = self.client.get(self.add_url)

        self.assertContains(response, 'admin/js/vendor/select2/i18n/pt-BR.js')
        self.assertContains(response, 'lang="pt-BR"')

    def test_creates_policy_through_the_admin(self):
        response = self.client.post(self.add_url, {
            'content_type': self.content_type.pk,
            'active': 'on',
            'date_fields': ['requested_at'],
            'keep_for': 3,
            'unit': 'months',
            'rows_per_file': 3000,
            'ordering': '-requested_at',
            'columns': ['requested_at', 'status_code'],
        })

        self.assertEqual(response.status_code, 302)
        policy = ArchivePolicy.objects.get(content_type=self.content_type)
        self.assertEqual(policy.config['schema'], {
            'id': 'String', 'requested_at': 'Datetime', 'status_code': 'Int64'
        })

    def test_editing_keeps_the_model_and_updates_the_config(self):
        policy = create_policy()
        url = reverse('admin:parquet_archive_archivepolicy_change', args=(policy.pk,))

        response = self.client.post(url, {
            'active': 'on',
            'date_fields': ['requested_at'],
            'keep_for': 2,
            'unit': 'months',
            'rows_per_file': 2,
            'ordering': '-requested_at',
            'columns': [field for field in API_REQUEST_CONFIG['schema'] if field != 'id'],
        })

        self.assertEqual(response.status_code, 302)
        policy.refresh_from_db()
        self.assertEqual(policy.content_type, self.content_type)
        self.assertEqual(policy.config['filter']['keep_for'], 2)
        self.assertEqual(policy.config['schema'], API_REQUEST_CONFIG['schema'])

    @patch('parquet_archive.admin.archive_tables_task')
    def test_action_archives_only_active_policies(self, archive_tables_task):
        active = create_policy()
        inactive = create_policy(Order, active=False)

        self.client.post(
            self.changelist_url, {'action': 'archive_tables', '_selected_action': [active.pk, inactive.pk]}
        )

        archive_tables_task.delay.assert_called_once_with(model_names=['apirequest'])

    @patch('parquet_archive.admin.archive_tables_task')
    def test_action_without_active_policy_does_not_run_the_task(self, archive_tables_task):
        inactive = create_policy(active=False)

        response = self.client.post(
            self.changelist_url, {'action': 'archive_tables', '_selected_action': [inactive.pk]}, follow=True
        )

        # NOTE: Running it with an empty list would make the task archive every table
        archive_tables_task.delay.assert_not_called()
        self.assertIn('No active policy selected.', [str(m) for m in response.context['messages']])

    @patch('parquet_archive.admin.index_files_task')
    def test_action_indexes_the_files_of_the_selected_policies(self, index_files_task):
        active = create_policy()
        inactive = create_policy(Order, active=False)

        self.client.post(
            self.changelist_url, {'action': 'index_files', '_selected_action': [active.pk, inactive.pk]}
        )

        index_files_task.delay.assert_called_once_with(model_names=['apirequest', 'order'])


class ArchivePolicyPermissionsTest(TestCase):
    """Even with every model permission, only superusers change or run the policy."""

    def setUp(self):
        staff = User.objects.create_user('staff', password='123456', is_staff=True)
        staff.user_permissions.set(Permission.objects.filter(
            content_type=ContentType.objects.get_for_model(ArchivePolicy)
        ))
        self.client.force_login(staff)
        self.policy = create_policy()
        self.change_url = reverse('admin:parquet_archive_archivepolicy_change', args=(self.policy.pk,))

    def test_does_not_create_policy(self):
        response = self.client.get(reverse('admin:parquet_archive_archivepolicy_add'))

        self.assertEqual(response.status_code, 403)

    def test_views_policy_read_only(self):
        response = self.client.get(self.change_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '&quot;requested_at&quot;')
        for field in ('content_type', 'active', 'date_fields', 'keep_for', 'columns'):
            self.assertNotContains(response, f'name="{field}"')

    def test_does_not_change_policy(self):
        response = self.client.post(self.change_url, {'active': '', 'keep_for': 1, 'unit': 'days'})

        self.policy.refresh_from_db()
        self.assertEqual(response.status_code, 403)
        self.assertTrue(self.policy.active)

    def test_does_not_list_the_run_action(self):
        response = self.client.get(reverse('admin:parquet_archive_archivepolicy_changelist'))

        # NOTE: Without the run action and the bulk delete no action is left and the selector does not show up
        self.assertIsNone(response.context['action_form'])


class ArchivePolicyHistoryTest(SuperuserMixin, TestCase):

    def test_records_who_changed_the_policy(self):
        policy = create_policy()
        url = reverse('admin:parquet_archive_archivepolicy_change', args=(policy.pk,))

        self.client.post(url, {
            'date_fields': ['requested_at'],
            'keep_for': 6,
            'unit': 'months',
            'rows_per_file': 2,
            'ordering': '-requested_at',
            'columns': [field for field in API_REQUEST_CONFIG['schema'] if field != 'id'],
        })

        # NOTE: The history is the admin's own, without the previous values
        change = LogEntry.objects.get(object_id=policy.pk)
        self.assertEqual(change.action_flag, CHANGE)
        self.assertEqual(change.user.username, 'admin')
        self.assertEqual(change.get_change_message(), 'Changed Active and Keep for.')
        history_url = reverse('admin:parquet_archive_archivepolicy_history', args=(policy.pk,))
        self.assertContains(self.client.get(history_url), 'Changed Active and Keep for.')


class ArchiveAdminMixinTest(SuperuserMixin, ArchivedRequestsMixin, TestCase):
    """Table without index field, where the changelist reads every file."""

    archive_config = {**API_REQUEST_CONFIG, 'ordering': ['status_code']}

    def setUp(self):
        super().setUp()
        self.url = reverse('admin:example_apirequest_changelist')

    def _detail_url(self, pk):
        return reverse('admin:example_apirequest_archive', args=(quote(pk),))

    def test_lists_database_rows_by_default(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIs(type(response.context['cl']), ChangeList)
        self.assertEqual([row.id for row in response.context['cl'].result_list], [self.recent.id])
        self.assertIsNotNone(response.context['action_form'])

    def test_lists_archived_rows_without_actions(self):
        response = self.client.get(self.url, ARCHIVE)

        cl = response.context['cl']
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(cl, ArchiveChangeList)
        self.assertIsInstance(cl.queryset, ArchiveQuery)
        self.assertEqual(cl.result_count, 3)
        self.assertEqual(cl.full_result_count, 3)
        self.assertIsNone(response.context['action_form'])
        self.assertContains(response, self._detail_url(self.old_requests[0].pk))

    def test_applies_fk_filter_and_search_in_the_archive(self):
        response = self.client.get(self.url, {**ARCHIVE, 'customer__id__exact': self.customer.id, 'q': 'document'})

        self.assertEqual([row.id for row in response.context['cl'].result_list], [self.old_requests[0].id])

    def test_warns_about_filter_and_search_fields_outside_the_archive(self):
        response = self.client.get(
            self.url, {**ARCHIVE, 'duration': '00:00:01', 'headers__isnull': 'True', 'q': 'timeout'}
        )

        messages = [str(message) for message in response.context['messages']]
        self.assertIn('Filters unavailable in the archive were ignored: duration, headers__isnull.', messages)
        self.assertIn('The archive search does not include the fields: headers, customer__name.', messages)
        self.assertEqual(response.context['cl'].result_count, 1)

    def test_removes_custom_filters_in_the_archive(self):
        database_filters = [str(spec.title) for spec in self.client.get(self.url).context['cl'].filter_specs]
        archive_filters = [str(spec.title) for spec in self.client.get(self.url, ARCHIVE).context['cl'].filter_specs]

        self.assertIn('has body', database_filters)
        self.assertNotIn('has body', archive_filters)
        self.assertIn('data source', archive_filters)

    def test_keeps_the_admin_date_filter(self):
        response = self.client.get(self.url, ARCHIVE)

        filters = [spec for spec in response.context['cl'].filter_specs if getattr(spec, 'field_path', None)]
        self.assertIn(DateFieldListFilter, [type(spec) for spec in filters if spec.field_path == 'requested_at'])

    def test_list_display_fk_comes_from_prefetch(self):
        response = self.client.get(self.url, {**ARCHIVE, 'customer__id__exact': self.customer.id})

        row = response.context['cl'].result_list[0]
        self.assertEqual(row._state.fields_cache['customer'], self.customer)

    def test_archived_row_detail(self):
        response = self.client.get(self._detail_url(self.old_requests[0].pk))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'https://api.alpha.example/bookings')
        self.assertContains(response, 'Alice')

    def test_detail_of_row_outside_the_archive_returns_404(self):
        self.assertEqual(self.client.get(self._detail_url(uuid4())).status_code, 404)

    def test_unreadable_file_shows_the_error_without_the_advice_to_index(self):
        get_storage().save('apirequest/20250101T000000_broken.parquet', io.BytesIO(b'not a parquet file'))

        with self.assertLogs('parquet_archive.admin', level='ERROR'):
            response = self.client.get(self.url, ARCHIVE)

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'Some archive files of this table could not be read.',
            [str(message) for message in response.context['messages']]
        )

    def test_admin_in_brazilian_portuguese(self):
        with self.settings(LANGUAGE_CODE='pt-br'):
            response = self.client.get(self.url, ARCHIVE)
            detail = self.client.get(self._detail_url(self.old_requests[0].pk))

        messages = [str(message) for message in response.context['messages']]
        self.assertIn('Exibindo registros do backup frio (arquivos parquet), somente para consulta.', messages)
        self.assertContains(response, 'origem dos dados')
        self.assertContains(detail, 'Registro lido do backup frio (arquivo parquet).')


class ArchiveAdminIndexTest(SuperuserMixin, ArchivedRequestsMixin, TestCase):
    """Table with index field: the requests of months 4 and 5 are in one file and that of month 6 in another."""

    def setUp(self):
        super().setUp()
        self.url = reverse('admin:example_apirequest_changelist')
        self.days = [timezone.localdate(request.requested_at) for request in self.old_requests]
        self.newest_file, self.oldest_file = ArchiveFile.objects.order_by('-max_date').values_list('path', flat=True)

    def _period(self, start, end):
        # NOTE: In the format of the links of the day filter
        field = ApiRequest._meta.get_field('requested_at')
        return {
            **ARCHIVE,
            'requested_at__gte': str(_start_of_day(field, start)),
            'requested_at__lt': str(_start_of_day(field, end + timedelta(days=1))),
        }

    def _ids(self, response):
        return [row.id for row in response.context['cl'].result_list]

    def _messages(self, response):
        return [str(message) for message in response.context['messages']]

    def _read_paths(self, read):
        return {source for call in read.call_args_list for source in call.args[1]}

    def test_without_date_filter_lists_every_row_sorted_by_the_index(self):
        response = self.client.get(self.url, ARCHIVE)

        cl = response.context['cl']
        self.assertIsInstance(cl.queryset, IndexedQuery)
        self.assertEqual(self._ids(response), [request.id for request in reversed(self.old_requests)])
        self.assertEqual((cl.result_count, cl.full_result_count), (3, 3))

    def test_counts_by_the_index_and_reads_only_the_files_of_the_page(self):
        with patch.object(ApiRequestAdmin, 'list_per_page', 1), \
                patch.object(ArchiveFolder, 'read', autospec=True, side_effect=ArchiveFolder.read) as read:
            response = self.client.get(self.url, ARCHIVE)

        self.assertEqual(self._ids(response), [self.old_requests[2].id])
        self.assertEqual(response.context['cl'].result_count, 3)
        self.assertEqual(self._read_paths(read), {self.newest_file})

    def test_period_filter_reads_only_its_files(self):
        with patch.object(ArchiveFolder, 'read', autospec=True, side_effect=ArchiveFolder.read) as read:
            response = self.client.get(self.url, self._period(self.days[0], self.days[0]))

        self.assertIsInstance(response.context['cl'].queryset, IndexedQuery)
        self.assertEqual(self._ids(response), [self.old_requests[0].id])
        self.assertEqual(self._read_paths(read), {self.oldest_file})
        self.assertEqual(response.context['cl'].archive_period, (self.days[0], self.days[0]))

    def test_search_reads_the_files_of_the_period(self):
        with patch.object(ArchiveFolder, 'read', autospec=True, side_effect=ArchiveFolder.read) as read:
            response = self.client.get(self.url, {**self._period(self.days[1], self.days[2]), 'q': 'beta'})

        self.assertIsInstance(response.context['cl'].queryset, ArchiveQuery)
        self.assertEqual(self._ids(response), [self.old_requests[2].id, self.old_requests[1].id])
        self.assertEqual(self._read_paths(read), {self.newest_file})

    def test_sorting_by_another_column_reads_the_files(self):
        status_code = ApiRequestAdmin.list_display.index('status_code')

        response = self.client.get(self.url, {**ARCHIVE, 'o': str(status_code)})

        self.assertIsInstance(response.context['cl'].queryset, ArchiveQuery)
        self.assertEqual([row.status_code for row in response.context['cl'].result_list], [200, 500, None])

    @override_settings(PARQUET_ARCHIVE_MAX_FILES=1)
    def test_more_files_than_the_limit_shows_the_most_recent_with_a_link_to_continue(self):
        response = self.client.get(self.url, {**ARCHIVE, 'method__exact': 'post'})

        self.assertEqual(self._ids(response), [self.old_requests[2].id])
        message = next(message for message in self._messages(response) if 'Continue before' in message)
        continue_url = html.unescape(re.search(r'href="([^"]+)"', message).group(1))
        self.assertIn('requested_at__lt=', continue_url)

        older = self.client.get(f'{self.url}{continue_url}')

        self.assertEqual(self._ids(older), [self.old_requests[0].id])
        self.assertFalse([message for message in self._messages(older) if 'Continue before' in message])

    INDEXING = (
        'The archive files of this table are being indexed, so the listing may be missing some of them for a few '
        'minutes.'
    )

    def _get_queueing_the_indexing(self):
        with patch('parquet_archive.admin.index_files_task') as index_files_task, \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.get(self.url, ARCHIVE)
        return response, index_files_task.delay

    def test_indexed_table_does_not_queue_the_indexing(self):
        response, delay = self._get_queueing_the_indexing()

        delay.assert_not_called()
        self.assertNotIn(self.INDEXING, self._messages(response))

    def test_table_never_indexed_queues_the_indexing_once(self):
        ArchivePolicy.objects.update(indexed_at=None)

        first, first_delay = self._get_queueing_the_indexing()
        second, second_delay = self._get_queueing_the_indexing()

        first_delay.assert_called_once_with(model_names=['apirequest'])
        second_delay.assert_not_called()
        self.assertIn(self.INDEXING, self._messages(first))
        self.assertIn(self.INDEXING, self._messages(second))

    def test_broker_that_refuses_the_indexing_does_not_break_the_page(self):
        ArchivePolicy.objects.update(indexed_at=None)

        # NOTE: The callbacks run when captureOnCommitCallbacks exits, so the log is captured outside of it
        with self.assertLogs('parquet_archive.admin', level='ERROR'), \
                patch('parquet_archive.admin.index_files_task') as index_files_task, \
                self.captureOnCommitCallbacks(execute=True):
            index_files_task.delay.side_effect = ConnectionError('broker unavailable')
            response = self.client.get(self.url, ARCHIVE)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self._ids(response)), 3)
        self.assertIsNone(ArchivePolicy.objects.get().indexing_requested_at)

    def test_indexing_not_finished_after_an_hour_is_queued_again(self):
        ArchivePolicy.objects.update(indexed_at=None, indexing_requested_at=timezone.now() - timedelta(hours=2))

        _response, delay = self._get_queueing_the_indexing()

        delay.assert_called_once_with(model_names=['apirequest'])

    def test_files_indexed_by_another_field_queue_the_indexing_again(self):
        ArchivePolicy.objects.update(indexing_requested_at=timezone.now() - timedelta(minutes=5))
        ArchiveFile.objects.filter(path=self.oldest_file).update(field='status_code')

        response, delay = self._get_queueing_the_indexing()

        delay.assert_called_once_with(model_names=['apirequest'])
        self.assertEqual(self._ids(response), [self.old_requests[2].id, self.old_requests[1].id])
        self.assertIn(self.INDEXING, self._messages(response))

    def test_warns_about_the_files_without_the_index_field(self):
        ArchiveFile.objects.create(
            content_type=ContentType.objects.get_for_model(ApiRequest), path='20250101T000000_old.parquet', rows=2,
            field='requested_at',
        )

        response, delay = self._get_queueing_the_indexing()

        delay.assert_not_called()
        self.assertIn('1 archive file of this table has no requested at and is not listed.', self._messages(response))

    def test_without_ordering_of_the_admin_lists_by_the_index_field(self):
        with patch.object(ApiRequestAdmin, 'ordering', None):
            response = self.client.get(self.url, ARCHIVE)

        self.assertIsInstance(response.context['cl'].queryset, IndexedQuery)
        self.assertEqual(self._ids(response), [request.id for request in reversed(self.old_requests)])

    def _remove_from_the_storage(self, path):
        get_storage().delete(f'apirequest/{path}')

    def test_file_removed_from_the_storage_shows_an_error_until_the_next_indexing(self):
        self._remove_from_the_storage(self.newest_file)

        with self.assertLogs('parquet_archive.admin', level='ERROR'):
            response = self.client.get(self.url, ARCHIVE)
        index_files()
        after = self.client.get(self.url, ARCHIVE)

        error = (
            'Some archive files of this table could not be read, e.g. because they were removed from the storage. '
            'In that case, index the files again in the archive policies.'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._ids(response), [])
        self.assertEqual(response.context['cl'].full_result_count, 3)
        self.assertIn(error, self._messages(response))
        self.assertEqual(self._ids(after), [self.old_requests[0].id])
        self.assertNotIn(error, self._messages(after))

    def test_pages_that_do_not_read_the_removed_file_still_list_their_rows(self):
        self._remove_from_the_storage(self.newest_file)

        response = self.client.get(self.url, self._period(self.days[0], self.days[0]))

        self.assertEqual(self._ids(response), [self.old_requests[0].id])

    def test_file_that_fails_only_when_the_page_is_read_shows_the_error(self):
        # NOTE: On S3 the files are read only when the page is counted, after the query is built
        read = ArchiveFolder.read

        def read_missing_file(folder, sources):
            return pl.scan_parquet(f'{uuid4()}/missing.parquet') if sources else read(folder, sources)

        with patch.object(ArchiveFolder, 'read', autospec=True, side_effect=read_missing_file), \
                self.assertLogs('parquet_archive.admin', level='ERROR'):
            response = self.client.get(self.url, {**ARCHIVE, 'q': 'beta'})

        self.assertEqual(self._ids(response), [])
        self.assertTrue([message for message in self._messages(response) if 'could not be read' in message])

    def test_search_with_a_file_removed_from_the_storage_shows_the_error(self):
        self._remove_from_the_storage(self.oldest_file)

        with self.assertLogs('parquet_archive.admin', level='ERROR'):
            response = self.client.get(self.url, {**ARCHIVE, 'q': 'beta'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._ids(response), [])
        self.assertTrue([message for message in self._messages(response) if 'could not be read' in message])

    def test_detail_of_a_row_whose_file_was_removed_goes_back_to_the_list_with_the_error(self):
        cl = self.client.get(self.url, ARCHIVE).context['cl']
        detail_url = cl.url_for_result(cl.result_list[2])
        self._remove_from_the_storage(self.oldest_file)

        with self.assertLogs('parquet_archive.admin', level='ERROR'):
            response = self.client.get(detail_url, follow=True)

        self.assertEqual(response.redirect_chain[0][1], 302)
        self.assertEqual(response.context['cl'].archive_period, (self.days[0], self.days[0]))
        self.assertIn(
            'The archive file of the row could not be read, e.g. because it was removed from the storage. '
            'In that case, index the files again in the archive policies.',
            self._messages(response)
        )

    def _write_into_the_model_folder(self, pk, requested_at):
        columns = {
            'id': [str(pk)],
            'requested_at': [timezone.make_aware(requested_at).astimezone(dt_timezone.utc)],
            'success': [False],
        }
        file = io.BytesIO()
        pl.DataFrame(columns).write_parquet(file)
        get_storage().save(f'apirequest/{uuid4()}.parquet', file)

    def test_files_outside_the_index_only_show_up_after_indexing(self):
        old = uuid4()
        self._write_into_the_model_folder(old, datetime.combine(self.days[0] - timedelta(days=10), time(12)))

        before = self.client.get(self.url, ARCHIVE)
        index_files()
        after = self.client.get(self.url, ARCHIVE)

        self.assertNotIn(old, self._ids(before))
        self.assertEqual(self._ids(after)[-1], old)

    def test_day_filter_lists_the_months_and_the_days_of_the_month_shown_that_the_files_cover(self):
        response = self.client.get(self.url, self._period(self.days[2], self.days[2]))

        cl = response.context['cl']
        filters = [spec for spec in cl.filter_specs if getattr(spec, 'field_path', None) == 'requested_at']
        self.assertEqual([type(list_filter) for list_filter in filters], [ArchiveDayFilter])
        choices = list(filters[0].choices(cl))
        # NOTE: The newest file has the requests of months 5 and 4, so it covers every day between them
        covered = [self.days[1] + timedelta(days=days) for days in range((self.days[2] - self.days[1]).days + 1)]
        shown_month = [day for day in reversed(covered) if day.month == self.days[2].month]
        expected = [
            'All', date_format(self.days[2], 'YEAR_MONTH_FORMAT'), *(short_date(day) for day in shown_month),
            date_format(self.days[1], 'YEAR_MONTH_FORMAT'), date_format(self.days[0], 'YEAR_MONTH_FORMAT'),
        ]
        self.assertEqual([choice['display'] for choice in choices], expected)
        selected = [choice['display'] for choice in choices if choice['selected']]
        self.assertEqual(selected, [short_date(self.days[2])])

    def test_day_filter_without_period_selects_all_and_lists_only_the_months(self):
        cl = self.client.get(self.url, ARCHIVE).context['cl']
        day_filter = next(spec for spec in cl.filter_specs if isinstance(spec, ArchiveDayFilter))

        choices = list(day_filter.choices(cl))

        self.assertEqual([choice['display'] for choice in choices], ['All'] + [
            date_format(day, 'YEAR_MONTH_FORMAT') for day in reversed(self.days)
        ])
        self.assertEqual(choices[0]['selected'], True)

    def test_month_choice_of_the_day_filter_shows_the_month(self):
        cl = self.client.get(self.url, ARCHIVE).context['cl']
        day_filter = next(spec for spec in cl.filter_specs if isinstance(spec, ArchiveDayFilter))
        display = date_format(self.days[1], 'YEAR_MONTH_FORMAT')
        previous_month = next(choice for choice in day_filter.choices(cl) if choice['display'] == display)

        response = self.client.get(f'{self.url}{previous_month["query_string"]}')

        self.assertEqual(self._ids(response), [self.old_requests[1].id])

    def test_database_choice_clears_the_archive_filters(self):
        filters = {**self._period(self.days[0], self.days[0]), 'method__exact': 'post'}
        cl = self.client.get(self.url, filters).context['cl']
        source = next(spec for spec in cl.filter_specs if isinstance(spec, DataSourceFilter))
        database = next(source.choices(cl))

        response = self.client.get(f'{self.url}{database["query_string"]}')

        self.assertEqual(self._ids(response), [self.recent.id])

    def test_detail_link_carries_the_date_and_the_detail_goes_back_to_its_day(self):
        cl = self.client.get(self.url, ARCHIVE).context['cl']
        detail_url = cl.url_for_result(cl.result_list[2])
        url = reverse('admin:example_apirequest_archive', args=(quote(self.old_requests[0].pk),))
        at = self.old_requests[0].requested_at.astimezone(dt_timezone.utc).isoformat()
        self.assertEqual(detail_url, f'{url}?{urlencode({"at": at})}')

        with patch.object(ArchiveFolder, 'read', autospec=True, side_effect=ArchiveFolder.read) as read:
            detail = self.client.get(detail_url)

        self.assertContains(detail, 'https://api.alpha.example/bookings')
        self.assertEqual(self._read_paths(read), {self.oldest_file})
        back = self.client.get(detail.context['changelist_url'])
        self.assertEqual(self._ids(back), [self.old_requests[0].id])
        self.assertEqual(back.context['cl'].archive_period, (self.days[0], self.days[0]))

    def test_detail_without_or_with_invalid_date_returns_404(self):
        detail_url = reverse('admin:example_apirequest_archive', args=(quote(self.old_requests[0].pk),))

        self.assertEqual(self.client.get(detail_url).status_code, 404)
        self.assertEqual(self.client.get(detail_url, {'at': 'yesterday'}).status_code, 404)
        self.assertEqual(self.client.get(detail_url, {'at': '9999-12-31T23:59:59.999999'}).status_code, 404)

    def test_day_bounds_in_project_time_zone_even_with_another_active_time_zone(self):
        night = uuid4()
        self._write_into_the_model_folder(night, datetime.combine(self.days[1], time(23, 30)))
        index_files()
        period = self._period(self.days[1], self.days[1])

        with timezone.override('UTC'):
            response = self.client.get(self.url, period)

        self.assertIn(night, self._ids(response))
        self.assertEqual(response.context['cl'].archive_period, (self.days[1], self.days[1]))

    def test_extreme_date_in_filter_is_an_invalid_parameter(self):
        response = self.client.get(self.url, {**ARCHIVE, 'requested_at__lte': '9999-12-31 23:59:59.999999+00:00'})

        self.assertEqual(response.status_code, 302)
        self.assertIn('e=1', response.url)

    def test_without_time_zone_support_the_day_bound_is_naive(self):
        field = ApiRequest._meta.get_field('requested_at')

        with self.settings(USE_TZ=False):
            self.assertEqual(_start_of_day(field, date(2026, 6, 15)), datetime(2026, 6, 15))


class ArchiveAdminDateIndexTest(SuperuserMixin, ArchiveStorageMixin, TestCase):
    """Index field that is a DateField: one payment per file, on 3 days."""

    def setUp(self):
        super().setUp()
        create_policy(Payment, config={
            'rows_per_file': 1,
            'filter': {'fields': ['paid_on'], 'keep_for': 1, 'unit': 'days'},
            'ordering': ['-paid_on'],
            'schema': {'id': 'Int64', 'paid_on': 'Date', 'description': 'String'},
        })
        self.payments = [
            Payment.objects.create(paid_on=date(2025, 1, day), description=f'payment {day}') for day in (10, 11, 12)
        ]
        archive_tables()
        index_files()
        self.url = reverse('admin:example_payment_changelist')

    @override_settings(PARQUET_ARCHIVE_MAX_FILES=1)
    def test_link_to_continue_goes_through_the_older_days(self):
        url, ids = f'{self.url}?{urlencode({**ARCHIVE, "q": "payment"})}', []
        for _ in range(len(self.payments) + 1):
            response = self.client.get(url)
            ids += [row.id for row in response.context['cl'].result_list]
            message = next((str(m) for m in response.context['messages'] if 'Continue before' in str(m)), None)
            if message is None:
                break
            query_string = re.search(r'href="([^"]+)"', message).group(1)
            url = f'{self.url}{html.unescape(query_string)}'

        self.assertEqual(ids, [payment.id for payment in reversed(self.payments)])

    def test_detail_reads_the_file_of_the_day_of_the_row(self):
        cl = self.client.get(self.url, ARCHIVE).context['cl']

        detail = self.client.get(cl.url_for_result(cl.result_list[0]))

        self.assertContains(detail, 'payment 12')
