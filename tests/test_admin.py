import io
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from unittest.mock import ANY, patch
from uuid import uuid4

import polars as pl

from django.contrib.admin import DateFieldListFilter
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.admin.utils import quote
from django.contrib.admin.views.main import ChangeList
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from parquet_archive.admin import ArchiveChangeList, ArchiveDayFilter, DataSourceFilter, _start_of_day
from parquet_archive.conf import get_storage
from parquet_archive.models import ArchivePolicy
from parquet_archive.query import ArchiveFolder
from tests.base import API_REQUEST_CONFIG, ArchivedRequestsMixin, create_policy
from tests.example.models import ApiRequest, Order


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
            'partition_field': 'requested_at',
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
            'partition_field': 'requested_at',
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
            'partition_field': 'requested_at',
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
    """Table without day subfolders, where the changelist reads every file."""

    archive_config = {**API_REQUEST_CONFIG, 'partition_field': None}

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

    def test_admin_in_brazilian_portuguese(self):
        with self.settings(LANGUAGE_CODE='pt-br'):
            response = self.client.get(self.url, ARCHIVE)
            detail = self.client.get(self._detail_url(self.old_requests[0].pk))

        messages = [str(message) for message in response.context['messages']]
        self.assertIn('Exibindo registros do backup frio (arquivos parquet), somente para consulta.', messages)
        self.assertContains(response, 'origem dos dados')
        self.assertContains(detail, 'Registro lido do backup frio (arquivo parquet).')


def midnight(day):
    return str(timezone.make_aware(datetime.combine(day, time.min)))


