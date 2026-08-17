import csv
import io
import os
import re
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, render_template, request, send_file

from database import get_connection, init_db

app = Flask(__name__)
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads", "receipts")
os.makedirs(UPLOAD_DIR, exist_ok=True)

init_db()

import calendar


def _days_in_month(year, month):
    return calendar.monthrange(year, month)[1]


def parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None


def advance_date(d, frequency):
    """Return the next billing date for a given frequency, or None if the
    frequency doesn't recur (One-time / Other / unrecognized)."""
    if isinstance(d, str):
        d = parse_date(d)
    if not d:
        return None
    freq = (frequency or "").strip().lower()

    def _add_months(base, months):
        month = base.month - 1 + months
        year = base.year + month // 12
        month = month % 12 + 1
        day = min(base.day, _days_in_month(year, month))
        return date(year, month, day)

    if freq == "monthly":
        return _add_months(d, 1)
    if freq == "quarterly":
        return _add_months(d, 3)
    if freq == "half yearly":
        return _add_months(d, 6)
    if freq == "annually":
        try:
            return d.replace(year=d.year + 1)
        except ValueError:
            return d.replace(year=d.year + 1, day=28)  # Feb 29 fallback
    return None  # One-time / Other / unset — doesn't recur


def iter_occurrence_dates(start_date_str, frequency, end_date_str, until_date):
    """Yield ISO date strings from start_date up to and including until_date,
    stopping when end_date is exceeded (not included)."""
    current = parse_date(start_date_str)
    if not current:
        return
    end_limit = parse_date(end_date_str) if end_date_str else None
    guard = 0
    while current and current <= until_date and guard < 2000:
        if end_limit and current >= end_limit:  # Changed > to >= to exclude end date
            return
        yield current.isoformat()
        nxt = advance_date(current, frequency)
        if not nxt:
            return
        current = nxt
        guard += 1


def generate_recurring_occurrences(
    conn, until_date=None, from_date=None, only_active=False
):
    if until_date is None:
        until_date = date.today()
    cursor = conn.cursor()
    subs = cursor.execute(
        "SELECT * FROM expenses WHERE record_type = 'recurring' AND parent_expense_id IS NULL"
    ).fetchall()
    exceptions = cursor.execute(
        "SELECT subscription_id, occurrence_date FROM expense_exceptions"
    ).fetchall()
    skip = {(r["subscription_id"], r["occurrence_date"]) for r in exceptions}

    # Fetch overrides
    overrides_rows = cursor.execute(
        "SELECT subscription_id, occurrence_date, amount, category, status, is_tax_deductible, notes, expense_date FROM occurrence_overrides"
    ).fetchall()
    overrides = {}
    for r in overrides_rows:
        key = (r["subscription_id"], r["occurrence_date"])
        overrides[key] = {
            "amount": r["amount"],
            "category": r["category"],
            "status": r["status"],
            "is_tax_deductible": r["is_tax_deductible"],
            "notes": r["notes"],
            "expense_date": r["expense_date"],
        }

    occurrences = []
    for sub in subs:
        # Convert to dict to safely use .get()
        sub = dict(sub)
        if only_active and sub["status"] != "Active":
            continue

        effective_end = sub["end_date"]
        deactivated_on = sub.get("deactivated_on")  # now works with dict
        if deactivated_on and (not effective_end or deactivated_on < effective_end):
            effective_end = deactivated_on

        for d in iter_occurrence_dates(
            sub["start_date"], sub["frequency"], effective_end, until_date
        ):
            if from_date and d < from_date:
                continue
            if (sub["id"], d) in skip:
                continue

            occ = {
                "id": f"v{sub['id']}_{d}",
                "template_id": sub["id"],
                "vendor_name": sub["vendor_name"],
                "category": sub["category"],
                "amount": sub["amount"],
                "currency": sub["currency"],
                "frequency": sub["frequency"],
                "start_date": sub["start_date"],
                "expense_date": d,
                "status": sub["status"],
                "is_tax_deductible": sub["is_tax_deductible"],
                "notes": sub["notes"],
                "auto_deduction": sub["auto_deduction"],  # <-- ADD THIS LINE
            }
            override = overrides.get((sub["id"], d))
            if override:
                if override["amount"] is not None:
                    occ["amount"] = override["amount"]
                if override["category"]:
                    occ["category"] = override["category"]
                if override["status"]:
                    occ["status"] = override["status"]
                if override["is_tax_deductible"] is not None:
                    occ["is_tax_deductible"] = override["is_tax_deductible"]
                if override["notes"] is not None:
                    occ["notes"] = override["notes"]
                if override["expense_date"]:
                    occ["expense_date"] = override["expense_date"]
            occurrences.append(occ)
    return occurrences


