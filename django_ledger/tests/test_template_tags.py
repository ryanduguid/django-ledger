from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from django_ledger.models.entity import EntityModel, EntityModelQuerySet
from django_ledger.forms.app_filters import EntityFilterForm
from django_ledger.templatetags.django_ledger import default_entity
from django_ledger.utils import get_default_entity_session_key


class DefaultEntityTagTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='template-first')
        other_user = get_user_model().objects.create_user(username='template-second')
        cls.own_entity = EntityModel.create_entity(
            name='Example One', admin=cls.user, use_accrual_method=False, fy_start_month=1
        )
        cls.second_entity = EntityModel.create_entity(
            name='Example Three', admin=cls.user, use_accrual_method=False, fy_start_month=1
        )
        cls.other_entity = EntityModel.create_entity(
            name='Example Two', admin=other_user, use_accrual_method=False, fy_start_month=1
        )
        cls.managed_entity = EntityModel.create_entity(
            name='Example Four', admin=other_user, use_accrual_method=False, fy_start_month=1
        )
        cls.managed_entity.managers.add(cls.user)
        cls.own_entity.create_chart_of_accounts(assign_as_default=True, coa_name='Example Chart', commit=True)

    def test_session_fallback_preserves_user_scope(self):
        key = get_default_entity_session_key()
        cases = (
            ('missing slot', {}, None),
            ('null slot', {key: None}, None),
            ('missing key', {key: {}}, None),
            ('valid slot', {key: {'entity_uuid': str(self.own_entity.uuid)}}, str(self.own_entity.uuid)),
            ('unpermitted initial', {key: {'entity_uuid': str(self.other_entity.uuid)}}, str(self.other_entity.uuid)),
        )
        expected_pks = list(EntityModel.objects.for_user(user_model=self.user).values_list('pk', flat=True))
        expected_coa_slugs = dict(
            EntityModel.objects.for_user(user_model=self.user).values_list('pk', 'default_coa__slug')
        )
        for label, session, initial in cases:
            with self.subTest(session=label):
                result = default_entity({'user': self.user, 'request': SimpleNamespace(session=session)})
                form = result['default_entity_form']
                self.assertEqual(form.initial.get('entity_model'), initial)
                self.assertEqual(form.form_id, result['form_id'])
                queryset = form.fields['entity_model'].queryset
                self.assertIsInstance(queryset, EntityModelQuerySet)
                with self.assertNumQueries(1):
                    entities = list(queryset)
                    labels = [form.fields['entity_model'].label_from_instance(entity) for entity in entities]
                self.assertEqual(len(labels), 3)
                self.assertEqual([entity.pk for entity in entities], expected_pks)
                self.assertIn(self.own_entity, entities)
                self.assertIn(self.second_entity, entities)
                self.assertIn(self.managed_entity, entities)
                self.assertNotIn(self.other_entity, entities)
                self.assertEqual({entity.pk: entity._default_coa_slug for entity in entities}, expected_coa_slugs)
                self.assertNotIn(f'value="{self.other_entity.uuid}"', str(form['entity_model']))
        bound_form = EntityFilterForm(user_model=self.user, data={'entity_model': str(self.other_entity.uuid)})
        self.assertFalse(bound_form.is_valid())

    def test_form_construction_errors_propagate(self):
        key = get_default_entity_session_key()
        context = {
            'user': self.user,
            'request': SimpleNamespace(session={key: {'entity_uuid': str(self.own_entity.uuid)}}),
        }
        for error_type in (KeyError, TypeError):
            with self.subTest(error_type=error_type):

                def construct(**kwargs):
                    if 'current_entity_uuid' in kwargs:
                        raise error_type('form construction failed')
                    return object()

                with patch(
                    'django_ledger.templatetags.django_ledger.EntityFilterForm', side_effect=construct
                ) as factory:
                    with self.assertRaises(error_type):
                        default_entity(context)
                    factory.assert_called_once()