class ArchiveAdminByDayTest(SuperuserMixin, ArchivedRequestsMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.url = reverse('admin:example_apirequest_changelist')
        self.days = [timezone.localdate(request.requested_at) for request in self.old_requests]

    def _period(self, start, end):
        return {
            **ARCHIVE,
            'requested_at__gte': midnight(start),
            'requested_at__lt': midnight(end + timedelta(days=1)),
        }

    def _ids(self, response):
        return [row.id for row in response.context['cl'].result_list]

    def _messages(self, response):
        return [str(message) for message in response.context['messages']]

    def test_without_date_filter_shows_the_most_recent_day(self):
        response = self.client.get(self.url, ARCHIVE)

        self.assertEqual(self._ids(response), [self.old_requests[2].id])
        self.assertEqual(response.context['cl'].archive_period, (self.days[2], self.days[2]))
        self.assertIn(
            f'Showing {short_date(self.days[2])}, the most recent day in the archive. '
            'Filter by requested at to see other days.',
            self._messages(response)
        )

    def test_reads_only_the_subfolders_of_the_period(self):
        with patch.object(ArchiveFolder, 'paths', autospec=True, side_effect=ArchiveFolder.paths) as paths:
            response = self.client.get(self.url, self._period(self.days[0], self.days[0]))

        self.assertEqual(self._ids(response), [self.old_requests[0].id])
        paths.assert_called_once_with(ANY, 'requested_at', {self.days[0]})

    def test_period_longer_than_the_limit_shows_the_most_recent_days_with_warning(self):
        with patch.object(ArchiveFolder, 'paths', autospec=True, side_effect=ArchiveFolder.paths) as paths:
            response = self.client.get(self.url, {**ARCHIVE, 'requested_at__gte': midnight(self.days[0])})

        start = self.days[2] - timedelta(days=30)
        period_days = {day for day in self.days if day >= start}
        self.assertNotIn(self.days[0], period_days)
        paths.assert_called_once_with(ANY, 'requested_at', period_days)
        in_period = [request.id for request, day in zip(self.old_requests, self.days) if day in period_days]
        self.assertEqual(sorted(self._ids(response)), sorted(in_period))
        self.assertEqual(response.context['cl'].archive_period, (start, self.days[2]))
        self.assertIn(
            f'The archive shows up to 31 days at a time: showing {short_date(start)} to {short_date(self.days[2])}.',
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

    def test_filters_rows_of_files_without_day_by_the_period(self):
        on_the_day, off_the_day = uuid4(), uuid4()
        self._write_into_the_model_folder(on_the_day, datetime.combine(self.days[2], time(12)))
        self._write_into_the_model_folder(off_the_day, datetime.combine(self.days[2] - timedelta(days=10), time(12)))

        response = self.client.get(self.url, ARCHIVE)

        self.assertEqual(sorted(self._ids(response)), sorted([self.old_requests[2].id, on_the_day]))

    def test_day_filter_lists_the_months_and_the_days_of_the_month_shown(self):
        response = self.client.get(self.url, ARCHIVE)

        cl = response.context['cl']
        filters = [spec for spec in cl.filter_specs if getattr(spec, 'field_path', None) == 'requested_at']
        self.assertEqual([type(list_filter) for list_filter in filters], [ArchiveDayFilter])
        choices = list(filters[0].choices(cl))
        expected = [
            date_format(self.days[2], 'YEAR_MONTH_FORMAT'), short_date(self.days[2]),
            date_format(self.days[1], 'YEAR_MONTH_FORMAT'), date_format(self.days[0], 'YEAR_MONTH_FORMAT'),
        ]
        self.assertEqual([choice['display'] for choice in choices], expected)
        self.assertEqual([choice['selected'] for choice in choices], [False, True, False, False])

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

    def test_detail_link_carries_the_day_and_the_detail_goes_back_to_it(self):
        cl = self.client.get(self.url, ARCHIVE).context['cl']
        detail_url = cl.url_for_result(cl.result_list[0])
        url = reverse('admin:example_apirequest_archive', args=(quote(self.old_requests[2].pk),))
        self.assertEqual(detail_url, f'{url}?day={self.days[2].isoformat()}')

        with patch.object(ArchiveFolder, 'paths', autospec=True, side_effect=ArchiveFolder.paths) as paths:
            detail = self.client.get(detail_url)

        self.assertContains(detail, 'https://api.beta.example/orders')
        paths.assert_called_once_with(ANY, 'requested_at', {self.days[2]})
        back = self.client.get(detail.context['changelist_url'])
        self.assertEqual(self._ids(back), [self.old_requests[2].id])
        self.assertEqual(back.context['cl'].archive_period, (self.days[2], self.days[2]))

    def test_day_bounds_in_project_time_zone_even_with_another_active_time_zone(self):
        night = uuid4()
        self._write_into_the_model_folder(night, datetime.combine(self.days[1], time(23, 30)))
        period = self._period(self.days[1], self.days[1])

        with timezone.override('UTC'):
            response = self.client.get(self.url, period)

        self.assertIn(night, self._ids(response))
        self.assertEqual(response.context['cl'].archive_period, (self.days[1], self.days[1]))

    def test_detail_without_the_day_only_searches_the_model_folder(self):
        detail_url = reverse('admin:example_apirequest_archive', args=(quote(self.old_requests[0].pk),))

        self.assertEqual(self.client.get(detail_url).status_code, 404)
        self.assertEqual(self.client.get(detail_url, {'day': 'yesterday'}).status_code, 404)
        self.assertEqual(self.client.get(detail_url, {'day': '9999-12-31'}).status_code, 404)

    def test_extreme_date_in_filter_is_an_invalid_parameter(self):
        response = self.client.get(self.url, {**ARCHIVE, 'requested_at__lt': '0001-01-01 00:00:00'})

        self.assertEqual(response.status_code, 302)
        self.assertIn('e=1', response.url)

    def test_without_time_zone_support_the_day_bound_is_naive(self):
        field = ApiRequest._meta.get_field('requested_at')

        with self.settings(USE_TZ=False):
            self.assertEqual(_start_of_day(field, date(2026, 6, 15)), datetime(2026, 6, 15))