def matches_python_filters(item):
    """Same filters as build_expense_filters, applied to an in-memory dict —
    used for virtual recurring occurrences, which never touch the DB."""
    args = request.args
    q = args.get("q", "").strip().lower()
    if q:
        haystack = " ".join(
            [
                str(item.get("vendor_name") or ""),
                str(item.get("category") or ""),
                str(item.get("notes") or ""),
            ]
        ).lower()
        if q not in haystack:
            return False
    category = args.get("category", "").strip()
    if category and item.get("category") != category:
        return False
    record_type = args.get("record_type", "").strip()
    if record_type and record_type != "recurring":
        return False
    if args.get("tax_only", "").strip() == "1" and not item.get("is_tax_deductible"):
        return False
    status = args.get("status", "").strip()
    if status and item.get("status") != status:
        return False
    date_from = args.get("date_from", "").strip()
    if date_from and (item.get("expense_date") or "") < date_from:
        return False
    date_to = args.get("date_to", "").strip()
    if date_to and (item.get("expense_date") or "") > date_to:
        return False
    return True


def row_to_dict(row):
    return dict(row) if row else None


def parse_expense_form(form):
    record_type = form.get("record_type", "recurring")
    expense_date = form.get("expense_date") or None
    start_date = form.get("start_date") or None

    if record_type == "daily" and not expense_date:
        expense_date = date.today().isoformat()

    return {
        "vendor_name": form.get("vendor_name"),
        "website_url": form.get("website_url"),
        "username": form.get("username"),
        "password": form.get("password"),
        "amount": float(form.get("amount") or 0),
        "currency": form.get("currency", "EUR"),
        "frequency": form.get("frequency")
        or ("One-time" if record_type == "daily" else None),
        "expense_date": expense_date,
        "start_date": start_date,
        "end_date": form.get("end_date") or None,
        "auto_deduction": 1 if form.get("auto_deduction") == "on" else 0,
        "payment_method_type": form.get("payment_method_type"),
        "bank_account_last4": form.get("bank_account_last4"),
        "associated_email": form.get("associated_email"),
        "associated_phone": form.get("associated_phone"),
        "billing_address": form.get("billing_address"),
        "category": form.get("category"),
        "is_tax_deductible": 1 if form.get("is_tax_deductible") == "on" else 0,
        "tax_rate_percent": 0,
        "receipt_url_path": form.get("receipt_url_path"),
        "status": form.get("status", "Active"),
        "notes": form.get("notes"),
        "record_type": record_type,
    }


