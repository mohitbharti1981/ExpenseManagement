# Ledger — Self-Hosted Expense Tracker

A lightweight, self-hosted expense tracker built with Flask and SQLite. Track one-off daily spending and recurring subscriptions side by side, see upcoming bills, and generate filtered reports — all from a single-file backend with no external services required.

## Features

- **Dashboard** — all expenses in one filterable table (search, category, type, date range, tax-only), plus a 30-day view of upcoming recurring bills.
- **Daily Log** — quick one-off expense entry for day-to-day spending.
- **Subscriptions** — manage recurring bills and memberships (amount, frequency, billing details, payment method). Occurrences are computed on the fly from the start date and frequency, so past and future bills show up automatically without a background job.
- **Invoice editing** — double-click any individual occurrence of a recurring subscription to edit just that instance (amount, date, category, notes) without changing the subscription itself.
- **Reports** — summarize spend by category and record type over a date range, with CSV export.
- **Receipt scanning (optional)** — upload a receipt image and auto-fill the daily expense form via local OCR (Tesseract). Falls back gracefully if OCR isn't installed.

## Tech Stack

- **Backend:** Flask (Python)
- **Database:** SQLite (single file, zero configuration)
- **Frontend:** Vanilla HTML/CSS/JS (no build step)
- **OCR (optional):** [pytesseract](https://github.com/madmaze/pytesseract) + Pillow, backed by the [Tesseract](https://github.com/tesseract-ocr/tesseract) engine

## Getting Started

### Prerequisites

- Python 3.9+
- pip

### Installation

```bash
# Clone the repo
git clone https://github.com/<your-username>/ledger.git
cd ledger

# (Recommended) create a virtual environment
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Install dependencies
pip install flask
```

### Optional: Receipt OCR

Receipt scanning works without any extra setup (it just saves the file), but to enable automatic field extraction:

```bash
# macOS
brew install tesseract

# Ubuntu/Debian
sudo apt install tesseract-ocr

pip install pytesseract Pillow
```

### Run the app

```bash
python app.py
```

The database (`expenses.db`) is created automatically on first run. Open **http://localhost:5000** in your browser.

## Project Structure

```
.
├── app.py            # Flask app: routes, business logic, receipt OCR
├── database.py        # SQLite connection + schema/migrations
├── templates/
│   └── index.html      # Single-page frontend (dashboard, forms, reports)
├── uploads/
│   └── receipts/       # Uploaded receipt images (created at runtime)
└── expenses.db         # SQLite database (created at runtime)
```

## API Overview

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/expenses` | List expenses (supports filters: `q`, `category`, `record_type`, `date_from`, `date_to`, `tax_only`) |
| GET | `/api/expense/<id>` | Get a single record |
| DELETE | `/api/expense/<id>` | Delete a record (or exclude a single recurring occurrence) |
| GET | `/api/occurrence/<id>` | Get a single recurring occurrence (real or virtual) for editing |
| GET | `/api/subscriptions` | List active subscription templates |
| GET | `/api/upcoming?days=30` | List bills due in the next N days |
| GET | `/api/categories` | Distinct categories in use |
| GET | `/api/reports/summary` | Totals by category / type for a given filter set |
| GET | `/api/reports/export` | Download filtered results as CSV |
| POST | `/add_expense` | Create or update a daily, recurring, or occurrence record |
| POST | `/api/ocr/receipt` | Upload a receipt image for OCR field extraction |

## Notes

- All amounts are stored and displayed in EUR (`€`).
- Recurring occurrences are **not** pre-generated and stored — they're computed dynamically from each subscription's start date, frequency, and end date every time the dashboard, upcoming list, or reports are loaded. Editing a single occurrence "materializes" just that one date into its own record and excludes it from future virtual generation.
