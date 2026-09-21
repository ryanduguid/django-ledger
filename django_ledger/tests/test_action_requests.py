"""F-018: request intent, entity boundaries and persisted accounting transitions."""

from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.template import Context, Template
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from django_ledger.models import (
    BillModel, CustomerModel, EntityModel, InvoiceModel, ItemModel,
    ItemTransactionModel, JournalEntryModel, LedgerModel, TransactionModel,
    UnitOfMeasureModel, VendorModel,
)
from django_ledger.urls import bill, invoice, journal_entry, ledger


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'input' and values.get('name') == 'csrfmiddlewaretoken':
            self.token = values['value']


class ActionRequestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='sample-owner')
        cls.outsider = get_user_model().objects.create_user(username='sample-outsider')
        cls.entity = EntityModel.create_entity('Sample Entity', False, cls.user, 1)
        cls.other_entity = EntityModel.create_entity('Sample Other Entity', False, cls.user, 1)
        cls.entity.populate_default_coa(activate_accounts=True)
        cls.ledger = LedgerModel.objects.create(entity=cls.entity, name='Sample Ledger')
        cls.journal = JournalEntryModel.objects.create(ledger=cls.ledger, timestamp=timezone.now())
        accounts = cls.entity.get_default_coa_accounts()
        cls.cash = accounts.filter(role='asset_ca_cash').first()
        cls.income = accounts.filter(role='in_operational').first()
        for account, tx_type in [(cls.cash, 'debit'), (cls.income, 'credit')]:
            TransactionModel.objects.create(
                journal_entry=cls.journal, account=account, tx_type=tx_type, amount=Decimal('100.00'),
            )
        customer = CustomerModel.objects.create(entity_model=cls.entity, customer_name='Sample Customer')
        vendor = VendorModel.objects.create(entity_model=cls.entity, vendor_name='Sample Vendor')
        cls.invoice = cls.entity.create_invoice(customer, terms='on_receipt')
        cls.bill = cls.entity.create_bill(vendor, terms='on_receipt')
        uom = UnitOfMeasureModel.objects.create(entity=cls.entity, name='Sample unit', unit_abbr='unit')
        service = ItemModel(
            entity=cls.entity, name='Sample service', uom=uom,
            item_role=ItemModel.ITEM_ROLE_SERVICE, item_type=ItemModel.ITEM_TYPE_LABOR,
            earnings_account=cls.income, cogs_account=accounts.filter(role__startswith='cogs').first(),
        )
        service.clean()
        service.save()
        expense = cls.entity.create_item_expense('Sample expense', ItemModel.ITEM_TYPE_OTHER, uom)
        for document, item, field in [(cls.invoice, service, 'invoice_model'), (cls.bill, expense, 'bill_model')]:
            ItemTransactionModel.objects.create(
                **{field: document}, item_model=item, quantity=1, unit_cost=Decimal('100.00'),
                total_amount=Decimal('100.00'),
            )
            document.amount_due = Decimal('100.00')
            document.save()

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)

    def routes(self):
        values = {'entity_slug': self.entity.slug, 'ledger_pk': self.ledger.pk,
                  'je_pk': self.journal.pk, 'invoice_pk': self.invoice.pk, 'bill_pk': self.bill.pk}
        for module in [ledger, journal_entry, invoice, bill]:
            for route in module.urlpatterns:
                if getattr(route.callback.view_class, 'action_name', None) or route.callback.view_initkwargs.get('action_name'):
                    kwargs = {name: values[name] for name in route.pattern.converters}
                    yield route.name, reverse('django_ledger:' + route.name, kwargs=kwargs), kwargs

    def snapshot(self):
        return [list(model.objects.order_by('pk').values()) for model in
                [LedgerModel, JournalEntryModel, TransactionModel, InvoiceModel, BillModel]]

    def token(self, client=None):
        client = client or self.client
        client.get(reverse('django_ledger:login'))
        return client.cookies['csrftoken'].value

    def post(self, name, document=None, **data):
        kwargs = {'entity_slug': self.entity.slug}
        if name.startswith('ledger-'):
            kwargs['ledger_pk'] = self.ledger.pk
        elif name.startswith('je-'):
            kwargs.update(ledger_pk=self.ledger.pk, je_pk=self.journal.pk)
        else:
            kind = name.split('-')[0]
            kwargs[kind + '_pk'] = (document or getattr(self, kind)).pk
        return self.client.post(reverse('django_ledger:' + name, kwargs=kwargs),
                                {'csrfmiddlewaretoken': self.token(), **data})

    def test_all_30_routes_get_and_head_preserve_database(self):
        routes = list(self.routes())
        self.assertEqual(len(routes), 30)
        for name, url, _ in routes:
            for method in ['get', 'head']:
                with self.subTest(route=name, method=method):
                    before = self.snapshot()
                    response = getattr(self.client, method)(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(self.snapshot(), before)
                    if method == 'get':
                        self.assertContains(response, 'method="post"')
                        self.assertContains(response, 'name="csrfmiddlewaretoken"')

    def test_all_30_routes_reject_missing_and_invalid_csrf(self):
        self.token()
        for name, url, _ in self.routes():
            for data in [{}, {'csrfmiddlewaretoken': 'invalid'}, {'csrfmiddlewaretoken': 'A' * 32}]:
                with self.subTest(route=name, data=data):
                    before = self.snapshot()
                    self.assertEqual(self.client.post(url, data).status_code, 403)
                    self.assertEqual(self.snapshot(), before)

    def test_all_30_routes_preserve_login_and_entity_scoping(self):
        for user, expected in [(None, 302), (self.outsider, 403)]:
            client = Client(enforce_csrf_checks=True)
            if user:
                client.force_login(user)
            token = self.token(client)
            for name, url, _ in self.routes():
                with self.subTest(route=name, user=user):
                    before = self.snapshot()
                    self.assertEqual(client.post(url, {'csrfmiddlewaretoken': token}).status_code, expected)
                    self.assertEqual(self.snapshot(), before)
        for name, _, kwargs in self.routes():
            with self.subTest(route=name, wrong_entity=True):
                kwargs['entity_slug'] = self.other_entity.slug
                url = reverse('django_ledger:' + name, kwargs=kwargs)
                before = self.snapshot()
                self.assertEqual(self.client.post(url, {'csrfmiddlewaretoken': self.token()}).status_code, 404)
                self.assertEqual(self.snapshot(), before)

    def test_authorised_ledger_and_journal_transitions(self):
        for name, model, field, expected in [
            ('ledger-action-post', self.ledger, 'posted', True),
            ('ledger-action-lock', self.ledger, 'locked', True),
            ('ledger-action-unlock', self.ledger, 'locked', False),
            ('ledger-action-unpost', self.ledger, 'posted', False),
            ('ledger-action-hide', self.ledger, 'hidden', True),
            ('ledger-action-unhide', self.ledger, 'hidden', False),
            ('je-mark-as-locked', self.journal, 'locked', True),
            ('je-mark-as-unlocked', self.journal, 'locked', False),
            ('je-mark-as-locked', self.journal, 'locked', True),
            ('je-mark-as-posted', self.journal, 'posted', True),
            ('je-mark-as-unposted', self.journal, 'posted', False),
            ('ledger-action-lock-journal-entries', self.journal, 'locked', True),
            ('ledger-action-post-journal-entries', self.journal, 'posted', True),
        ]:
            with self.subTest(route=name):
                self.assertEqual(self.post(name).status_code, 302)
                model.refresh_from_db()
                self.assertEqual(getattr(model, field), expected)

    def test_invoice_and_bill_approval_payment_and_balances(self):
        for kind in ['invoice', 'bill']:
            document = getattr(self, kind)
            for state in ['review', 'draft', 'review', 'approved', 'paid']:
                with self.subTest(kind=kind, state=state):
                    self.assertEqual(self.post(f'{kind}-action-mark-as-{state}').status_code, 302)
                    document.refresh_from_db()
                    self.assertEqual(getattr(document, kind + '_status'), 'in_review' if state == 'review' else state)
            document.ledger.refresh_from_db()
            self.assertEqual(document.amount_paid, document.amount_due)
            self.assertTrue(document.ledger.posted)
            self.assertTrue(document.ledger.locked)
            transactions = TransactionModel.objects.filter(journal_entry__ledger=document.ledger)
            self.assertTrue(transactions.exists())
            debit = transactions.filter(tx_type='debit').aggregate(total=Sum('amount'))['total']
            credit = transactions.filter(tx_type='credit').aggregate(total=Sum('amount'))['total']
            self.assertEqual(debit, credit)
            self.assertEqual(debit, Decimal('100.00'))

    def test_invoice_draft_post_persists_transition_date(self):
        InvoiceModel.objects.filter(pk=self.invoice.pk).update(date_draft=date(2020, 1, 1))
        self.assertEqual(self.post('invoice-action-mark-as-review').status_code, 302)
        self.assertEqual(self.post('invoice-action-mark-as-draft').status_code, 302)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.invoice_status, InvoiceModel.INVOICE_STATUS_DRAFT)
        self.assertEqual(self.invoice.date_draft, timezone.localdate())

    def test_invoice_draft_dates_respect_commit(self):
        for supplied, expected in [(None, timezone.localdate()), (date(2020, 2, 3), date(2020, 2, 3)),
                                   (datetime(2020, 4, 5, 12), date(2020, 4, 5))]:
            for commit in [False, True]:
                with self.subTest(supplied=supplied, commit=commit):
                    InvoiceModel.objects.filter(pk=self.invoice.pk).update(
                        invoice_status=InvoiceModel.INVOICE_STATUS_REVIEW, date_draft=date(2020, 1, 1),
                    )
                    self.invoice.refresh_from_db()
                    self.invoice.mark_as_draft(supplied, commit=commit)
                    self.assertEqual(self.invoice.date_draft, expected)
                    self.invoice.refresh_from_db()
                    self.assertEqual(self.invoice.date_draft, expected if commit else date(2020, 1, 1))
                    self.assertEqual(self.invoice.invoice_status, InvoiceModel.INVOICE_STATUS_DRAFT
                                     if commit else InvoiceModel.INVOICE_STATUS_REVIEW)

    def test_invalid_document_state_and_unbalanced_journal_refuse(self):
        for kind in ['invoice', 'bill']:
            before = self.snapshot()
            self.assertEqual(self.post(f'{kind}-action-mark-as-paid').status_code, 302)
            self.assertEqual(self.snapshot(), before)
        self.post('je-mark-as-locked')
        TransactionModel.objects.filter(journal_entry=self.journal, tx_type='credit').update(amount=Decimal('99.00'))
        self.assertEqual(self.post('je-mark-as-posted').status_code, 302)
        self.journal.refresh_from_db()
        self.assertFalse(self.journal.posted)

    def test_document_cancellation(self):
        for kind in ['invoice', 'bill']:
            self.assertEqual(self.post(f'{kind}-action-mark-as-canceled').status_code, 302)
            document = getattr(self, kind)
            document.refresh_from_db()
            self.assertEqual(getattr(document, kind + '_status'), 'canceled')

    def test_document_ledger_controls_and_void(self):
        for kind in ['invoice', 'bill']:
            document = getattr(self, kind)
            self.post(f'{kind}-action-mark-as-review')
            self.post(f'{kind}-action-mark-as-approved')
            for action, locked in [('lock-ledger', True), ('unlock-ledger', False)]:
                self.assertEqual(self.post(f'{kind}-action-{action}').status_code, 302)
                document.ledger.refresh_from_db()
                self.assertEqual(document.ledger.locked, locked)
            self.assertEqual(self.post(f'{kind}-action-force-migrate').status_code, 302)
            self.assertEqual(self.post(f'{kind}-action-mark-as-void').status_code, 302)
            document.refresh_from_db()
            self.assertEqual(getattr(document, kind + '_status'), 'void')
            self.assertEqual(document.amount_paid, Decimal('0.00'))

    def test_rendered_confirmation_token_performs_the_action(self):
        url = reverse('django_ledger:ledger-action-post', kwargs={
            'entity_slug': self.entity.slug, 'ledger_pk': self.ledger.pk,
        })
        response = self.client.get(url)
        parser = FormParser()
        parser.feed(response.content.decode())
        self.assertIsNotNone(parser.token)
        self.assertEqual(self.client.post(url, {'csrfmiddlewaretoken': parser.token}).status_code, 302)
        self.ledger.refresh_from_db()
        self.assertTrue(self.ledger.posted)

    def test_closed_period_prevents_unposting(self):
        self.post('je-mark-as-locked')
        self.post('je-mark-as-posted')
        self.post('ledger-action-post')
        EntityModel.objects.filter(pk=self.entity.pk).update(last_closing_date=timezone.localdate())
        before = self.snapshot()
        for name in ['je-mark-as-unposted', 'je-mark-as-unlocked', 'ledger-action-unpost']:
            with self.subTest(route=name):
                self.assertEqual(self.post(name).status_code, 302)
                self.assertEqual(self.snapshot(), before)

    def test_redirects_stay_on_the_current_host(self):
        for name in ['ledger-action-hide', 'je-mark-as-locked']:
            for target in ['/local-return/', 'https://outside.invalid/', '//outside.invalid/']:
                with self.subTest(route=name, target=target):
                    response = self.post(name, next=target)
                    if target == '/local-return/':
                        self.assertEqual(response.url, target)
                    else:
                        self.assertNotIn('outside.invalid', response.url)

    def test_shared_modal_uses_post_only_when_requested(self):
        template = Template('{% load django_ledger %}{% modal_action_v2 model url "Confirm" "sample-modal" http_method=method %}')
        for method in ['get', 'post']:
            html = template.render(Context({'model': self.invoice, 'url': '/sample-action/', 'method': method,
                                            'csrf_token': self.token()}))
            if method == 'post':
                self.assertIn('method="post"', html)
                self.assertIn('name="csrfmiddlewaretoken"', html)
                self.assertNotIn('<a href="/sample-action/"', html)
            else:
                self.assertIn('<a href="/sample-action/"', html)

    @override_settings(MIDDLEWARE=[
        'django.contrib.sessions.middleware.SessionMiddleware',
        'django.contrib.auth.middleware.AuthenticationMiddleware',
        'django.contrib.messages.middleware.MessageMiddleware',
    ])
    def test_actions_enforce_csrf_without_global_middleware(self):
        before = self.snapshot()
        for name, url, _ in self.routes():
            with self.subTest(route=name):
                self.assertEqual(self.client.post(url).status_code, 403)
                self.assertEqual(self.snapshot(), before)