def build_expense_filters():
    clauses = []
    params = []

    q = request.args.get("q", "").strip()
    if q:
        clauses.append(
            "(vendor_name LIKE ? OR category LIKE ? OR notes LIKE ? OR website_url LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])

    category = request.args.get("category", "").strip()
    if category:
        clauses.append("category = ?")
        params.append(category)

    tax_only = request.args.get("tax_only", "").strip()
    if tax_only == "1":
        clauses.append("is_tax_deductible = 1")

    status = request.args.get("status", "").strip()
    if status:
        clauses.append("status = ?")
        params.append(status)

    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    if date_from:
        clauses.append("expense_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("expense_date <= ?")
        params.append(date_to)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _get_all_expense_items(conn):
    """Real 'daily' DB rows + virtual recurring occurrences, filtered
    identically, merged into one list of plain dicts."""
    where, params = build_expense_filters()
    record_type = request.args.get("record_type", "").strip()
    cursor = conn.cursor()
    items = []

    if record_type != "recurring":
        joiner = "AND" if where else "WHERE"
        cursor.execute(
            f"""
            SELECT id, vendor_name, category, amount, currency, frequency,
                   expense_date, status, is_tax_deductible, record_type, notes, auto_deduction
            FROM expenses
            {where} {joiner} record_type = 'daily'
            """,
            params,
        )
        for r in cursor.fetchall():
            items.append(
                {
                    "id": r["id"],
                    "template_id": None,
                    "vendor_name": r["vendor_name"],
                    "category": r["category"],
                    "amount": r["amount"],
                    "currency": r["currency"],
                    "frequency": r["frequency"],
                    "expense_date": r["expense_date"],
                    "status": r["status"],
                    "tax_considerate": r["is_tax_deductible"] == 1,
                    "record_type": "daily",
                    "notes": r["notes"],
                    "auto_deduction": r["auto_deduction"],
                }
            )

    if record_type != "daily":
        for occ in generate_recurring_occurrences(conn):
            if matches_python_filters(occ):
                items.append(
                    {
                        "id": occ["id"],
                        "template_id": occ["template_id"],
                        "vendor_name": occ["vendor_name"],
                        "category": occ["category"],
                        "amount": occ["amount"],
                        "currency": occ["currency"],
                        "frequency": occ["frequency"],
                        "expense_date": occ["expense_date"],
                        "status": occ["status"],
                        "tax_considerate": occ["is_tax_deductible"] == 1,
                        "record_type": "recurring",
                        "notes": occ["notes"],
                        "auto_deduction": occ["auto_deduction"],
                    }
                )

    return items


@app.route("/")
def home():
    return render_template("index.html")


def _parse_virtual_id(expense_id):
    """Return (subscription_id, occurrence_date) if expense_id is a virtual
    recurring-occurrence id like 'v12_2026-03-01', else None."""
    if not expense_id or not expense_id.startswith("v") or "_" not in expense_id:
        return None
    try:
        sub_id_str, occ_date = expense_id[1:].split("_", 1)
        return int(sub_id_str), occ_date
    except ValueError:
        return None


@app.route("/add_expense", methods=["POST"])
def add_expense():
    expense_id = request.form.get("expense_id")
    edit_scope = request.form.get("edit_scope")
    data = parse_expense_form(request.form)

    conn = get_connection()
    cursor = conn.cursor()

    # ---------- Handle editing an occurrence (invoice) of a subscription ----------
    virtual = _parse_virtual_id(expense_id) if edit_scope == "occurrence" else None
    if virtual:
        sub_id, occ_date = virtual
        sub = conn.execute("SELECT * FROM expenses WHERE id = ?", (sub_id,)).fetchone()
        if not sub:
            conn.close()
            return jsonify(
                {"status": "error", "message": "Subscription not found"}
            ), 404

        # Upsert override for this occurrence
        try:
            cursor.execute(
                """
                INSERT INTO occurrence_overrides (
                    subscription_id, occurrence_date, amount, category, status,
                    is_tax_deductible, notes, expense_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subscription_id, occurrence_date) DO UPDATE SET
                    amount = excluded.amount,
                    category = excluded.category,
                    status = excluded.status,
                    is_tax_deductible = excluded.is_tax_deductible,
                    notes = excluded.notes,
                    expense_date = excluded.expense_date
            """,
                (
                    sub_id,
                    occ_date,
                    data["amount"] if data["amount"] != sub["amount"] else None,
                    data["category"] if data["category"] != sub["category"] else None,
                    data["status"] if data["status"] != sub["status"] else None,
                    data["is_tax_deductible"]
                    if data["is_tax_deductible"] != sub["is_tax_deductible"]
                    else None,
                    data["notes"] if data["notes"] != sub["notes"] else None,
                    data["expense_date"] if data["expense_date"] != occ_date else None,
                ),
            )
            conn.commit()
            conn.close()
            return jsonify(
                {"status": "success", "message": "Invoice updated successfully!"}
            ), 200
        except Exception as e:
            conn.close()
            return jsonify({"status": "error", "message": str(e)}), 400

    # ---------- Handle editing the subscription itself or adding new ----------
    if expense_id:
        data["id"] = int(expense_id)

    # Track deactivation for subscriptions
    if data["record_type"] == "recurring":
        existing_status_row = None
        if expense_id:
            existing_status_row = conn.execute(
                "SELECT status, deactivated_on FROM expenses WHERE id = ?",
                (data["id"],),
            ).fetchone()
        if data["status"] != "Active":
            already_deactivated = (
                existing_status_row
                and existing_status_row["status"] != "Active"
                and existing_status_row["deactivated_on"]
            )
            data["deactivated_on"] = (
                existing_status_row["deactivated_on"]
                if already_deactivated
                else date.today().isoformat()
            )
        else:
            data["deactivated_on"] = None
    else:
        data["deactivated_on"] = None

    if expense_id:
        query = """
            UPDATE expenses SET
                vendor_name=:vendor_name, website_url=:website_url, username=:username,
                password=:password, amount=:amount, currency=:currency, frequency=:frequency,
                expense_date=:expense_date, start_date=:start_date, end_date=:end_date,
                auto_deduction=:auto_deduction,
                payment_method_type=:payment_method_type, bank_account_last4=:bank_account_last4,
                associated_email=:associated_email, associated_phone=:associated_phone,
                billing_address=:billing_address, category=:category,
                is_tax_deductible=:is_tax_deductible, tax_rate_percent=:tax_rate_percent,
                receipt_url_path=:receipt_url_path, status=:status, notes=:notes,
                record_type=:record_type, deactivated_on=:deactivated_on
            WHERE id=:id
        """
        msg = "Expense updated successfully!"
        status_code = 200
    else:
        query = """
            INSERT INTO expenses (
                vendor_name, website_url, username, password, amount, currency, frequency,
                expense_date, start_date, end_date, auto_deduction,
                payment_method_type, bank_account_last4, associated_email, associated_phone,
                billing_address, category, is_tax_deductible, tax_rate_percent,
                receipt_url_path, status, notes, record_type, deactivated_on
            ) VALUES (
                :vendor_name, :website_url, :username, :password, :amount, :currency, :frequency,
                :expense_date, :start_date, :end_date, :auto_deduction,
                :payment_method_type, :bank_account_last4, :associated_email, :associated_phone,
                :billing_address, :category, :is_tax_deductible, :tax_rate_percent,
                :receipt_url_path, :status, :notes, :record_type, :deactivated_on
            )
        """
        msg = "Expense added successfully!"
        status_code = 201

    try:
        cursor.execute(query, data)
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": msg}), status_code
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/expenses")
def get_expenses_api():
    conn = get_connection()
    items = _get_all_expense_items(conn)
    conn.close()
    items.sort(key=lambda x: (x["expense_date"] or "", str(x["id"])), reverse=True)
    return jsonify(
        [
            {
                "id": i["id"],
                "template_id": i["template_id"],
                "vendor": i["vendor_name"],
                "category": i["category"],
                "amount": i["amount"],
                "currency": i["currency"],
                "frequency": i["frequency"],
                "display_date": i["expense_date"],
                "status": i["status"],
                "tax_considerate": i["tax_considerate"],
                "record_type": i["record_type"],
                "notes": i["notes"],
                "auto_deduction": i["auto_deduction"],
            }
            for i in items
        ]
    )


@app.route("/api/expense/<expense_id>")
def get_single_expense(expense_id):
    try:
        eid = int(expense_id)
    except ValueError:
        return jsonify({"error": "Not found"}), 404
    conn = get_connection()
    row = conn.execute("SELECT * FROM expenses WHERE id = ?", (eid,)).fetchone()
    conn.close()
    return jsonify(row_to_dict(row)) if row else (jsonify({"error": "Not found"}), 404)


@app.route("/api/expense/<expense_id>", methods=["DELETE"])
def delete_expense(expense_id):
    conn = get_connection()

    if expense_id.startswith("v") and "_" in expense_id:
        try:
            sub_id_str, occ_date = expense_id[1:].split("_", 1)
            sub_id = int(sub_id_str)
        except ValueError:
            conn.close()
            return jsonify({"status": "error", "message": "Invalid id"}), 400
        conn.execute(
            "INSERT OR IGNORE INTO expense_exceptions (subscription_id, occurrence_date) VALUES (?, ?)",
            (sub_id, occ_date),
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "That occurrence was removed."})

    try:
        eid = int(expense_id)
    except ValueError:
        conn.close()
        return jsonify({"status": "error", "message": "Invalid id"}), 400
    row = conn.execute("SELECT id FROM expenses WHERE id = ?", (eid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"status": "error", "message": "Not found"}), 404
    conn.execute("DELETE FROM expenses WHERE id = ?", (eid,))
    conn.execute("DELETE FROM expense_exceptions WHERE subscription_id = ?", (eid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "Expense deleted successfully!"})


@app.route("/api/occurrence/<occurrence_id>")
def get_occurrence(occurrence_id):
    virtual = _parse_virtual_id(occurrence_id)
    conn = get_connection()
    if virtual:
        sub_id, occ_date = virtual
        sub = conn.execute("SELECT * FROM expenses WHERE id = ?", (sub_id,)).fetchone()
        conn.close()
        if not sub:
            return jsonify({"error": "Not found"}), 404
        d = dict(sub)
        d["expense_date"] = occ_date
        return jsonify(d)

    try:
        eid = int(occurrence_id)
    except ValueError:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    row = conn.execute("SELECT * FROM expenses WHERE id = ?", (eid,)).fetchone()
    conn.close()
    return jsonify(row_to_dict(row)) if row else (jsonify({"error": "Not found"}), 404)


@app.route("/api/subscriptions")
def get_subscriptions_api():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM expenses WHERE record_type = 'recurring' AND parent_expense_id IS NULL"
        ).fetchall()
        return jsonify([dict(row) for row in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/categories")
def get_categories():
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT category FROM expenses WHERE category IS NOT NULL AND category != '' ORDER BY category"
    ).fetchall()
    conn.close()
    return jsonify([r["category"] for r in rows])


@app.route("/api/reports/summary")
def report_summary():
    conn = get_connection()
    items = _get_all_expense_items(conn)
    conn.close()

    total = sum(i["amount"] for i in items)
    tax_total = sum(i["amount"] for i in items if i["tax_considerate"])
    by_cat, by_type = {}, {}
    for i in items:
        c = i["category"] or "Uncategorized"
        by_cat.setdefault(c, {"category": c, "count": 0, "total": 0.0})
        by_cat[c]["count"] += 1
        by_cat[c]["total"] += i["amount"]
        t = i["record_type"]
        by_type.setdefault(t, {"record_type": t, "count": 0, "total": 0.0})
        by_type[t]["count"] += 1
        by_type[t]["total"] += i["amount"]

    return jsonify(
        {
            "count": len(items),
            "total": round(total, 2),
            "tax_considerate_total": round(tax_total, 2),
            "by_category": sorted(by_cat.values(), key=lambda x: -x["total"]),
            "by_type": sorted(by_type.values(), key=lambda x: -x["total"]),
        }
    )


@app.route("/api/reports/export")
def export_csv():
    conn = get_connection()
    items = _get_all_expense_items(conn)
    conn.close()
    items.sort(key=lambda x: x["expense_date"] or "", reverse=True)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "For Person",
            "Category",
            "Amount",
            "Currency",
            "Frequency",
            "Date",
            "Status",
            "Tax Considerate",
            "Record Type",
            "Notes",
        ]
    )
    for i in items:
        writer.writerow(
            [
                i["vendor_name"],
                i["category"],
                i["amount"],
                i["currency"],
                i["frequency"],
                i["expense_date"],
                i["status"],
                "Yes" if i["tax_considerate"] else "No",
                i["record_type"],
                i["notes"],
            ]
        )

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8-sig"))
    mem.seek(0)
    filename = f"expense_report_{date.today().isoformat()}.csv"
    return send_file(
        mem, mimetype="text/csv", as_attachment=True, download_name=filename
    )


