"""Regression tests for the six seeded defects.

Each test names the bug it guards against and the business rule it enforces.
Run with: python -m unittest discover -s tests -v
"""
import io
import tempfile
import unittest
from pathlib import Path
from ledger import storage, importing, reporting


def fresh_db():
    """Return an in-memory-backed seeded database for each test."""
    tmp = tempfile.mkdtemp()
    db = storage.connect(Path(tmp) / 'test.sqlite3')
    storage.seed(db)
    return db


class TestPaymentMatchingByIdentity(unittest.TestCase):
    """Bug 1: matching.py was matching payments by amount instead of customer+invoice_number.
    BUSINESS_RULES.md: 'An amount alone does not establish identity.'
    """

    def test_same_amount_two_customers_matches_correct_invoice(self):
        """INV-100 (HARBOR) and INV-200 (MAPLE) both have amount=1250.00.
        A payment for MAPLE/INV-200 must NOT land on HARBOR/INV-100."""
        db = fresh_db()
        result = importing.import_csv(
            db,
            'payment_id,customer_id,invoice_number,amount\n'
            'PAY-X,MAPLE,INV-200,1250.00\n',
            'payments'
        )
        self.assertEqual(result['imported'], 1)
        self.assertEqual(result['rejected'], 0)
        rows = {r['invoice_number']: r for r in reporting.invoices(db)}
        # MAPLE/INV-200 must be paid, HARBOR/INV-100 must still be open
        self.assertEqual(rows['INV-200']['paid'], 1250.00)
        self.assertEqual(rows['INV-100']['paid'], 0.00)
        db.close()

    def test_payment_with_no_matching_invoice_is_unmatched(self):
        """A valid payment referencing a non-existent invoice must be stored unmatched.
        It must not change any invoice balance."""
        db = fresh_db()
        # Snapshot paid totals before the ghost import
        paid_before = {r['invoice_number']: r['paid'] for r in reporting.invoices(db)}

        result = importing.import_csv(
            db,
            'payment_id,customer_id,invoice_number,amount\n'
            'PAY-GHOST,HARBOR,WAIT-999,50.00\n',
            'payments'
        )
        self.assertEqual(result['imported'], 1)
        ov = reporting.overview(db)
        # No invoice's paid total should have changed
        for inv in ov['invoices']:
            self.assertEqual(
                inv['paid'], paid_before[inv['invoice_number']],
                f"Invoice {inv['invoice_number']} paid amount changed after unmatched import"
            )
        # Must appear in unmatched payments
        unmatched_ids = [p['payment_id'] for p in ov['unmatched_payments']]
        self.assertIn('PAY-GHOST', unmatched_ids)
        db.close()


class TestCSVImportRowIsolation(unittest.TestCase):
    """Bug 2: importing.py validated all rows eagerly, so one bad row aborted the whole import.
    BUSINESS_RULES.md: 'An invalid data row rejects only that row. Other valid rows must still be
    processed.'
    """

    def test_invalid_row_does_not_block_valid_rows(self):
        """Valid, invalid, valid: expect 2 imported and 1 rejected (not a crash)."""
        db = fresh_db()
        csv = (
            'customer_id,invoice_number,amount,due_date\n'
            'HARBOR,INV-103,84.00,2026-09-12\n'           # valid — line 2
            'NORTH,INV-302,not-a-number,2026-09-12\n'     # invalid — line 3
            'MAPLE,INV-203,100.00,2026-09-13\n'           # valid — line 4
        )
        result = importing.import_csv(db, csv, 'invoices')
        self.assertEqual(result['imported'], 2)
        self.assertEqual(result['rejected'], 1)
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(result['errors'][0]['line'], 3)
        db.close()

    def test_invalid_header_rejects_whole_file(self):
        """An invalid header must reject the whole import — no rows processed."""
        db = fresh_db()
        with self.assertRaises(ValueError):
            importing.import_csv(db, 'wrong,header\nHARBOR,INV-X,10.00,2026-09-09\n', 'invoices')

    def test_empty_data_with_valid_header_succeeds(self):
        """A valid header with no data rows is a successful import with zero counts."""
        db = fresh_db()
        result = importing.import_csv(db, 'customer_id,invoice_number,amount,due_date\n', 'invoices')
        self.assertEqual(result['imported'], 0)
        self.assertEqual(result['rejected'], 0)
        db.close()


