from decimal import Decimal
from random import choice, randint

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone

from django_ledger.forms.transactions import (
    get_transactionmodel_formset_class,
    TransactionModelForm,
)
from django_ledger.io.io_core import get_localdate
from django_ledger.models import (
    TransactionModel, EntityModel, AccountModel, LedgerModel, JournalEntryModel,
    TransactionModelValidationError, JournalEntryValidationError
)
from django_ledger.tests.base import DjangoLedgerBaseTest

UserModel = get_user_model()


class TransactionModelTest(DjangoLedgerBaseTest):

    def test_invalid_balance(self):
        entity_model = self.get_random_entity_model()
        txs_model = self.get_random_transaction(entity_model=entity_model)

        # transaction model does not allow for negative balances
        txs_model.amount = Decimal('-100.00')

        with self.assertRaises(ValidationError):
            txs_model.full_clean()


class TransactionModelFormTest(DjangoLedgerBaseTest):

    def test_valid_data(self):
        entity_model: EntityModel = self.get_random_entity_model()

        account_model = str(self.get_random_account(entity_model=entity_model, balance_type='credit').uuid),
        random_tx_type = choice([tx_type[0] for tx_type in TransactionModel.TX_TYPE])

        form_data = {
            'account': account_model[0],
            'tx_type': random_tx_type,
            'amount': Decimal(randint(10000, 99999)),
            'description': 'Bought Something ...'
        }

        form = TransactionModelForm(form_data)

        self.assertTrue(form.is_valid(), msg=f'Form is invalid with error: {form.errors}')
        with self.assertRaises(TransactionModel.journal_entry.RelatedObjectDoesNotExist):
            form.save()

    def test_invalid_tx_type(self):
        account_model = choice(AccountModel.objects.filter(balance_type='credit'))
        form = TransactionModelForm({
            'account': account_model,
            'tx_type': 'crebit patty',
        })
        self.assertFalse(form.is_valid(), msg='tx_type other than credit / debit shouldn\'t be valid')

    def test_blank_data(self):
        form = TransactionModelForm()
        self.assertFalse(form.is_valid(), msg='Form without data is supposed to be invalid')

    def test_invalid_account(self):
        form = TransactionModelForm({
            'account': 'Asset', 'tx_type': 'debit', 'amount': Decimal('100.00'),
        })
        self.assertFalse(form.is_valid())
        self.assertEqual(set(form.errors), {'account'})
        self.assertEqual(form.errors.as_data()['account'][0].code, 'invalid_choice')


class TransactionModelFormSetTest(DjangoLedgerBaseTest):

    def make_txs_formset(self, entity_model, credit_amount=Decimal('100.00')):
        ledger = LedgerModel.objects.create(entity=entity_model, name='Sample form ledger')
        journal = JournalEntryModel.objects.create(ledger=ledger, timestamp=timezone.now())
        credit = self.get_random_account(entity_model=entity_model, balance_type='credit', active=True, locked=False)
        debit = self.get_random_account(entity_model=entity_model, balance_type='debit', active=True, locked=False)
        data = {
            'form-TOTAL_FORMS': '8', 'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000',
            'form-0-account': str(credit.pk), 'form-0-tx_type': 'credit',
            'form-0-amount': str(credit_amount),
            'form-1-account': str(debit.pk), 'form-1-tx_type': 'debit', 'form-1-amount': '100.00',
        }
        for index in range(2, 8):
            data[f'form-{index}-amount'] = '0.00'
        formset_class = get_transactionmodel_formset_class(journal_entry_model=journal)
        return formset_class(data, entity_model=entity_model, je_model=journal), credit, debit

    def test_valid_formset(self):
        """Persist both sides of a balanced submission and check their exact values."""
        entity_model = self.get_random_entity_model()
        formset, credit, debit = self.make_txs_formset(entity_model)
        self.assertTrue(formset.is_valid(), msg=f'{formset.errors}: {formset.non_form_errors()}')
        instances = formset.save(commit=False)
        self.assertEqual(len(instances), 2)
        for transaction in instances:
            transaction.journal_entry = formset.JE_MODEL
        formset.save()
        saved = list(formset.JE_MODEL.transactionmodel_set.order_by('tx_type'))
        self.assertEqual([(tx.tx_type, tx.account_id, tx.amount) for tx in saved], [
            ('credit', credit.pk, Decimal('100.00')), ('debit', debit.pk, Decimal('100.00')),
        ])

    def test_imbalance_transactions(self):
        """A one-cent imbalance must produce a formset error without writing transactions."""
        formset, _, _ = self.make_txs_formset(self.get_random_entity_model(), Decimal('99.99'))
        self.assertFalse(formset.is_valid())
        self.assertEqual(formset.errors, [{}, {}, {}, {}, {}, {}, {}, {}])
        self.assertEqual(list(formset.non_form_errors()), ['Credits and Debits do not balance.'])
        self.assertFalse(formset.JE_MODEL.transactionmodel_set.exists())

    def test_je_locked(self):
        """
        Transaction on locked a locked Journal Entry should fail.
        """
        entity_model: EntityModel = self.get_random_entity_model()
        ledger_model: LedgerModel = self.get_random_ledger(
            entity_model=entity_model
        )

        je_model: JournalEntryModel = self.get_random_je(
            entity_model=entity_model,
            ledger_model=ledger_model
        )
        je_model.mark_as_locked(commit=True, raise_exception=False)

        self.assertTrue(je_model.is_locked())
        txs_model = je_model.transactionmodel_set.all().first()
        txs_model.amount += Decimal.from_float(1.00)

        with self.assertRaises(TransactionModelValidationError):
            txs_model.save()

        with self.assertRaises(
                TransactionModelValidationError,
                msg=f'Cannot create transaction on locked Journal Entry'
        ):
            je_model.transactionmodel_set.create(
                amount=Decimal.from_float(100.00),
                account=self.get_random_account(entity_model=entity_model, balance_type='debit')
            )

    def test_ledger_lock(self):
        """
        Transaction on locked a locked Ledger should fail.
        """
        entity_model: EntityModel = self.get_random_entity_model()
        ledger_model = self.get_random_ledger(entity_model=entity_model)
        ledger_model.post(commit=True, raise_exception=False)
        self.assertTrue(ledger_model.is_posted())
        ledger_model.lock(commit=True, raise_exception=False)
        self.assertTrue(ledger_model.is_locked())

        with self.assertRaises(
                JournalEntryValidationError,
                msg='Cannot create Journal Entries on locked ledgers.'
        ):
            ledger_model.journal_entries.create(
                timestamp=get_localdate(),
                description='Test Journal Entry'
            )

        je_model = ledger_model.journal_entries.first()

        with self.assertRaises(
                JournalEntryValidationError,
                msg='Cannot unpost journal entry on locked ledgers'
        ):
            je_model.mark_as_unposted(commit=True, raise_exception=True)


