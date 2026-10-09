from unittest.mock import patch

from parameterized import parameterized

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from parquet_archive.forms import ArchivePolicyForm
from parquet_archive.schema import polars_type_for_field
from tests.base import API_REQUEST_CONFIG, create_policy
from tests.example.models import ApiRequest, Customer


class ArchivePolicyFormTest(TestCase):

    def _form_data(self, model=ApiRequest, **kwargs):
        return {
            'content_type': ContentType.objects.get_for_model(model).pk,
            'active': True,
            'date_fields': ['requested_at'],
            'keep_for': 3,
            'unit': 'months',
            'rows_per_file': 3000,
            'partition_field': 'requested_at',
            'ordering': '-requested_at',
            'columns': ['requested_at', 'status_code'],
            **kwargs
        }

    def _choices(self, form, field):
        return [value for value, _ in form.fields[field].choices]

    def test_saves_config_with_schema_derived_from_columns_and_primary_key(self):
        form = ArchivePolicyForm(data=self._form_data())

        self.assertTrue(form.is_valid(), form.errors)
        policy = form.save()

        self.assertEqual(policy.content_type, ContentType.objects.get_for_model(ApiRequest))
        self.assertEqual(policy.config, {
            'rows_per_file': 3000,
            'partition_field': 'requested_at',
            'filter': {'fields': ['requested_at'], 'keep_for': 3, 'unit': 'months'},
            'ordering': ['-requested_at'],
            'schema': {'id': 'String', 'requested_at': 'Datetime', 'status_code': 'Int64'},
        })

    def test_form_reproduces_saved_config(self):
        row_filter = API_REQUEST_CONFIG['filter']
        form = ArchivePolicyForm(data=self._form_data(
            date_fields=row_filter['fields'],
            keep_for=row_filter['keep_for'],
            unit=row_filter['unit'],
            rows_per_file=API_REQUEST_CONFIG['rows_per_file'],
            partition_field=API_REQUEST_CONFIG['partition_field'],
            ordering=','.join(API_REQUEST_CONFIG['ordering']),
            columns=[field for field in API_REQUEST_CONFIG['schema'] if field != 'id'],
        ))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().config, API_REQUEST_CONFIG)

    def test_without_model_does_not_list_fields(self):
        form = ArchivePolicyForm()

        for field in ('date_fields', 'partition_field', 'ordering', 'columns'):
            with self.subTest(field=field):
                self.assertEqual(self._choices(form, field), [])
                self.assertEqual(form.fields[field].help_text, 'Choose the model to list its fields.')

    def test_creation_lists_fields_of_the_chosen_model_already_selected(self):
        form = ArchivePolicyForm(initial={'content_type': str(ContentType.objects.get_for_model(ApiRequest).pk)})

        supported_columns = [
            'requested_at', 'url', 'method', 'body', 'status_code', 'amount', 'success', 'reference_date',
            'headers', 'duration', 'customer_id', 'retry_of_id',
        ]
        self.assertEqual(self._choices(form, 'columns'), supported_columns)
        self.assertEqual(form.initial['columns'], supported_columns)
        self.assertIn('Not supported in the archive (encrypted fields, custom value objects...): external_code.',
                      form.fields['columns'].help_text)
        self.assertEqual(
            self._choices(form, 'date_fields'),
            ['requested_at', 'reference_date', 'customer__registered_at', 'retry_of__requested_at',
             'retry_of__reference_date']
        )
        self.assertEqual(self._choices(form, 'partition_field'), ['', 'requested_at', 'reference_date'])
        self.assertIn('-requested_at', self._choices(form, 'ordering'))

    def test_partition_field_is_always_saved(self):
        form = ArchivePolicyForm(data=self._form_data(partition_field='reference_date', columns=['status_code']))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.save().config['schema'], {'id': 'String', 'status_code': 'Int64', 'reference_date': 'Date'}
        )

    def test_daily_folders_are_optional(self):
        form = ArchivePolicyForm(data=self._form_data(partition_field='', columns=['status_code']))

        self.assertTrue(form.is_valid(), form.errors)
        config = form.save().config
        self.assertNotIn('partition_field', config)
        self.assertEqual(config['schema'], {'id': 'String', 'status_code': 'Int64'})

    def test_daily_folders_do_not_offer_date_field_without_archive_type(self):
        def no_type_for_reference_date(field):
            return None if field.attname == 'reference_date' else polars_type_for_field(field)

        with patch('parquet_archive.forms.polars_type_for_field', side_effect=no_type_for_reference_date):
            form = ArchivePolicyForm(initial={'content_type': str(ContentType.objects.get_for_model(ApiRequest).pk)})

        self.assertEqual(self._choices(form, 'partition_field'), ['', 'requested_at'])

    @parameterized.expand([('change', 'reference_date'), ('clear', '')])
    def test_editing_does_not_change_daily_folders_already_set(self, _, partition_field):
        form = ArchivePolicyForm(instance=create_policy(), data=self._form_data(partition_field=partition_field))

        self.assertTrue(form.fields['partition_field'].disabled)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().config['partition_field'], 'requested_at')

    def test_editing_without_daily_folders_allows_setting_them(self):
        policy = create_policy()
        del policy.config['partition_field']
        policy.save()

        form = ArchivePolicyForm(instance=policy, data=self._form_data(partition_field='requested_at'))

        self.assertFalse(form.fields['partition_field'].disabled)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().config['partition_field'], 'requested_at')

    def test_editing_keeps_saved_partition_field_outside_the_choices(self):
        def no_type_for_requested_at(field):
            return None if field.attname == 'requested_at' else polars_type_for_field(field)

        with patch('parquet_archive.forms.polars_type_for_field', side_effect=no_type_for_requested_at):
            form = ArchivePolicyForm(instance=create_policy(), data=self._form_data(columns=['status_code']))

        self.assertIn('requested_at', self._choices(form, 'partition_field'))
        self.assertTrue(form.is_valid(), form.errors)

    @parameterized.expand([('url',), ('customer__registered_at',)])
    def test_rejects_partition_field_that_is_not_a_date_of_the_model_itself(self, partition_field):
        form = ArchivePolicyForm(data=self._form_data(partition_field=partition_field))

        self.assertFalse(form.is_valid())
        self.assertIn('partition_field', form.errors)

    def test_rejects_column_without_supported_type(self):
        form = ArchivePolicyForm(data=self._form_data(columns=['external_code']))

        self.assertFalse(form.is_valid())
        self.assertIn('external_code', form.errors['columns'][0])

    @parameterized.expand([('url',), ('missing_field',), ('requested_at__field',)])
    def test_rejects_date_field_outside_the_choices(self, date_field):
        form = ArchivePolicyForm(data=self._form_data(date_fields=[date_field]))

        self.assertFalse(form.is_valid())
        self.assertIn(date_field, form.errors['date_fields'][0])

    def test_rejects_ordering_by_missing_field(self):
        form = ArchivePolicyForm(data=self._form_data(ordering='-missing_field'))

        self.assertFalse(form.is_valid())
        self.assertIn('-missing_field', form.errors['ordering'][0])

    def test_rejects_model_with_relations_blocking_delete(self):
        form = ArchivePolicyForm(data=self._form_data(
            Customer, date_fields=['registered_at'], partition_field='registered_at', ordering='', columns=['name']
        ))

        self.assertFalse(form.is_valid())
        self.assertIn('example.ApiRequest.customer (PROTECT)', form.errors['content_type'][0])

    def test_editing_fills_fields_with_the_saved_config(self):
        form = ArchivePolicyForm(instance=create_policy())

        self.assertEqual(form.initial['date_fields'], ['requested_at'])
        self.assertEqual(form.initial['keep_for'], 3)
        self.assertEqual(form.initial['unit'], 'months')
        self.assertEqual(form.initial['rows_per_file'], 2)
        self.assertEqual(form.initial['partition_field'], 'requested_at')
        self.assertEqual(form.initial['ordering'], '-requested_at')
        self.assertEqual(
            form.initial['columns'], [field for field in API_REQUEST_CONFIG['schema'] if field != 'id']
        )

    def test_editing_keeps_saved_date_field_and_ordering_outside_the_choices(self):
        policy = create_policy(config={
            **API_REQUEST_CONFIG,
            'filter': {'fields': ['retry_of__customer__registered_at'], 'keep_for': 1, 'unit': 'years'},
            'ordering': ['requested_at', '-status_code'],
        })

        form = ArchivePolicyForm(instance=policy)

        self.assertIn('retry_of__customer__registered_at', self._choices(form, 'date_fields'))
        self.assertIn('requested_at,-status_code', self._choices(form, 'ordering'))
