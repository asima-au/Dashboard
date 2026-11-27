"""Chart of accounts and tax codes for A Academy. Rates are NOT set here: load them from
official sources with load_rate(); tax code rates below are the GST structure only."""
from .ledger import LedgerError

ACCOUNTS = [
    # code, name, type, bas_label, cf_class
    ('1000', 'Cash at bank', 'asset', None, None),
    ('1100', 'Fees receivable (students)', 'asset', None, None),
    ('1500', 'Equipment at cost', 'asset', None, None),
    ('1510', 'Accumulated depreciation - equipment', 'asset', None, None),
    ('2000', 'Accounts payable (suppliers)', 'liability', None, None),
    ('2100', 'GST payable (ATO)', 'liability', None, None),
    ('2200', 'PAYG withholding payable', 'liability', None, None),
    ('2300', 'Superannuation payable', 'liability', None, None),
    ('2400', 'Contract liabilities (fees received in advance)', 'liability', None, None),
    ('3000', "Owner's capital", 'equity', None, None),
    ('4000', 'Tuition revenue (AASB 15, over time)', 'income', None, None),
    ('4010', 'Textbook pack revenue (AASB 15, point in time)', 'income', None, None),
    ('5000', 'Teacher wages', 'expense', 'W1', None),
    ('5010', 'Admin salaries', 'expense', 'W1', None),
    ('5020', 'Superannuation expense', 'expense', None, None),
    ('5100', 'Rent', 'expense', None, None),
    ('5110', 'Utilities', 'expense', None, None),
    ('5120', 'Software & LMS', 'expense', None, None),
    ('5130', 'Marketing', 'expense', None, None),
    ('5140', 'Insurance', 'expense', None, None),
    ('5150', 'Teaching supplies', 'expense', None, None),
    ('5160', 'Accounting & legal', 'expense', None, None),
    ('5170', 'Bank & merchant fees', 'expense', None, None),
    ('5200', 'Depreciation', 'expense', None, None),
]

TAX_CODES = [
    ('GST', 1000, 'Taxable supply / creditable acquisition (rate to confirm against your fee status)'),
    ('FRE', 0, 'GST-free (use only if the course is confirmed GST-free)'),
    ('NT', 0, 'Not reportable (wages, super, bank movements)'),
]


def seed(conn, actor='system'):
    conn.executemany('INSERT OR IGNORE INTO accounts(code,name,type,bas_label,cf_class) VALUES (?,?,?,?,?)', ACCOUNTS)
    conn.executemany('INSERT OR IGNORE INTO tax_codes(code,rate_bp,description) VALUES (?,?,?)', TAX_CODES)
    conn.commit()


def load_rate(conn, name, effective_from, rate_bp, source_ref):
    """Only an official rate with its source reference may be stored."""
    if not source_ref or source_ref.strip().lower() in ('sample', 'test', 'tbd'):
        raise LedgerError('rates need a real source reference (e.g. the ATO page and edition)')
    conn.execute('INSERT INTO rates(name, effective_from, rate_bp, source_ref) VALUES (?,?,?,?)',
                 (name, effective_from, rate_bp, source_ref))
    conn.commit()
