-- A Academy ledger schema. All money is INTEGER cents. Posted records are immutable.

CREATE TABLE IF NOT EXISTS accounts (
  code      TEXT PRIMARY KEY,
  name      TEXT NOT NULL,
  type      TEXT NOT NULL CHECK (type IN ('asset','liability','equity','income','expense')),
  bas_label TEXT,                       -- e.g. 'W1' for gross wages
  cf_class  TEXT                        -- cash-flow class for bank postings (see ledger.CF_CLASSES)
);

CREATE TABLE IF NOT EXISTS tax_codes (
  code        TEXT PRIMARY KEY,
  rate_bp     INTEGER NOT NULL CHECK (rate_bp >= 0),   -- 1000 = 10%
  description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS periods (
  ym        TEXT PRIMARY KEY CHECK (ym GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
  status    TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  closed_at TEXT,
  closed_by TEXT
);

CREATE TABLE IF NOT EXISTS journals (
  id          INTEGER PRIMARY KEY,
  journal_date TEXT NOT NULL,
  period      TEXT NOT NULL REFERENCES periods(ym),
  source      TEXT NOT NULL,            -- invoice, receipt, bill, payroll, depreciation, bank_fee, reversal, opening
  source_ref  TEXT,                     -- document number (invoice no., bill no., pay run id ...)
  description TEXT NOT NULL,
  cf_class    TEXT,                     -- for journals touching the bank account
  reverses    INTEGER REFERENCES journals(id),
  posted_at   TEXT NOT NULL,
  posted_by   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journal_lines (
  id          INTEGER PRIMARY KEY,
  journal_id  INTEGER NOT NULL REFERENCES journals(id),
  account     TEXT NOT NULL REFERENCES accounts(code),
  debit_c     INTEGER NOT NULL DEFAULT 0 CHECK (debit_c >= 0),
  credit_c    INTEGER NOT NULL DEFAULT 0 CHECK (credit_c >= 0),
  tax_code    TEXT REFERENCES tax_codes(code),
  tax_c       INTEGER NOT NULL DEFAULT 0 CHECK (tax_c >= 0),
  party       TEXT,                     -- student id, supplier, employee
  CHECK ((debit_c = 0) <> (credit_c = 0))
);

CREATE TABLE IF NOT EXISTS audit_log (
  id        INTEGER PRIMARY KEY,
  at        TEXT NOT NULL,
  actor     TEXT NOT NULL,
  action    TEXT NOT NULL,
  detail    TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS students (
  id        TEXT PRIMARY KEY,
  name      TEXT NOT NULL,
  class_id  TEXT,
  start_ym  TEXT NOT NULL,
  end_ym    TEXT,                       -- first month NOT attending
  fee_c     INTEGER NOT NULL CHECK (fee_c >= 0),
  tax_code  TEXT NOT NULL REFERENCES tax_codes(code),
  leave_reason TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
  id         INTEGER PRIMARY KEY,
  number     TEXT NOT NULL UNIQUE,
  student_id TEXT NOT NULL REFERENCES students(id),
  ym         TEXT NOT NULL,
  net_c      INTEGER NOT NULL,
  tax_c      INTEGER NOT NULL,
  total_c    INTEGER NOT NULL,
  journal_id INTEGER NOT NULL REFERENCES journals(id),
  UNIQUE (student_id, ym)
);

CREATE TABLE IF NOT EXISTS allocations (   -- receipts matched to specific invoices
  id          INTEGER PRIMARY KEY,
  invoice_id  INTEGER NOT NULL REFERENCES invoices(id),
  journal_id  INTEGER NOT NULL REFERENCES journals(id),
  amount_c    INTEGER NOT NULL CHECK (amount_c > 0)
);

CREATE TABLE IF NOT EXISTS bank_transactions (
  id         INTEGER PRIMARY KEY,
  bank_ref   TEXT NOT NULL UNIQUE,      -- reference from the bank statement
  txn_date   TEXT NOT NULL,
  amount_c   INTEGER NOT NULL,          -- +in / -out
  description TEXT,
  journal_id INTEGER REFERENCES journals(id)
);

CREATE TABLE IF NOT EXISTS bills (
  id         INTEGER PRIMARY KEY,
  number     TEXT NOT NULL UNIQUE,      -- supplier invoice number
  supplier   TEXT NOT NULL,
  bill_date  TEXT NOT NULL,
  net_c      INTEGER NOT NULL,
  tax_c      INTEGER NOT NULL,
  total_c    INTEGER NOT NULL,
  journal_id INTEGER NOT NULL REFERENCES journals(id),
  paid_journal_id INTEGER REFERENCES journals(id)
);

CREATE TABLE IF NOT EXISTS employees (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  wage_account TEXT NOT NULL REFERENCES accounts(code),  -- 5000 teaching or 5010 admin
  rate_c      INTEGER NOT NULL CHECK (rate_c > 0),       -- hourly rate, cents
  payg_scale  TEXT NOT NULL,
  super_fund  TEXT,
  active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS timesheets (
  id          INTEGER PRIMARY KEY,
  employee_id TEXT NOT NULL REFERENCES employees(id),
  work_date   TEXT NOT NULL,
  hours_milli INTEGER NOT NULL CHECK (hours_milli > 0),  -- 1.5 h = 1500
  class_id    TEXT,
  approved_by TEXT,
  pay_run_id  INTEGER REFERENCES pay_runs(id),
  UNIQUE (employee_id, work_date, class_id)
);

CREATE TABLE IF NOT EXISTS pay_runs (
  id        INTEGER PRIMARY KEY,
  pay_date  TEXT NOT NULL UNIQUE,
  journal_id INTEGER NOT NULL REFERENCES journals(id)
);

CREATE TABLE IF NOT EXISTS pay_lines (
  id          INTEGER PRIMARY KEY,
  pay_run_id  INTEGER NOT NULL REFERENCES pay_runs(id),
  employee_id TEXT NOT NULL REFERENCES employees(id),
  gross_c     INTEGER NOT NULL,
  payg_c      INTEGER NOT NULL,
  super_c     INTEGER NOT NULL,
  net_c       INTEGER NOT NULL,
  payg_source TEXT NOT NULL              -- which loaded table produced the withholding
);

-- Rates are only ever loaded from an official source; nothing is hard-coded here.
CREATE TABLE IF NOT EXISTS rates (
  name           TEXT NOT NULL,          -- 'GST', 'SG'
  effective_from TEXT NOT NULL,
  rate_bp        INTEGER NOT NULL,
  source_ref     TEXT NOT NULL,
  PRIMARY KEY (name, effective_from)
);

-- PAYG withholding scale bands from the ATO statement for the scale in use.
CREATE TABLE IF NOT EXISTS payg_tables (
  scale          TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  lower_c        INTEGER NOT NULL,      -- band starts above this amount
  upper_c        INTEGER,               -- NULL = no upper limit
  base_c         INTEGER NOT NULL,      -- withholding at the band's lower edge
  rate_bp        INTEGER NOT NULL,      -- marginal rate above lower_c
  source_ref     TEXT NOT NULL,         -- ATO document and edition
  PRIMARY KEY (scale, effective_from, lower_c)
);

CREATE TABLE IF NOT EXISTS assets (
  id            INTEGER PRIMARY KEY,
  description   TEXT NOT NULL,
  cost_c        INTEGER NOT NULL CHECK (cost_c > 0),
  purchase_ym   TEXT NOT NULL,
  life_months   INTEGER NOT NULL CHECK (life_months > 0),
  journal_id    INTEGER NOT NULL REFERENCES journals(id)
);

CREATE TABLE IF NOT EXISTS depreciation_runs (
  asset_id   INTEGER NOT NULL REFERENCES assets(id),
  ym         TEXT NOT NULL,
  amount_c   INTEGER NOT NULL,
  journal_id INTEGER NOT NULL REFERENCES journals(id),
  PRIMARY KEY (asset_id, ym)
);

-- AASB 15 customer contracts (prepaid term). Revenue is recognised from the contract
-- liability as performance obligations are satisfied, never at invoice date.
CREATE TABLE IF NOT EXISTS contracts (
  id             INTEGER PRIMARY KEY,
  student_id     TEXT NOT NULL REFERENCES students(id),
  term_ref       TEXT NOT NULL UNIQUE,
  start_date     TEXT NOT NULL,
  weeks          INTEGER NOT NULL CHECK (weeks > 0),
  transaction_price_c INTEGER NOT NULL,     -- ex GST
  service_c      INTEGER NOT NULL,          -- allocated to the over-time term obligation
  book_c         INTEGER NOT NULL,          -- allocated to the textbook pack (point in time)
  tax_c          INTEGER NOT NULL,          -- GST on the invoice (tax point)
  invoice_journal_id INTEGER NOT NULL REFERENCES journals(id),
  book_delivered_on TEXT
);

CREATE TABLE IF NOT EXISTS contract_weeks (
  contract_id  INTEGER NOT NULL REFERENCES contracts(id),
  week_no      INTEGER NOT NULL,
  recognise_on TEXT NOT NULL,              -- last day of the week of service
  amount_c     INTEGER NOT NULL CHECK (amount_c > 0),
  journal_id   INTEGER REFERENCES journals(id),
  PRIMARY KEY (contract_id, week_no)
);

-- Immutability: posted records cannot be changed or removed.
CREATE TRIGGER IF NOT EXISTS journals_no_update BEFORE UPDATE ON journals
BEGIN SELECT RAISE(ABORT, 'posted journals are immutable: post a reversal instead'); END;
CREATE TRIGGER IF NOT EXISTS journals_no_delete BEFORE DELETE ON journals
BEGIN SELECT RAISE(ABORT, 'posted journals are immutable: post a reversal instead'); END;
CREATE TRIGGER IF NOT EXISTS lines_no_update BEFORE UPDATE ON journal_lines
BEGIN SELECT RAISE(ABORT, 'posted journal lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS lines_no_delete BEFORE DELETE ON journal_lines
BEGIN SELECT RAISE(ABORT, 'posted journal lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
