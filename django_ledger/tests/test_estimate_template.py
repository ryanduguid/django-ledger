from decimal import Decimal
from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from django_ledger.models import CustomerModel, EntityModel, EstimateModel


class EstimateSummaryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}
        self.current_id = None

    def handle_starttag(self, tag, attrs):
        element_id = dict(attrs).get('id', '')
        if element_id.startswith('djl-cj-detail-estimated-'):
            self.current_id = element_id
            self.values.setdefault(element_id, []).append('')

    def handle_data(self, data):
        if self.current_id:
            self.values[self.current_id][-1] += data

    def handle_endtag(self, tag):
        if tag == 'p':
            self.current_id = None


class EstimateTemplateTests(TestCase):
    def test_summary_ids_select_distinct_estimate_values(self):
        user = get_user_model().objects.create_user(username='estimate-owner')
        entity = EntityModel.create_entity('Estimate Entity', False, user, 1)
        customer = CustomerModel.objects.create(entity_model=entity, customer_name='Estimate Customer')
        estimate = entity.create_estimate('Example Estimate', EstimateModel.CONTRACT_TERMS_FIXED, customer)
        estimate.revenue_estimate = Decimal('25.00')
        estimate.labor_estimate = Decimal('10.00')
        estimate.save()
        self.client.force_login(user)

        response = self.client.get(
            reverse(
                'django_ledger:customer-estimate-detail',
                kwargs={
                    'entity_slug': entity.slug,
                    'ce_pk': estimate.pk,
                },
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'django_ledger/estimate/estimate_detail.html')
        summary = EstimateSummaryParser()
        summary.feed(response.content.decode())
        expected = {'revenue': '25.00', 'cost': '10.00', 'profit': '15.00', 'gross-margin': '150.00%'}
        self.assertEqual(set(summary.values), {f'djl-cj-detail-estimated-{name}' for name in expected})
        for name, value in expected.items():
            with self.subTest(summary=name):
                selected = summary.values[f'djl-cj-detail-estimated-{name}']
                self.assertEqual(len(selected), 1)
                self.assertIn(value, selected[0])