class TestStatusFilter(unittest.TestCase):
    """Bug 3: reporting.py had {'open': 'paid', 'paid': 'paid'} — both filters returned paid.
    BUSINESS_RULES.md: 'open means a positive balance. paid means a zero or negative balance.'
    """

    def test_open_filter_returns_only_open_invoices(self):
        """?status=open must return only invoices with positive balances."""
        db = fresh_db()
        open_rows = reporting.invoices(db, 'open')
        for row in open_rows:
            self.assertEqual(row['status'], 'open',
                             f"Filter 'open' returned a {row['status']} invoice")
        db.close()

    def test_paid_filter_returns_only_paid_invoices(self):
        """?status=paid must return only invoices with zero or negative balances."""
        db = fresh_db()
        paid_rows = reporting.invoices(db, 'paid')
        for row in paid_rows:
            self.assertEqual(row['status'], 'paid',
                             f"Filter 'paid' returned a {row['status']} invoice")
        db.close()

    def test_open_and_paid_filters_are_complementary(self):
        """open + paid counts must equal all-invoices count."""
        db = fresh_db()
        total = len(reporting.invoices(db, 'all'))
        open_count = len(reporting.invoices(db, 'open'))
        paid_count = len(reporting.invoices(db, 'paid'))
        self.assertEqual(open_count + paid_count, total)
        db.close()

    def test_invalid_status_raises(self):
        """An invalid status must raise ValueError (returns HTTP 400)."""
        db = fresh_db()
        with self.assertRaises(ValueError):
            reporting.invoices(db, 'unknown')
        db.close()


class TestExportCSVMoneyPrecision(unittest.TestCase):
    """Bug 4: export_csv used int(x*100)/100 which truncates — 19.99 became 19.98.
    BUSINESS_RULES.md: 'Calculations must preserve cents.'
    """

    def _get_csv_row(self, db, invoice_number):
        csv_text = reporting.export_csv(db)
        for line in csv_text.splitlines()[1:]:
            if not line.strip():
                continue
            parts = line.split(',')
            if parts[1] == invoice_number:
                return parts
        return None

    def test_19_99_exports_correctly(self):
        """19.99 must appear as 19.99, not 19.98."""
        db = fresh_db()
        parts = self._get_csv_row(db, 'INV-300')  # NORTH/INV-300 = 19.99
        self.assertIsNotNone(parts)
        self.assertEqual(parts[2], '19.99', f"Amount exported as {parts[2]}, expected 19.99")
        db.close()

    def test_balance_with_cents_exports_correctly(self):
        """After a partial payment, balance cents must round correctly."""
        db = fresh_db()
        # INV-300 has amount 19.99, SEED-2 payment is 10.00, balance = 9.99
        parts = self._get_csv_row(db, 'INV-300')
        self.assertIsNotNone(parts)
        # balance is the 5th field (0-indexed)
        self.assertEqual(parts[4], '9.99', f"Balance exported as {parts[4]}, expected 9.99")
        db.close()

    def test_custom_amount_88_20_exports_correctly(self):
        """88.20 must export as 88.20 (float rounding edge case)."""
        db = fresh_db()
        importing.import_csv(
            db,
            'customer_id,invoice_number,amount,due_date\nMAPLE,KEEP-700,88.20,2026-09-10\n',
            'invoices'
        )
        parts = self._get_csv_row(db, 'KEEP-700')
        self.assertIsNotNone(parts)
        self.assertEqual(parts[2], '88.20', f"Amount exported as {parts[2]}, expected 88.20")
        db.close()