@app.route("/api/upcoming")
def upcoming_expenses():
    days = int(request.args.get("days", 30))
    today = date.today()
    end = today + timedelta(days=days)
    window_start = (today + timedelta(days=1)).isoformat()

    conn = get_connection()

    # Reuse the same occurrence generator used everywhere else, so exceptions
    # (deleted occurrences) and the deactivation cutoff are respected here too
    # — the old version here had its own separate loop that ignored both.
    occs = generate_recurring_occurrences(
        conn, until_date=end, from_date=window_start, only_active=True
    )
    upcoming = [
        {
            "id": o["template_id"],
            "vendor": o["vendor_name"],
            "category": o["category"],
            "amount": o["amount"],
            "currency": o["currency"],
            "frequency": o["frequency"],
            "start_date": o["start_date"],
            "next_billing": o["expense_date"],
            "status": o["status"],
        }
        for o in occs
    ]

    # If a future occurrence was individually edited (materialized into its
    # own 'daily' record via the invoice editor), show the overridden
    # amount/date instead of silently keeping the old subscription defaults.
    materialized = conn.execute(
        """
        SELECT e.*, p.status AS parent_status
        FROM expenses e
        JOIN expenses p ON p.id = e.parent_expense_id
        WHERE e.record_type = 'daily' AND e.parent_expense_id IS NOT NULL
          AND e.expense_date >= ? AND e.expense_date <= ?
          AND p.status = 'Active'
        """,
        (window_start, end.isoformat()),
    ).fetchall()
    conn.close()

    for m in materialized:
        upcoming.append(
            {
                "id": m["parent_expense_id"],
                "vendor": m["vendor_name"],
                "category": m["category"],
                "amount": m["amount"],
                "currency": m["currency"],
                "frequency": m["frequency"],
                "start_date": m["expense_date"],
                "next_billing": m["expense_date"],
                "status": m["parent_status"],
            }
        )

    upcoming.sort(key=lambda x: x["next_billing"])
    return jsonify(upcoming)


