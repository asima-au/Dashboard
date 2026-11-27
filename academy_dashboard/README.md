# A Academy · Operations & Accounting Dashboard

A single-file dashboard (`index.html`) for an education academy. No install, no server, no dependencies: open it in a browser.

## Sections

1. **Overview**: KPIs, automated alerts, enrolment, revenue, cash and waitlist charts
2. **Students & enrolment**: new, returning and leaving students, leave reasons, student register
3. **Fees & payments**: billing, collections, receivables aging, reminder queue
4. **Admin & staff**: expense matrix, hour-based teacher pay, admin payroll, unpaid bills
5. **Classes & capacity**: capacity by month, class list, weekly timetable
6. **BAS & tax**: quarterly BAS summary, BAS worksheet (G1, 1A, G10, G11, 1B, W1, W2), compliance calendar
7. **3-statement pack**: monthly P&L, balance sheet (with balance check) and cash flow
8. **Growth & risk**: 6-month projection at a chosen monthly growth rate, scenario comparison, threat register

Data & settings (top right) holds CSV import, data-quality checks and all assumptions.

## Use it

1. Open `index.html`. The sample data (6 months, enrolments growing 200% a month) loads automatically.
2. Click **Data & settings**. Download the CSV templates, fill them in, and import each file.
3. Review the assumptions (GST, super, PAYG, fees, seats, teacher rates) and confirm them with your accountant.

## CSV columns

| File | Columns |
|---|---|
| students | student_id, name, class_id, start_month, end_month, fee, returning (Y/N), leave_reason |
| payments | payment_date, student_id, amount_inc_gst |
| expenses | date, type (opex / capex / payroll), category, payee, description, amount_ex_gst, gst_claimable (Y/N), paid_date (blank = unpaid), receipt_attached (Y/N) |
| timesheets | month, teacher, class_id, hours, hourly_rate |
| classes | class_id, name, teacher, day, start_time, seats |

Dates accept `YYYY-MM-DD` or `DD/MM/YYYY`.

## Method and limits

- The statements are built from the transaction tables. The balance sheet is checked every month.
- Receivables are allocated to invoices oldest first, because payments do not carry an invoice number. Adding `invoice_no` to payments would give exact aging.
- PAYG uses a flat average rate. Replace it with ATO withholding tables for real payroll.
- Fees are treated as GST-inclusive by default. Confirm whether your accredited courses are GST-free.
- BAS figures are management estimates, not a lodged BAS. Have a registered BAS agent review them.
- Imported data lives in the page only and is lost on reload. Export or keep your source CSVs as the record.
