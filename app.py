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
    stepping by frequency, stopping early if end_date is exceeded."""
    current = parse_date(start_date_str)
    if not current:
        return
    end_limit = parse_date(end_date_str) if end_date_str else None
    guard = 0
    while current and current <= until_date and guard < 2000:
        if end_limit and current > end_limit:
            return
        yield current.isoformat()
        nxt = advance_date(current, frequency)
        if not nxt:
            return
        current = nxt
        guard += 1


def generate_recurring_occurrences(conn, until_date=None):
    """Compute virtual expense occurrences for every subscription, straight
    from start_date + frequency — nothing is written to the database. Dates
    the user explicitly deleted (expense_exceptions) are skipped."""
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

    occurrences = []
    for sub in subs:
        for d in iter_occurrence_dates(
            sub["start_date"], sub["frequency"], sub["end_date"], until_date
        ):
            if (sub["id"], d) in skip:
                continue
            occurrences.append(
                {
                    "id": f"v{sub['id']}_{d}",
                    "template_id": sub["id"],
                    "vendor_name": sub["vendor_name"],
                    "category": sub["category"],
                    "amount": sub["amount"],
                    "currency": sub["currency"],
                    "frequency": sub["frequency"],
                    "expense_date": d,
                    "status": sub["status"],
                    "is_tax_deductible": sub["is_tax_deductible"],
                    "notes": sub["notes"],
                }
            )
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
                   expense_date, status, is_tax_deductible, record_type, notes
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
                    }
                )

    return items


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/add_expense", methods=["POST"])
def add_expense():
    expense_id = request.form.get("expense_id")
    data = parse_expense_form(request.form)

    conn = get_connection()
    cursor = conn.cursor()

    if expense_id:
        data["id"] = int(expense_id)
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
                record_type=:record_type
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
                receipt_url_path, status, notes, record_type
            ) VALUES (
                :vendor_name, :website_url, :username, :password, :amount, :currency, :frequency,
                :expense_date, :start_date, :end_date, :auto_deduction,
                :payment_method_type, :bank_account_last4, :associated_email, :associated_phone,
                :billing_address, :category, :is_tax_deductible, :tax_rate_percent,
                :receipt_url_path, :status, :notes, :record_type
            )
        """
        msg = "Expense added successfully!"
        status_code = 201

    try:
        cursor.execute(query, data)
        conn.commit()
        return jsonify({"status": "success", "message": msg}), status_code
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    finally:
        conn.close()


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

    conn = get_connection()
    subs = conn.execute(
        "SELECT * FROM expenses WHERE record_type = 'recurring' AND parent_expense_id IS NULL AND status = 'Active'"
    ).fetchall()
    conn.close()

    upcoming = []
    for sub in subs:
        d = parse_date(sub["start_date"])
        end_limit = parse_date(sub["end_date"]) if sub["end_date"] else None
        guard = 0
        while d and d <= today and guard < 2000:
            d = advance_date(d, sub["frequency"])
            guard += 1
        if d and (not end_limit or d <= end_limit) and today < d <= end:
            upcoming.append(
                {
                    "id": sub["id"],
                    "vendor": sub["vendor_name"],
                    "category": sub["category"],
                    "amount": sub["amount"],
                    "currency": sub["currency"],
                    "frequency": sub["frequency"],
                    "start_date": sub["start_date"],
                    "next_billing": d.isoformat(),
                    "status": sub["status"],
                }
            )
    upcoming.sort(key=lambda x: x["next_billing"])
    return jsonify(upcoming)


def extract_receipt_fields(text):
    """Best-effort parsing from OCR text — free local heuristic, not ML."""
    amount = None
    amount_match = re.search(
        r"(?:total|amount|sum|€|eur)\s*[:\s]*(\d+[.,]\d{2})", text, re.IGNORECASE
    )
    if not amount_match:
        amount_match = re.search(r"(\d+[.,]\d{2})\s*(?:€|EUR)?", text)
    if amount_match:
        amount = float(amount_match.group(1).replace(",", "."))

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
        from PIL import Image

        if ext == ".pdf":
            return jsonify(
                {
                    "status": "partial",
                    "message": "PDF saved. Install pdf2image + poppler for PDF OCR. Fields not auto-filled.",
                    "receipt_path": save_path,
                }
            )

        image = Image.open(save_path)
        text = pytesseract.image_to_string(image)
        fields = extract_receipt_fields(text)
        fields["receipt_url_path"] = save_path
        fields["status"] = "success"
        return jsonify(fields)
    except ImportError:
        return jsonify(
            {
                "status": "partial",
                "message": "Receipt saved. Install pytesseract + Pillow + Tesseract for OCR.",
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