def _parse_amount_str(raw):
    """Normalize a matched amount string like '1,245.99', '1.245,99', or
    '45,99' into a float, handling both US and EU thousands/decimal styles."""
    raw = raw.strip()
    if "," in raw and "." in raw:
        # Whichever separator appears last is the decimal separator.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        # Single comma: decimal separator only if exactly 2 digits follow.
        if re.search(r",\d{2}$", raw):
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def extract_receipt_fields(text):
    """Best-effort parsing from OCR text — free local heuristic, not ML."""
    amount = None
    currency_symbols = r"€|£|\$|EUR|USD|GBP"
    # A currency-shaped number: 1-3 leading digits, optional thousands
    # groups, optional 1-2 digit decimal. Deliberately does NOT allow
    # arbitrary runs of digits/separators (that previously let the regex
    # cross a line break and swallow part of a date, e.g. "15.03.2026",
    # as the amount).
    num = r"\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?"
    # [ \t:]* (not \s*) keeps the match on a single line, so a trigger
    # word/symbol can't reach across a newline into unrelated digits.
    # \btotal\b (not just "total") so "Subtotal" doesn't get mistaken for
    # the grand total.
    amount_match = re.search(
        rf"(?:\btotal\b|\bamount\b|\bsum\b|{currency_symbols})[ \t:]*"
        rf"(?:{currency_symbols})?[ \t]*({num})",
        text,
        re.IGNORECASE,
    )
    if not amount_match:
        amount_match = re.search(rf"({num})[ \t]*(?:{currency_symbols})?", text)
    if amount_match:
        amount = _parse_amount_str(amount_match.group(1))

    expense_date = None
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if date_match:
        expense_date = date_match.group(1)
    else:
        date_match = re.search(r"(\d{2}[./-]\d{2}[./-]\d{4})", text)
        if date_match:
            raw = date_match.group(1).replace("/", "-").replace(".", "-")
            parts = raw.split("-")
            if len(parts) == 3:
                expense_date = f"{parts[2]}-{parts[1]}-{parts[0]}"

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    vendor = lines[0][:80] if lines else None

    return {
        "vendor_name": vendor,
        "amount": amount,
        "expense_date": expense_date or date.today().isoformat(),
        "notes": "OCR extracted — please verify before saving.",
        "raw_text": text[:2000],
    }


