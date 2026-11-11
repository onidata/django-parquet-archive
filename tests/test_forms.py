from parameterized import parameterized

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from parquet_archive.forms import ArchivePolicyForm
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
            ordering=','.join(API_REQUEST_CONFIG['ordering']),
            columns=[field for field in API_REQUEST_CONFIG['schema'] if field != 'id'],
        ))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().config, API_REQUEST_CONFIG)

    def test_without_model_does_not_list_fields(self):
        form = ArchivePolicyForm()

        for field in ('date_fields', 'ordering', 'columns'):
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
        self.assertIn('-requested_at', self._choices(form, 'ordering'))

    def test_ordering_field_is_always_saved(self):
        form = ArchivePolicyForm(data=self._form_data(ordering='-reference_date', columns=['status_code']))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.save().config['schema'], {'id': 'String', 'status_code': 'Int64', 'reference_date': 'Date'}
        )

    def test_without_ordering_saves_only_the_columns(self):
        form = ArchivePolicyForm(data=self._form_data(ordering='', columns=['status_code']))

        self.assertTrue(form.is_valid(), form.errors)
        config = form.save().config
        self.assertEqual(config['ordering'], [])
        self.assertEqual(config['schema'], {'id': 'String', 'status_code': 'Int64'})

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
            Customer, date_fields=['registered_at'], ordering='', columns=['name']
        ))

        self.assertFalse(form.is_valid())
        self.assertIn('example.ApiRequest.customer (PROTECT)', form.errors['content_type'][0])

    def test_editing_fills_fields_with_the_saved_config(self):
        form = ArchivePolicyForm(instance=create_policy())

        self.assertEqual(form.initial['date_fields'], ['requested_at'])
        self.assertEqual(form.initial['keep_for'], 3)
        self.assertEqual(form.initial['unit'], 'months')
        self.assertEqual(form.initial['rows_per_file'], 2)
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
