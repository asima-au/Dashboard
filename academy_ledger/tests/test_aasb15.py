"""AASB 15 treatment of the prepaid 10-week term: $1,200 term fee, $85 admin fee, $120 textbook pack.

Amounts are ex GST (assumption to confirm). Start 5 Jan 2026:
  service weeks end 11 Jan .. 15 Mar 2026 (10 weeks, $128.50 each)
  textbook pack delivered at a point in time.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from academy_ledger import ledger as L  # noqa: E402
from academy_ledger.chart import seed  # noqa: E402

ACTOR = 'test'


def scalar(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()[0]


class AASB15Term(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.conn = L.connect(self.tmp.name)
        seed(self.conn)
        L.open_periods(self.conn, '2026-01', '2026-04', ACTOR)
        L.add_student(self.conn, 'AC-00001', 'Test Student', 'CL-01', '2026-01', 120000, 'GST', ACTOR)
        self.cid = L.enrol_term(self.conn, 'AC-00001', 'T1-2026', '2026-01-05', weeks=10,
                                term_fee_c=120000, admin_fee_c=8500, book_c=12000,
                                tax_code='GST', actor=ACTOR)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def account_balance(self, account):
        """Debit minus credit on an account."""
        return scalar(self.conn, 'SELECT COALESCE(SUM(debit_c - credit_c),0) FROM journal_lines WHERE account=?', (account,))

    def test_allocation_by_relative_ssp(self):
        # Price 1,405.00; standalone selling prices: service 1,285.00, textbook 120.00.
        c = self.conn.execute('SELECT * FROM contracts WHERE id=?', (self.cid,)).fetchone()
        self.assertEqual(c['transaction_price_c'], 140500)
        self.assertEqual(c['service_c'], 128500)   # term fee + admin fee (admin not distinct)
        self.assertEqual(c['book_c'], 12000)
        self.assertEqual(c['tax_c'], 14050)

    def test_upfront_cash_is_a_liability_not_income(self):
        # At enrolment nothing is income yet.
        self.assertEqual(scalar(self.conn, "SELECT COALESCE(SUM(credit_c),0) FROM journal_lines WHERE account IN ('4000','4010')"), 0)
        self.assertEqual(self.account_balance('2400'), -140500)

    def test_service_recognised_evenly_10_weeks_of_128_50(self):
        weeks = [r['amount_c'] for r in self.conn.execute(
            'SELECT amount_c FROM contract_weeks WHERE contract_id=? ORDER BY week_no', (self.cid,))]
        self.assertEqual(weeks, [12850] * 10)
        jan = L.recognise_service(self.conn, '2026-01', ACTOR)
        feb = L.recognise_service(self.conn, '2026-02', ACTOR)
        mar = L.recognise_service(self.conn, '2026-03', ACTOR)
        self.assertEqual(len(jan), 3)    # weeks ending 11, 18 and 25 January
        self.assertEqual(len(jan) + len(feb) + len(mar), 10)
        self.assertEqual(scalar(self.conn, "SELECT COALESCE(SUM(credit_c),0) FROM journal_lines WHERE account='4000'"), 128500)

    def test_textbook_recognised_only_on_delivery(self):
        L.recognise_service(self.conn, '2026-01', ACTOR)
        L.recognise_service(self.conn, '2026-02', ACTOR)
        L.recognise_service(self.conn, '2026-03', ACTOR)
        self.assertEqual(scalar(self.conn, "SELECT COALESCE(SUM(credit_c),0) FROM journal_lines WHERE account='4010'"), 0)
        self.assertEqual(self.account_balance('2400'), -12000)  # textbook still owed as a performance obligation
        L.deliver_textbooks(self.conn, self.cid, '2026-02-10', ACTOR)
        self.assertEqual(scalar(self.conn, "SELECT COALESCE(SUM(credit_c),0) FROM journal_lines WHERE account='4010'"), 12000)
        self.assertEqual(self.account_balance('2400'), 0)

    def test_cannot_deliver_textbooks_twice(self):
        L.deliver_textbooks(self.conn, self.cid, '2026-02-10', ACTOR)
        with self.assertRaises(L.LedgerError):
            L.deliver_textbooks(self.conn, self.cid, '2026-02-11', ACTOR)

    def test_gst_reported_at_invoice_tax_point_only(self):
        L.recognise_service(self.conn, '2026-01', ACTOR)
        g = L.bas_quarter(self.conn, '2026-01', '2026-03')
        self.assertEqual(g['G1'], 154550)   # 1,405.00 + 140.50 GST, reported on the invoice
        self.assertEqual(g['1A'], 14050)
        self.assertEqual(g['1B'], 0)        # recognition entries add no second GST

    def test_ledger_balances_after_recognition(self):
        L.recognise_service(self.conn, '2026-01', ACTOR)
        self.assertTrue(L.trial_balance(self.conn)['balanced'])

    def test_postings_are_immutable(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute('UPDATE journals SET description="x"')

    def test_no_recognition_into_closed_period(self):
        L.close_period(self.conn, '2026-01', ACTOR)
        with self.assertRaises(L.LedgerError):
            L.recognise_service(self.conn, '2026-01', ACTOR)


if __name__ == '__main__':
    unittest.main()