# @app.route("/api/ocr/receipt", methods=["POST"])
# def ocr_receipt():
#     if "receipt" not in request.files:
#         return jsonify({"status": "error", "message": "No file uploaded"}), 400

#     file = request.files["receipt"]
#     if not file.filename:
#         return jsonify({"status": "error", "message": "Empty filename"}), 400

#     ext = os.path.splitext(file.filename)[1].lower()
#     if ext not in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
#         return jsonify(
#             {"status": "error", "message": "Supported: PNG, JPG, WEBP, PDF"}
#         ), 400

#     ts = datetime.now().strftime("%Y%m%d_%H%M%S")
#     save_path = os.path.join(UPLOAD_DIR, f"{ts}_{file.filename}")
#     file.save(save_path)

#     try:
#         import pytesseract
#         from PIL import Image, ImageOps
#     except ImportError:
#         return jsonify(
#             {
#                 "status": "partial",
#                 "message": "Receipt saved. Install pytesseract + Pillow for OCR: "
#                 "pip install pytesseract Pillow",
#                 "receipt_url_path": save_path,
#             }
#         )

#     if ext == ".pdf":
#         return jsonify(
#             {
#                 "status": "partial",
#                 "message": "PDF saved. Install pdf2image + poppler for PDF OCR. Fields not auto-filled.",
#                 "receipt_path": save_path,
#             }
#         )

