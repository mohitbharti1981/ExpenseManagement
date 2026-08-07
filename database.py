import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "expenses.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_name TEXT NOT NULL,
    website_url TEXT,
    username TEXT,
    password TEXT,
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    frequency TEXT,
    expense_date TEXT,
    start_date TEXT,
    end_date TEXT,
    next_billing_date TEXT,
    auto_deduction INTEGER DEFAULT 0,
    payment_method_type TEXT,
    bank_account_last4 TEXT,
    associated_email TEXT,
    associated_phone TEXT,
    billing_address TEXT,
    category TEXT,
    is_tax_deductible INTEGER DEFAULT 0,
    tax_rate_percent REAL DEFAULT 0,
    receipt_url_path TEXT,
    status TEXT DEFAULT 'Active',
    notes TEXT,
    record_type TEXT DEFAULT 'recurring',
    is_template INTEGER DEFAULT 0,
    parent_expense_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS expense_exceptions (
    subscription_id INTEGER NOT NULL,
    occurrence_date TEXT NOT NULL,
    PRIMARY KEY (subscription_id, occurrence_date)
);
"""

MIGRATIONS = [
    ("expense_date", "ALTER TABLE expenses ADD COLUMN expense_date TEXT"),
    ("record_type", "ALTER TABLE expenses ADD COLUMN record_type TEXT DEFAULT 'recurring'"),
    ("created_at", "ALTER TABLE expenses ADD COLUMN created_at TEXT"),
    ("is_template", "ALTER TABLE expenses ADD COLUMN is_template INTEGER DEFAULT 0"),
    ("parent_expense_id", "ALTER TABLE expenses ADD COLUMN parent_expense_id INTEGER"),
]
# Note: is_template / parent_expense_id / next_billing_date are legacy columns,
# no longer used by app.py. Left in place so old DBs don't break; harmless.


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _existing_columns(cursor):
    cursor.execute("PRAGMA table_info(expenses)")
    return {row[1] for row in cursor.fetchall()}


def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.executescript(SCHEMA)
    columns = _existing_columns(cursor)
    for col_name, stmt in MIGRATIONS:
        if col_name not in columns:
            try:
                cursor.execute(stmt)
            except sqlite3.OperationalError:
                pass
    cursor.execute(
        """
        UPDATE expenses
        SET created_at = COALESCE(expense_date, next_billing_date, start_date, datetime('now'))
        WHERE created_at IS NULL OR created_at = ''
        """
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")