class GetTransactionModelFormSetClassTest(DjangoLedgerBaseTest):

    def make_journal(self, entity_model):
        ledger = LedgerModel.objects.create(entity=entity_model, name='Sample form layout ledger')
        journal = JournalEntryModel.objects.create(ledger=ledger, timestamp=timezone.now())
        for tx_type in ['credit', 'debit']:
            account = self.get_random_account(entity_model=entity_model, balance_type=tx_type, active=True, locked=False)
            TransactionModel.objects.create(journal_entry=journal, account=account,
                                            tx_type=tx_type, amount=Decimal('100.00'))
        return journal

    def test_unlocked_journal_entry_formset(self):
        """
        The Formset will contain 6 extra forms & delete fields if Journal Entry is unlocked.
        """
        entity_model: EntityModel = self.get_random_entity_model()
        je_model = self.make_journal(entity_model)

        transaction_model_form_set = get_transactionmodel_formset_class(journal_entry_model=je_model)
        txs_formset = transaction_model_form_set(
            entity_model=entity_model,
            je_model=je_model,
        )

        self.assertTrue(not je_model.is_locked(),
                        msg="At this point in this test case, Journal Entry should be unlocked.")

        delete_field = '<input type="checkbox" name="form-0-DELETE" id="id_form-0-DELETE">'
        self.assertInHTML(
            delete_field,
            txs_formset.as_table(),
            msg_prefix='Transactions Formset with unlocked Journal Entry should have `can_delete` enabled'
        )

        self.assertEqual(txs_formset.extra, 6,
                         msg='Transactions Formset with unlocked Journal Entry should have 6 extras')
        self.assertEqual(len(txs_formset), je_model.transactionmodel_set.count() + 6)

    def test_locked_journal_entry_formset(self):
        """
        The Formset will contain no extra forms & only forms with Transaction if Journal Entry is locked.
        """
        entity_model: EntityModel = self.get_random_entity_model()
        je_model = self.make_journal(entity_model)

        je_model.mark_as_locked(commit=True)
        self.assertTrue(
            je_model.is_locked(),
            msg="Journal Entry should be locked in this test case")

        transaction_model_form_set = get_transactionmodel_formset_class(journal_entry_model=je_model)

        txs_formset = transaction_model_form_set(
            entity_model=entity_model,
            je_model=je_model,
            queryset=je_model.transactionmodel_set.all().order_by('account__code')
        )

        self.assertEqual(
            len(txs_formset), (je_model.transactionmodel_set.count()),  # Convert pairs to total count
            msg="Transactions Formset with unlocked Journal Entry did not match the expected count")