#     try:
#         image = Image.open(save_path)
#         # Basic preprocessing improves OCR accuracy on phone-camera photos:
#         # normalize orientation, convert to grayscale, boost contrast, and
#         # upscale small images so text isn't too thin to recognize.
#         image = ImageOps.exif_transpose(image)
#         image = ImageOps.grayscale(image)
#         image = ImageOps.autocontrast(image)
#         if max(image.size) < 1500:
#             scale = 1500 / max(image.size)
#             image = image.resize(
#                 (int(image.width * scale), int(image.height * scale)), Image.LANCZOS
#             )

#         text = pytesseract.image_to_string(image)
#         fields = extract_receipt_fields(text)
#         fields["receipt_url_path"] = save_path
#         fields["status"] = "success"
#         return jsonify(fields)
#     except pytesseract.TesseractNotFoundError:
#         return jsonify(
#             {
#                 "status": "partial",
#                 "message": "Receipt saved. The Tesseract binary isn't installed or "
#                 "isn't on your PATH — install it (e.g. 'brew install tesseract' "
#                 "on macOS, 'apt install tesseract-ocr' on Linux).",
#                 "receipt_url_path": save_path,
#             }
#         )
#     except Exception as e:
#         return jsonify(
#             {
#                 "status": "partial",
#                 "message": f"Receipt saved but OCR failed: {e}",
#                 "receipt_url_path": save_path,
#             }
#         )