class TestInvoiceDuplicateHandling(unittest.TestCase):
    """Bug 5: insert_invoice had no duplicate detection — re-importing created duplicates.
    BUSINESS_RULES.md: 'identical details → SKIP / do not create duplicate;
    conflicting details → REJECT; never overwrite the existing invoice.'
    """

    def test_reimport_identical_invoice_skips(self):
        """Re-importing the same invoice with identical details must skip, not duplicate."""
        db = fresh_db()
        result = importing.import_csv(
            db,
            'customer_id,invoice_number,amount,due_date\n'
            'HARBOR,INV-100,1250.00,2026-09-01\n',
            'invoices'
        )
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['imported'], 0)
        # Only one row should exist
        count = db.execute(
            'SELECT COUNT(*) FROM invoices WHERE customer_id=? AND invoice_number=?',
            ('HARBOR', 'INV-100')
        ).fetchone()[0]
        self.assertEqual(count, 1)
        db.close()

    def test_reimport_conflicting_invoice_rejects(self):
        """Re-importing same invoice_number with different amount must reject, not overwrite."""
        db = fresh_db()
        result = importing.import_csv(
            db,
            'customer_id,invoice_number,amount,due_date\n'
            'HARBOR,INV-100,999.00,2026-09-01\n',
            'invoices'
        )
        self.assertEqual(result['rejected'], 1)
        self.assertEqual(result['imported'], 0)
        # Original amount must be preserved
        orig = db.execute(
            'SELECT amount FROM invoices WHERE customer_id=? AND invoice_number=?',
            ('HARBOR', 'INV-100')
        ).fetchone()
        self.assertEqual(orig['amount'], 1250.00)
        db.close()

    def test_same_invoice_number_different_customers_allowed(self):
        """The same invoice_number for different customers is valid — identity is (customer_id, invoice_number)."""
        db = fresh_db()
        # HARBOR/INV-SHARED and MAPLE/INV-SHARED should both be allowed
        result = importing.import_csv(
            db,
            'customer_id,invoice_number,amount,due_date\n'
            'HARBOR,INV-SHARED,50.00,2026-09-09\n'
            'MAPLE,INV-SHARED,75.00,2026-09-09\n',
            'invoices'
        )
        self.assertEqual(result['imported'], 2)
        self.assertEqual(result['rejected'], 0)
        db.close()


class TestPaymentDuplicateHandling(unittest.TestCase):
    """BUSINESS_RULES.md: Re-importing an identical payment must skip it.
    Reusing its ID with different details must reject the row.
    """

    def test_reimport_identical_payment_skips(self):
        db = fresh_db()
        result = importing.import_csv(
            db,
            'payment_id,customer_id,invoice_number,amount\n'
            'SEED-1,HARBOR,INV-101,300.00\n',
            'payments'
        )
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['imported'], 0)
        db.close()

    def test_reimport_conflicting_payment_rejects(self):
        db = fresh_db()
        result = importing.import_csv(
            db,
            'payment_id,customer_id,invoice_number,amount\n'
            'SEED-1,HARBOR,INV-101,999.00\n',  # different amount
            'payments'
        )
        self.assertEqual(result['rejected'], 1)
        self.assertEqual(result['imported'], 0)
        db.close()


class TestOverviewAgreesWithFilter(unittest.TestCase):
    """Overview open_count must equal len(invoices?status=open).
    The owner's complaint: 'The open-invoice view doesn't seem to agree with the overview.'
    """

    def test_overview_open_count_agrees_with_open_filter(self):
        db = fresh_db()
        ov = reporting.overview(db)
        open_rows = reporting.invoices(db, 'open')
        self.assertEqual(ov['summary']['open_count'], len(open_rows))
        db.close()

    def test_overpayment_marks_invoice_paid(self):
        """An overpayment results in a negative balance and status 'paid'."""
        db = fresh_db()
        # Pay more than INV-300 (19.99)
        importing.import_csv(
            db,
            'payment_id,customer_id,invoice_number,amount\n'
            'PAY-OVER,NORTH,INV-300,30.00\n',
            'payments'
        )
        rows = {r['invoice_number']: r for r in reporting.invoices(db)}
        self.assertEqual(rows['INV-300']['status'], 'paid')
        self.assertLess(rows['INV-300']['balance'], 0)
        db.close()


