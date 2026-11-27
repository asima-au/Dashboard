"""A Academy double-entry ledger engine.

Rules enforced here (and by the database triggers in schema.sql):
  * money is integer cents; every rounding is explicit and half-up
  * every journal balances (debits == credits) and each line is one-sided
  * posting only into an open period; closed periods are locked
  * posted journals are immutable: corrections are reversals
  * every posting, lock and reversal is written to a hash-chained audit log
  * payroll needs approved timesheets and a loaded PAYG table; otherwise it refuses
  * no averages or estimates are used to compute any tax or pay amount
"""
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3

SCHEMA = (pathlib.Path(__file__).with_name('schema.sql')).read_text()

CF_CLASSES = ['receipts_students', 'payments_suppliers', 'payroll', 'gst_bas',
              'super', 'capex', 'owner', 'bank_fees', 'other']


class LedgerError(Exception):
    pass


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.executescript(SCHEMA)
    return conn


# ---------- money -----------------------------------------------------------
def mul_bp(amount_c, bp):
    """amount × bp / 10000, rounded half-up to whole cents (sign-safe)."""
    sign = -1 if amount_c < 0 else 1
    a = abs(amount_c) * bp
    return sign * ((2 * a + 10000) // 20000)


def div_half_up(a, b):
    return (2 * a + b) // (2 * b)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def ym_of(date):
    return date[:7]


def ym_index(ym):
    return int(ym[:4]) * 12 + int(ym[5:7]) - 1


def ym_from_index(i):
    return f'{i // 12}-{(i % 12) + 1:02d}'


def months_between(start_ym, end_ym):
    return [ym_from_index(i) for i in range(ym_index(start_ym), ym_index(end_ym) + 1)]


# ---------- audit log -------------------------------------------------------
def audit(conn, actor, action, detail):
    prev = conn.execute('SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1').fetchone()
    prev_hash = prev['hash'] if prev else 'GENESIS'
    at = now_iso()
    body = json.dumps(detail, sort_keys=True)
    h = hashlib.sha256('|'.join([prev_hash, at, actor, action, body]).encode()).hexdigest()
    conn.execute('INSERT INTO audit_log(at, actor, action, detail, prev_hash, hash) VALUES (?,?,?,?,?,?)',
                 (at, actor, action, body, prev_hash, h))


def verify_audit(conn):
    """Recompute the hash chain. Returns (ok, first_bad_id)."""
    prev_hash = 'GENESIS'
    for r in conn.execute('SELECT * FROM audit_log ORDER BY id'):
        if r['prev_hash'] != prev_hash:
            return False, r['id']
        h = hashlib.sha256('|'.join([r['prev_hash'], r['at'], r['actor'], r['action'], r['detail']]).encode()).hexdigest()
        if h != r['hash']:
            return False, r['id']
        prev_hash = h
    return True, None


# ---------- periods ---------------------------------------------------------
def open_periods(conn, start_ym, end_ym, actor):
    for ym in months_between(start_ym, end_ym):
        conn.execute('INSERT OR IGNORE INTO periods(ym) VALUES (?)', (ym,))
    audit(conn, actor, 'open_periods', {'from': start_ym, 'to': end_ym})
    conn.commit()


def close_period(conn, ym, actor):
    row = conn.execute('SELECT status FROM periods WHERE ym=?', (ym,)).fetchone()
    if row is None:
        raise LedgerError(f'period {ym} does not exist')
    if row['status'] == 'closed':
        return
    tb = trial_balance(conn, ym)
    if not tb['balanced']:
        raise LedgerError(f'cannot close {ym}: trial balance does not balance')
    conn.execute("UPDATE periods SET status='closed', closed_at=?, closed_by=? WHERE ym=?", (now_iso(), actor, ym))
    audit(conn, actor, 'close_period', {'ym': ym})
    conn.commit()


# ---------- rates & PAYG ----------------------------------------------------
def rate_at(conn, name, date):
    row = conn.execute('SELECT rate_bp, source_ref FROM rates WHERE name=? AND effective_from<=? '
                       'ORDER BY effective_from DESC LIMIT 1', (name, date)).fetchone()
    if row is None:
        raise LedgerError(f'no {name} rate loaded for {date}; load it from the official source first')
    return row['rate_bp'], row['source_ref']


def payg_withholding(conn, scale, date, ote_c):
    """Withholding from the loaded ATO table: base + marginal rate × (amount − band lower edge)."""
    row = conn.execute(
        'SELECT lower_c, upper_c, base_c, rate_bp, source_ref, effective_from FROM payg_tables '
        'WHERE scale=? AND effective_from<=? AND lower_c<=? AND (upper_c IS NULL OR upper_c>=?) '
        'ORDER BY effective_from DESC, lower_c DESC LIMIT 1', (scale, date, ote_c, ote_c)).fetchone()
    if row is None:
        raise LedgerError(f'no PAYG withholding table loaded for scale {scale} at {date}. '
                          'Refusing to estimate. Load the ATO statement table first.')
    amount = row['base_c'] + mul_bp(ote_c - row['lower_c'], row['rate_bp'])
    return amount, f"{row['source_ref']} (from {row['effective_from']})"


# ---------- posting -----------------------------------------------------------
def dr(account, amount_c, **kw):
    return dict(account=account, debit_c=amount_c, credit_c=0, **kw)


def cr(account, amount_c, **kw):
    return dict(account=account, debit_c=0, credit_c=amount_c, **kw)


def post(conn, journal_date, source, description, lines, actor, source_ref=None, cf_class=None, reverses=None):
    period = ym_of(journal_date)
    per = conn.execute('SELECT status FROM periods WHERE ym=?', (period,)).fetchone()
    if per is None:
        raise LedgerError(f'period {period} is not opened')
    if per['status'] != 'open':
        raise LedgerError(f'period {period} is closed; post in an open period')
    if not lines:
        raise LedgerError('journal has no lines')
    dr_total = cr_total = 0
    for l in lines:
        d, c = l.get('debit_c', 0), l.get('credit_c', 0)
        if (d > 0) == (c > 0):
            raise LedgerError(f"line on {l['account']} must be exactly one of debit or credit")
        if conn.execute('SELECT 1 FROM accounts WHERE code=?', (l['account'],)).fetchone() is None:
            raise LedgerError(f"unknown account {l['account']}")
        tc = l.get('tax_code')
        tax = l.get('tax_c', 0)
        if tc:
            t = conn.execute('SELECT rate_bp FROM tax_codes WHERE code=?', (tc,)).fetchone()
            if t is None:
                raise LedgerError(f'unknown tax code {tc}')
            expected = mul_bp(d + c, t['rate_bp'])
            if tax != expected:
                raise LedgerError(f'tax on {l["account"]} is {tax}c but {tc} gives {expected}c')
        elif tax:
            raise LedgerError('tax amount given without a tax code')
        dr_total += d
        cr_total += c
    if dr_total != cr_total:
        raise LedgerError(f'unbalanced journal: debits {dr_total} credits {cr_total}')
    if cf_class and cf_class not in CF_CLASSES:
        raise LedgerError(f'unknown cf_class {cf_class}')
    touches_bank = any(l['account'] == '1000' for l in lines)
    if touches_bank and not cf_class:
        raise LedgerError('bank postings need a cf_class')

    with conn:
        cur = conn.execute(
            'INSERT INTO journals(journal_date, period, source, source_ref, description, cf_class, reverses, posted_at, posted_by) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (journal_date, period, source, source_ref, description, cf_class, reverses, now_iso(), actor))
        jid = cur.lastrowid
        for l in lines:
            conn.execute(
                'INSERT INTO journal_lines(journal_id, account, debit_c, credit_c, tax_code, tax_c, party) VALUES (?,?,?,?,?,?,?)',
                (jid, l['account'], l.get('debit_c', 0), l.get('credit_c', 0), l.get('tax_code'), l.get('tax_c', 0), l.get('party')))
        audit(conn, actor, 'post', {'journal': jid, 'source': source, 'ref': source_ref, 'amount_c': dr_total})
    return jid


def reverse(conn, journal_id, actor):
    j = conn.execute('SELECT * FROM journals WHERE id=?', (journal_id,)).fetchone()
    if j is None:
        raise LedgerError('no such journal')
    if conn.execute('SELECT 1 FROM journals WHERE reverses=?', (journal_id,)).fetchone():
        raise LedgerError(f'journal {journal_id} is already reversed')
    lines = conn.execute('SELECT * FROM journal_lines WHERE journal_id=?', (journal_id,)).fetchall()
    swapped = [dict(account=l['account'], debit_c=l['credit_c'], credit_c=l['debit_c'],
                    tax_code=l['tax_code'], tax_c=l['tax_c'], party=l['party']) for l in lines]
    # Reversal goes into the current open period (the original period may be locked).
    today = dt.date.today().isoformat()
    cur_ym = ym_of(today)
    conn.execute('INSERT OR IGNORE INTO periods(ym) VALUES (?)', (cur_ym,))
    return post(conn, today, 'reversal', f'Reversal of journal {journal_id}: {j["description"]}',
                swapped, actor, source_ref=j['source_ref'], cf_class=j['cf_class'], reverses=journal_id)


# ---------- students & fees ---------------------------------------------------
def add_student(conn, sid, name, class_id, start_ym, fee_c, tax_code, actor, end_ym=None, leave_reason=None):
    with conn:
        conn.execute('INSERT INTO students(id,name,class_id,start_ym,end_ym,fee_c,tax_code,leave_reason) VALUES (?,?,?,?,?,?,?,?)',
                     (sid, name, class_id, start_ym, end_ym, fee_c, tax_code, leave_reason))
        audit(conn, actor, 'add_student', {'id': sid, 'start': start_ym, 'fee_c': fee_c})


def bill_fees(conn, ym, actor):
    """Disabled. Recognising tuition as income at invoice date is not AASB 15 compliant for
    prepaid terms. Use enrol_term() for each student's term contract."""
    raise LedgerError('bill_fees is disabled: use enrol_term (AASB 15 contract accounting)')


def _legacy_bill_fees(conn, ym, actor):
    students = conn.execute(
        'SELECT * FROM students WHERE start_ym<=? AND (end_ym IS NULL OR end_ym>?) ORDER BY id', (ym, ym)).fetchall()
    created = []
    for s in students:
        if conn.execute('SELECT 1 FROM invoices WHERE student_id=? AND ym=?', (s['id'], ym)).fetchone():
            continue
        rate = conn.execute('SELECT rate_bp FROM tax_codes WHERE code=?', (s['tax_code'],)).fetchone()['rate_bp']
        net = s['fee_c']
        tax = mul_bp(net, rate)
        total = net + tax
        number = f'INV-{ym.replace("-", "")}-{s["id"]}'
        lines = [dr('1100', total, party=s['id']),
                 cr('4000', net, tax_code=s['tax_code'], tax_c=tax, party=s['id'])]
        if tax:
            lines.append(cr('2100', tax))
        jid = post(conn, f'{ym}-01', 'invoice', f'Tuition {ym} · {s["name"]}', lines, actor, source_ref=number)
        conn.execute('INSERT INTO invoices(number, student_id, ym, net_c, tax_c, total_c, journal_id) VALUES (?,?,?,?,?,?,?)',
                     (number, s['id'], ym, net, tax, total, jid))
        created.append(number)
    return created


def open_balance(conn, invoice_id):
    inv = conn.execute('SELECT total_c FROM invoices WHERE id=?', (invoice_id,)).fetchone()
    paid = conn.execute('SELECT COALESCE(SUM(amount_c),0) s FROM allocations WHERE invoice_id=?', (invoice_id,)).fetchone()['s']
    return inv['total_c'] - paid


def receive_payment(conn, date, bank_ref, amount_c, allocations, actor):
    """allocations: list of (invoice_number, amount_c). Every cent must be matched to an invoice."""
    if sum(a for _, a in allocations) != amount_c:
        raise LedgerError('receipt is not fully allocated to invoices')
    student = None
    with conn:
        for number, a in allocations:
            inv = conn.execute('SELECT * FROM invoices WHERE number=?', (number,)).fetchone()
            if inv is None:
                raise LedgerError(f'unknown invoice {number}')
            if student is None:
                student = inv['student_id']
            if inv['student_id'] != student:
                raise LedgerError('one receipt can only allocate to one student')
            if a > open_balance(conn, inv['id']):
                raise LedgerError(f'over-allocation on {number}')
        jid = post(conn, date, 'receipt', f'Receipt {bank_ref} · {student}',
                   [dr('1000', amount_c, party=student), cr('1100', amount_c, party=student)],
                   actor, source_ref=bank_ref, cf_class='receipts_students')
        for number, a in allocations:
            inv = conn.execute('SELECT id FROM invoices WHERE number=?', (number,)).fetchone()
            conn.execute('INSERT INTO allocations(invoice_id, journal_id, amount_c) VALUES (?,?,?)', (inv['id'], jid, a))
    return jid


# ---------- suppliers ---------------------------------------------------------
def record_bill(conn, date, number, supplier, expense_account, net_c, tax_code, actor):
    rate = conn.execute('SELECT rate_bp FROM tax_codes WHERE code=?', (tax_code,)).fetchone()['rate_bp']
    tax = mul_bp(net_c, rate)
    lines = [dr(expense_account, net_c, tax_code=tax_code, tax_c=tax, party=supplier), cr('2000', net_c + tax, party=supplier)]
    if tax:
        lines.append(dr('2100', tax))
    # Lines sum: debit net + tax, credit AP net + tax. Keep the tax debit on the control account.
    jid = post(conn, date, 'bill', f'Bill {number} · {supplier}', lines, actor, source_ref=number)
    conn.execute('INSERT INTO bills(number, supplier, bill_date, net_c, tax_c, total_c, journal_id) VALUES (?,?,?,?,?,?,?)',
                 (number, supplier, date, net_c, tax, net_c + tax, jid))
    return jid


def pay_bill(conn, date, number, bank_ref, actor):
    b = conn.execute('SELECT * FROM bills WHERE number=?', (number,)).fetchone()
    if b is None:
        raise LedgerError(f'unknown bill {number}')
    if b['paid_journal_id']:
        raise LedgerError(f'bill {number} already paid')
    with conn:
        jid = post(conn, date, 'bill_payment', f'Payment {number} · {b["supplier"]}',
                   [dr('2000', b['total_c'], party=b['supplier']), cr('1000', b['total_c'], party=b['supplier'])],
                   actor, source_ref=bank_ref, cf_class='payments_suppliers')
        conn.execute('UPDATE bills SET paid_journal_id=? WHERE id=?', (jid, b['id']))
    return jid


# ---------- payroll (timesheets -> pay, nothing averaged) ---------------------
def add_timesheet(conn, employee_id, work_date, hours_milli, class_id, actor):
    with conn:
        conn.execute('INSERT INTO timesheets(employee_id, work_date, hours_milli, class_id) VALUES (?,?,?,?)',
                     (employee_id, work_date, hours_milli, class_id))
        audit(conn, actor, 'add_timesheet', {'employee': employee_id, 'date': work_date, 'hours_milli': hours_milli})


def approve_timesheets(conn, employee_id, up_to_date, approver):
    with conn:
        conn.execute('UPDATE timesheets SET approved_by=? WHERE employee_id=? AND work_date<=? AND approved_by IS NULL',
                     (approver, employee_id, up_to_date))
        audit(conn, approver, 'approve_timesheets', {'employee': employee_id, 'up_to': up_to_date})


def run_payroll(conn, pay_date, actor):
    if conn.execute('SELECT 1 FROM pay_runs WHERE pay_date=?', (pay_date,)).fetchone():
        raise LedgerError(f'pay run for {pay_date} already exists')
    sg_bp, sg_ref = rate_at(conn, 'SG', pay_date)
    employees = conn.execute('SELECT * FROM employees WHERE active=1 ORDER BY id').fetchall()

    per_emp = []
    for e in employees:
        unapproved = conn.execute(
            'SELECT COUNT(*) c FROM timesheets WHERE employee_id=? AND work_date<=? AND approved_by IS NULL AND pay_run_id IS NULL',
            (e['id'], pay_date)).fetchone()['c']
        if unapproved:
            raise LedgerError(f'{unapproved} unapproved timesheet line(s) for {e["id"]}: approve before the pay run')
        rows = conn.execute(
            'SELECT id, hours_milli FROM timesheets WHERE employee_id=? AND work_date<=? AND approved_by IS NOT NULL AND pay_run_id IS NULL',
            (e['id'], pay_date)).fetchall()
        if not rows:
            continue
        hours = sum(r['hours_milli'] for r in rows)
        gross = div_half_up(hours * e['rate_c'], 1000)     # hours in milli-hours × cents per hour
        payg, payg_src = payg_withholding(conn, e['payg_scale'], pay_date, gross)
        super_c = mul_bp(gross, sg_bp)
        per_emp.append(dict(e=e, rows=rows, gross=gross, payg=payg, payg_src=payg_src, super=super_c, net=gross - payg))

    if not per_emp:
        raise LedgerError('no approved timesheets to pay')

    by_acct = {}
    for p in per_emp:
        by_acct[p['e']['wage_account']] = by_acct.get(p['e']['wage_account'], 0) + p['gross']
    total = {k: sum(p[k] for p in per_emp) for k in ('gross', 'payg', 'super', 'net')}
    lines = [dr(acc, amt, party=None) for acc, amt in sorted(by_acct.items())]
    lines += [dr('5020', total['super']), cr('2200', total['payg']), cr('2300', total['super']), cr('1000', total['net'])]
    with conn:
        jid = post(conn, pay_date, 'payroll', f'Pay run {pay_date}', lines, actor,
                   source_ref=f'PAY-{pay_date}', cf_class='payroll')
        cur = conn.execute('INSERT INTO pay_runs(pay_date, journal_id) VALUES (?,?)', (pay_date, jid))
        run_id = cur.lastrowid
        for p in per_emp:
            conn.execute('INSERT INTO pay_lines(pay_run_id, employee_id, gross_c, payg_c, super_c, net_c, payg_source) VALUES (?,?,?,?,?,?,?)',
                         (run_id, p['e']['id'], p['gross'], p['payg'], p['super'], p['net'], p['payg_src']))
            for r in p['rows']:
                conn.execute('UPDATE timesheets SET pay_run_id=? WHERE id=?', (run_id, r['id']))
        audit(conn, actor, 'pay_run_detail', {'run': run_id, 'sg_source': sg_ref, 'totals': total})
    return jid


# ---------- depreciation (exact straight line) -------------------------------
def add_asset(conn, description, cost_c, purchase_ym, life_months, actor, cost_account='1500'):
    lines = [dr(cost_account, cost_c), cr('1000', cost_c)]
    jid = post(conn, f'{purchase_ym}-01', 'asset', f'Purchase · {description}', lines, actor,
               source_ref=f'ASSET-{description[:20]}', cf_class='capex')
    cur = conn.execute('INSERT INTO assets(description, cost_c, purchase_ym, life_months, journal_id) VALUES (?,?,?,?,?)',
                       (description, cost_c, purchase_ym, life_months, jid))
    conn.commit()
    return cur.lastrowid


def depreciation_for(cost_c, life_months, months_elapsed):
    """Cumulative straight-line charge, exact to the cent (no monthly rounding drift)."""
    if months_elapsed <= 0:
        return 0
    k = min(months_elapsed, life_months)
    return div_half_up(cost_c * k, life_months)


def run_depreciation(conn, ym, actor):
    created = []
    for a in conn.execute('SELECT * FROM assets').fetchall():
        k = ym_index(ym) - ym_index(a['purchase_ym'])
        if k < 1 or k > a['life_months']:
            continue
        if conn.execute('SELECT 1 FROM depreciation_runs WHERE asset_id=? AND ym=?', (a['id'], ym)).fetchone():
            continue
        amount = depreciation_for(a['cost_c'], a['life_months'], k) - depreciation_for(a['cost_c'], a['life_months'], k - 1)
        with conn:
            jid = post(conn, f'{ym}-28', 'depreciation', f'Depreciation {ym} · {a["description"]}',
                       [dr('5200', amount), cr('1510', amount)], actor, source_ref=f'DEP-{ym}-{a["id"]}')
            conn.execute('INSERT INTO depreciation_runs(asset_id, ym, amount_c, journal_id) VALUES (?,?,?,?)', (a['id'], ym, amount, jid))
        created.append((a['id'], amount))
    return created


def record_cash_expense(conn, date, bank_ref, account, amount_c, description, actor, tax_code=None, tax_c=0, cf_class='other'):
    lines = [dr(account, amount_c, tax_code=tax_code, tax_c=tax_c), cr('1000', amount_c + tax_c)]
    if tax_c:
        lines.append(dr('2100', tax_c))
    return post(conn, date, 'bank', description, lines, actor, source_ref=bank_ref, cf_class=cf_class)


# ---------- reports -------------------------------------------------------------
def trial_balance(conn, through_ym=None):
    q = ('SELECT a.code, a.name, a.type, COALESCE(SUM(l.debit_c),0) d, COALESCE(SUM(l.credit_c),0) c '
         'FROM accounts a LEFT JOIN journal_lines l ON l.account=a.code '
         'LEFT JOIN journals j ON j.id=l.journal_id ')
    params = ()
    if through_ym:
        q += 'WHERE j.period<=? OR j.id IS NULL '
        params = (through_ym,)
    q += 'GROUP BY a.code ORDER BY a.code'
    rows = [dict(r) for r in conn.execute(q, params)]
    d = sum(r['d'] for r in rows)
    c = sum(r['c'] for r in rows)
    return {'rows': rows, 'debits': d, 'credits': c, 'balanced': d == c}


def _sum_accounts(conn, types, start_ym, end_ym):
    q = ('SELECT a.code, a.name, a.type, COALESCE(SUM(l.debit_c),0) d, COALESCE(SUM(l.credit_c),0) c FROM accounts a '
         'JOIN journal_lines l ON l.account=a.code JOIN journals j ON j.id=l.journal_id '
         f"WHERE a.type IN ({','.join('?' * len(types))}) AND j.period BETWEEN ? AND ? "
         'GROUP BY a.code ORDER BY a.code')
    return [dict(r) for r in conn.execute(q, (*types, start_ym, end_ym))]


def profit_and_loss(conn, start_ym, end_ym):
    rows = _sum_accounts(conn, ['income', 'expense'], start_ym, end_ym)
    income = [dict(code=r['code'], name=r['name'], amount_c=r['c'] - r['d']) for r in rows if r['type'] == 'income']
    expense = [dict(code=r['code'], name=r['name'], amount_c=r['d'] - r['c']) for r in rows if r['type'] == 'expense']
    ti = sum(x['amount_c'] for x in income)
    te = sum(x['amount_c'] for x in expense)
    return {'income': income, 'expense': expense, 'total_income': ti, 'total_expense': te, 'net_profit': ti - te}


def balance_sheet(conn, as_of_ym):
    tb = trial_balance(conn, as_of_ym)['rows']
    first = '0000-00'
    groups = {'asset': [], 'liability': [], 'equity': []}
    profit = 0
    for r in tb:
        if r['type'] in ('income', 'expense'):
            profit += (r['c'] - r['d']) if r['type'] == 'income' else -(r['d'] - r['c'])
            continue
        bal = (r['d'] - r['c']) if r['type'] == 'asset' else (r['c'] - r['d'])
        if bal:
            groups[r['type']].append(dict(code=r['code'], name=r['name'], amount_c=bal))
    ta = sum(x['amount_c'] for x in groups['asset'])
    tl = sum(x['amount_c'] for x in groups['liability'])
    te = sum(x['amount_c'] for x in groups['equity']) + profit
    return {**groups, 'retained_profit_c': profit, 'total_assets': ta, 'total_liabilities': tl,
            'total_equity': te, 'balanced': ta == tl + te}


def bas_quarter(conn, start_ym, end_ym):
    """GST worksheet from tagged ledger lines (G1, 1A, G10, G11, 1B) and W1/W2 from payroll."""
    rows = conn.execute(
        'SELECT a.type, a.bas_label, l.account, l.debit_c, l.credit_c, l.tax_code, l.tax_c '
        'FROM journal_lines l JOIN accounts a ON a.code=l.account JOIN journals j ON j.id=l.journal_id '
        'WHERE j.period BETWEEN ? AND ?', (start_ym, end_ym)).fetchall()
    g1 = g1a = g10 = g11 = g1b = w1 = w2 = 0
    for r in rows:
        # GST is reported at the tax point (invoice). Invoice lines carry the tax code on the
        # contract-liability (2400) credit; revenue recognition lines are untagged to avoid double counting.
        if r['tax_code'] and r['type'] in ('income', 'liability') and r['credit_c']:
            g1 += r['credit_c'] + r['tax_c']
            g1a += r['tax_c']
        if r['tax_code'] and r['type'] == 'expense' and r['debit_c']:
            g11 += r['debit_c']
            g1b += r['tax_c']
        if r['tax_code'] and r['type'] == 'asset' and r['debit_c'] and r['account'] not in ('1000', '1100', '2100'):
            g10 += r['debit_c']
            g1b += r['tax_c']
        if r['bas_label'] == 'W1' and r['debit_c']:
            w1 += r['debit_c']
        if r['account'] == '2200' and r['credit_c']:
            w2 += r['credit_c']
    return {'G1': g1, '1A': g1a, 'G10': g10, 'G11': g11, '1B': g1b,
            'W1': w1, 'W2': w2, 'net_gst_payable': g1a - g1b}


def cash_flow(conn, start_ym, end_ym):
    rows = conn.execute(
        'SELECT j.cf_class, SUM(l.debit_c - l.credit_c) amt FROM journal_lines l JOIN journals j ON j.id=l.journal_id '
        'WHERE l.account=\'1000\' AND j.period BETWEEN ? AND ? GROUP BY j.cf_class', (start_ym, end_ym)).fetchall()
    out = {r['cf_class']: r['amt'] for r in rows}
    return {'by_class': out, 'net_change': sum(out.values())}


def ar_aging(conn, as_of_date):
    rows = []
    for inv in conn.execute('SELECT i.*, j.journal_date FROM invoices i JOIN journals j ON j.id=i.journal_id').fetchall():
        bal = open_balance(conn, inv['id'])
        if bal <= 0:
            continue
        age = (dt.date.fromisoformat(as_of_date) - dt.date.fromisoformat(inv['journal_date'])).days
        bucket = '0-30' if age <= 30 else '31-60' if age <= 60 else '61-90' if age <= 90 else '90+'
        rows.append(dict(number=inv['number'], student_id=inv['student_id'], open_c=bal, days=age, bucket=bucket))
    ledger_ar = conn.execute("SELECT COALESCE(SUM(debit_c-credit_c),0) b FROM journal_lines WHERE account='1100'").fetchone()['b']
    return {'items': rows, 'total_c': sum(r['open_c'] for r in rows), 'ledger_c': ledger_ar,
            'reconciled': sum(r['open_c'] for r in rows) == ledger_ar}


def bank_reconciliation(conn, statement):
    """statement: list of (bank_ref, amount_c). Compares against ledger bank postings."""
    ledger = {r['bank_ref']: r['amount_c'] for r in conn.execute('SELECT bank_ref, amount_c FROM bank_transactions')}
    stmt = dict(statement)
    only_ledger = sorted(set(ledger) - set(stmt))
    only_bank = sorted(set(stmt) - set(ledger))
    diff = sorted(k for k in set(ledger) & set(stmt) if ledger[k] != stmt[k])
    return {'only_in_ledger': only_ledger, 'only_in_bank': only_bank, 'amount_mismatch': diff,
            'reconciled': not (only_ledger or only_bank or diff)}


# ---------- AASB 15: prepaid term contracts ------------------------------------
def allocate_price(price_c, ssp_service_c, ssp_book_c):
    """AASB 15 paras 73-80: allocate the transaction price by relative standalone selling price."""
    total_ssp = ssp_service_c + ssp_book_c
    book = div_half_up(price_c * ssp_book_c, total_ssp)
    return price_c - book, book


def enrol_term(conn, student_id, term_ref, start_date, weeks, term_fee_c, admin_fee_c, book_c,
               tax_code, actor, book_ssp_c=None):
    """One contract per student per term.

    Obligations under AASB 15:
      * Term teaching (over time): term fee + admin fee. The admin fee is an enrolment charge
        with no separate service, so it is not distinct and is recognised with the term.
      * Textbook pack (point in time, on delivery): a distinct good with its own standalone price.
    Transaction price is allocated by relative standalone selling price (book SSP defaults to its price).
    Cash/invoice goes to contract liabilities (2400), never to income.
    """
    if weeks <= 0:
        raise LedgerError('term must have at least one week')
    st = conn.execute('SELECT * FROM students WHERE id=?', (student_id,)).fetchone()
    if st is None:
        raise LedgerError(f'unknown student {student_id}')
    if conn.execute('SELECT 1 FROM contracts WHERE term_ref=?', (term_ref,)).fetchone():
        raise LedgerError(f'contract {term_ref} already exists')
    rate = conn.execute('SELECT rate_bp FROM tax_codes WHERE code=?', (tax_code,)).fetchone()['rate_bp']
    service_ssp = term_fee_c + admin_fee_c
    price = service_ssp + book_c
    service_c, book_alloc = allocate_price(price, service_ssp, book_c if book_ssp_c is None else book_ssp_c)
    tax = mul_bp(price, rate)
    number = f'INV-{term_ref}'
    with conn:
        jid = post(conn, start_date, 'invoice', f'Term contract {term_ref} · {student_id}',
                   [dr('1100', price + tax, party=student_id),
                    cr('2400', price, tax_code=tax_code, tax_c=tax, party=student_id),
                    cr('2100', tax)] if tax else
                   [dr('1100', price, party=student_id), cr('2400', price, party=student_id)],
                   actor, source_ref=number)
        cur = conn.execute('INSERT INTO contracts(student_id, term_ref, start_date, weeks, transaction_price_c, service_c, '
                           'book_c, tax_c, invoice_journal_id) VALUES (?,?,?,?,?,?,?,?,?)',
                           (student_id, term_ref, start_date, weeks, price, service_c, book_alloc, tax, jid))
        cid = cur.lastrowid
        # Service spread evenly over the weeks; any cent remainder lands in the final week.
        base, rem = divmod(service_c, weeks)
        start = dt.date.fromisoformat(start_date)
        for w in range(1, weeks + 1):
            amount = base + (rem if w == weeks else 0)
            on = (start + dt.timedelta(days=7 * w - 1)).isoformat()
            conn.execute('INSERT INTO contract_weeks(contract_id, week_no, recognise_on, amount_c) VALUES (?,?,?,?)',
                         (cid, w, on, amount))
        audit(conn, actor, 'enrol_term', {'contract': cid, 'service_c': service_c, 'book_c': book_alloc})
    return cid


def recognise_service(conn, ym, actor):
    """Release contract liability for weeks of service that end in `ym` (not yet recognised)."""
    weeks = conn.execute(
        'SELECT cw.*, c.student_id FROM contract_weeks cw JOIN contracts c ON c.id=cw.contract_id '
        'WHERE cw.journal_id IS NULL AND substr(cw.recognise_on,1,7)=? ORDER BY cw.contract_id, cw.week_no', (ym,)).fetchall()
    out = []
    for w in weeks:
        with conn:
            jid = post(conn, w['recognise_on'], 'revenue_recognition',
                       f'Term service week {w["week_no"]} · {w["student_id"]}',
                       [dr('2400', w['amount_c'], party=w['student_id']), cr('4000', w['amount_c'], party=w['student_id'])],
                       actor, source_ref=f'CONTRACT-{w["contract_id"]}-W{w["week_no"]}')
            conn.execute('UPDATE contract_weeks SET journal_id=? WHERE contract_id=? AND week_no=?',
                         (jid, w['contract_id'], w['week_no']))
        out.append(jid)
    return out


def deliver_textbooks(conn, contract_id, date, actor):
    c = conn.execute('SELECT * FROM contracts WHERE id=?', (contract_id,)).fetchone()
    if c is None:
        raise LedgerError('no such contract')
    if c['book_delivered_on']:
        raise LedgerError('textbook pack already delivered')
    with conn:
        jid = post(conn, date, 'revenue_recognition', f'Textbook pack delivered · {c["student_id"]}',
                   [dr('2400', c['book_c'], party=c['student_id']), cr('4010', c['book_c'], party=c['student_id'])],
                   actor, source_ref=f'BOOK-{c["term_ref"]}')
        conn.execute('UPDATE contracts SET book_delivered_on=? WHERE id=?', (date, contract_id))
    return jid


def contract_liability_schedule(conn, as_of_ym):
    """Contract liability (2400) per student at month end, from the ledger."""
    rows = conn.execute(
        "SELECT party, SUM(credit_c - debit_c) bal FROM journal_lines l JOIN journals j ON j.id=l.journal_id "
        "WHERE l.account='2400' AND j.period<=? GROUP BY party HAVING bal<>0 ORDER BY party", (as_of_ym,)).fetchall()
    return {r['party']: r['bal'] for r in rows}