@app.route("/api/ocr/receipt", methods=["POST"])
def ocr_receipt():
    if "receipt" not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400

    file = request.files["receipt"]
    if not file.filename:
        return jsonify({"status": "error", "message": "Empty filename"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
        return jsonify(
            {"status": "error", "message": "Supported: PNG, JPG, WEBP, PDF"}
        ), 400

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = os.path.join(UPLOAD_DIR, f"{ts}_{file.filename}")
    file.save(save_path)

    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError:
        return jsonify(
            {
                "status": "partial",
                "message": "Receipt saved. Install pytesseract + Pillow for OCR: "
                "pip install pytesseract Pillow",
                "receipt_url_path": save_path,
            }
        )

    # Handle PDF files
    if ext == ".pdf":
        try:
            from pdf2image import convert_from_path

            # Convert PDF to images (first page only for speed, or all pages)
            images = convert_from_path(save_path, first_page=1, last_page=1, dpi=300)

            if not images:
                return jsonify(
                    {
                        "status": "partial",
                        "message": "PDF has no pages to scan.",
                        "receipt_url_path": save_path,
                    }
                )

            # Process the first page
            image = images[0]

            # Apply same preprocessing as images
            image = ImageOps.grayscale(image)
            image = ImageOps.autocontrast(image)
            if max(image.size) < 1500:
                scale = 1500 / max(image.size)
                image = image.resize(
                    (int(image.width * scale), int(image.height * scale)), Image.LANCZOS
                )

            # Run OCR
            text = pytesseract.image_to_string(image)
            fields = extract_receipt_fields(text)
            fields["receipt_url_path"] = save_path
            fields["status"] = "success"

            # Add a note about PDF scanning
            fields["notes"] = (
                f"OCR extracted from PDF — please verify before saving. {fields.get('notes', '')}"
            )

            return jsonify(fields)

        except ImportError:
            return jsonify(
                {
                    "status": "partial",
                    "message": "PDF saved but cannot scan. Install pdf2image: pip install pdf2image (also requires poppler: brew install poppler)",
                    "receipt_path": save_path,
                }
            )
        except Exception as e:
            return jsonify(
                {
                    "status": "partial",
                    "message": f"PDF saved but OCR failed: {e!s}",
                    "receipt_url_path": save_path,
                }
            )

    # Handle image files (existing code)
    try:
        image = Image.open(save_path)
        # Basic preprocessing improves OCR accuracy on phone-camera photos:
        # normalize orientation, convert to grayscale, boost contrast, and
        # upscale small images so text isn't too thin to recognize.
        image = ImageOps.exif_transpose(image)
        image = ImageOps.grayscale(image)
        image = ImageOps.autocontrast(image)
        if max(image.size) < 1500:
            scale = 1500 / max(image.size)
            image = image.resize(
                (int(image.width * scale), int(image.height * scale)), Image.LANCZOS
            )

        text = pytesseract.image_to_string(image)
        fields = extract_receipt_fields(text)
        fields["receipt_url_path"] = save_path
        fields["status"] = "success"
        return jsonify(fields)
    except pytesseract.TesseractNotFoundError:
        return jsonify(
            {
                "status": "partial",
                "message": "Receipt saved. The Tesseract binary isn't installed or "
                "isn't on your PATH — install it (e.g. 'brew install tesseract' "
                "on macOS, 'apt install tesseract-ocr' on Linux).",
                "receipt_url_path": save_path,
            }
        )
    except Exception as e:
        return jsonify(
            {
                "status": "partial",
                "message": f"Receipt saved but OCR failed: {e}",
                "receipt_url_path": save_path,
            }
        )


if __name__ == "__main__":
    app.run(debug=True)