class TestFixturePreservation(unittest.TestCase):
    """Verify the existing register loads correctly with the migration applied.
    Expected: 9 invoices, 7 open, outstanding INR 3698.19, 1 unmatched payment.
    """

    def setUp(self):
        fixture = Path(__file__).parent.parent / 'fixtures' / 'existing-register.sqlite3'
        if not fixture.exists():
            self.skipTest('Fixture not available')
        import shutil
        self.tmp = tempfile.mkdtemp()
        self.db_path = Path(self.tmp) / 'fixture-copy.sqlite3'
        shutil.copy(fixture, self.db_path)
        self.db = storage.connect(self.db_path)  # migration runs here

    def tearDown(self):
        self.db.close()

    def test_fixture_invoice_count(self):
        self.assertEqual(len(reporting.invoices(self.db)), 9)

    def test_fixture_open_count(self):
        open_rows = reporting.invoices(self.db, 'open')
        self.assertEqual(len(open_rows), 7)

    def test_fixture_outstanding(self):
        ov = reporting.overview(self.db)
        self.assertAlmostEqual(ov['summary']['outstanding'], 3698.19, places=2)

    def test_fixture_unmatched_payments(self):
        ov = reporting.overview(self.db)
        self.assertEqual(len(ov['unmatched_payments']), 1)
        self.assertEqual(ov['unmatched_payments'][0]['payment_id'], 'KEEP-U1')

    def test_fixture_new_invoice_import_works_after_migration(self):
        """After migration, valid new invoice imports must succeed."""
        result = importing.import_csv(
            self.db,
            'customer_id,invoice_number,amount,due_date\n'
            'HARBOR,POST-RESTORE-1,456.78,2026-09-20\n',
            'invoices'
        )
        self.assertEqual(result['imported'], 1)

    def test_fixture_new_payment_import_works_after_migration(self):
        """After migration, valid new payment matching an existing invoice must succeed."""
        result = importing.import_csv(
            self.db,
            'payment_id,customer_id,invoice_number,amount\n'
            'POST-PAY-1,MAPLE,KEEP-700,50.00\n',
            'payments'
        )
        self.assertEqual(result['imported'], 1)


class TestOverdueImprovement(unittest.TestCase):
    """Improvement: invoices() returns an 'overdue' boolean.
    open invoices whose due_date < today are overdue=True; paid invoices are never overdue.
    This is an additive field; BUSINESS_RULES.md permits adding fields.
    """

    def test_past_due_open_invoice_is_overdue(self):
        """An open invoice with a due_date in the past must have overdue=True."""
        db = fresh_db()
        # All seeded invoices have due dates in 2026-09 which are in the past at test time.
        open_rows = reporting.invoices(db, 'open')
        self.assertTrue(len(open_rows) > 0, "Need at least one open invoice to test")
        for row in open_rows:
            self.assertIn('overdue', row, "overdue field missing from invoice")
            # All seeded open invoices have 2026-09 due dates, which are past
            self.assertTrue(
                row['overdue'],
                f"{row['invoice_number']} due {row['due_date']} should be overdue"
            )
        db.close()

    def test_paid_invoice_is_never_overdue(self):
        """A paid invoice must always have overdue=False regardless of due_date."""
        db = fresh_db()
        paid_rows = reporting.invoices(db, 'paid')
        self.assertTrue(len(paid_rows) > 0, "Need at least one paid invoice")
        for row in paid_rows:
            self.assertFalse(
                row['overdue'],
                f"Paid invoice {row['invoice_number']} must not be flagged overdue"
            )
        db.close()

    def test_future_due_date_open_invoice_is_not_overdue(self):
        """An open invoice with a future due_date must have overdue=False."""
        db = fresh_db()
        # Import an invoice due far in the future
        importing.import_csv(
            db,
            'customer_id,invoice_number,amount,due_date\n'
            'HARBOR,FUTURE-1,100.00,2099-12-31\n',
            'invoices'
        )
        rows = {r['invoice_number']: r for r in reporting.invoices(db)}
        self.assertFalse(
            rows['FUTURE-1']['overdue'],
            "Invoice due in 2099 must not be overdue"
        )
        db.close()

    def test_overdue_field_present_in_all_filter(self):
        """The overdue field must appear on every invoice returned by the all filter."""
        db = fresh_db()
        for row in reporting.invoices(db, 'all'):
            self.assertIn('overdue', row, f"overdue missing on {row['invoice_number']}")
        db.close()


if __name__ == '__main__':
    unittest.main()
