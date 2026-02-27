# Mail Invoice Scanner

A production-ready Python 3.11 CLI that connects to an IMAP mailbox, scans
all emails from a given calendar year, detects invoice attachments (German
and English), classifies them with OpenAI, and downloads valid invoices into
a structured directory.

---

## Features

- **IMAP4 SSL** with UID-based search — no in-memory mailbox loading
- **AI classification** via OpenAI `gpt-4o-mini` — handles German invoices
  (`Rechnung`, `MwSt`, `IBAN`, `Steuernummer`, …)
- **Multiple extraction backends** — PDF (pdfplumber → PyPDF2 fallback),
  images (pytesseract OCR, German+English), DOCX (python-docx)
- **Incremental processing** — processed UIDs stored in `processed.json`,
  skip already-seen messages on re-run
- **Structured output** — `invoices/<year>/<vendor>/<invoice_number>_<date>_<amount>_<currency>.pdf`
- **CSV summary** — `invoices_summary_<year>.csv`
- **Security** — filename sanitization, path-traversal prevention, 20 MB
  attachment limit, no credential logging
- **Dry-run mode** — classify without saving files

---

## Requirements

| Requirement | Version |
|---|---|
| Python | 3.10+ |
| tesseract-ocr (system) | 4.x+ |

---

## Quick Start

### 1. Clone / set up the project

```bash
git clone <repo-url>
cd mailinvoice
```

### 2. Create and activate the virtual environment

**Always use `.venv` to keep dependencies isolated from your system Python.**

```bash
python -m venv .venv
```

Activate it:

| Platform | Shell | Command |
|---|---|---|
| Windows | CMD | `.venv\Scripts\activate.bat` |
| Windows | PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows | Git Bash | `source .venv/Scripts/activate` |
| macOS / Linux | bash/zsh | `source .venv/bin/activate` |

Your prompt will show `(.venv)` when active.

### 3. Install Python dependencies

```bash
# Minimum versions (resolves latest compatible releases)
pip install -r requirements.txt

# Or pin to the exact tested versions
pip install -r requirements-lock.txt
```

### 4. Install Tesseract (for image OCR)

**Windows** — download installer from https://github.com/UB-Mannheim/tesseract/wiki
Add the install directory to `PATH`, or set `pytesseract.pytesseract.tesseract_cmd`
in `extractor.py`.

**macOS**
```bash
brew install tesseract tesseract-lang
```

**Debian / Ubuntu**
```bash
sudo apt install tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng
```

### 5. Configure credentials

```bash
cp .env.example .env
# Edit .env — fill in IMAP_HOST, IMAP_USER, IMAP_PASSWORD, OPENAI_API_KEY
```

#### Gmail note
Enable IMAP in Gmail settings and use an **App Password** (not your account
password) if 2-Step Verification is active.

### 6. Run

```bash
# Scan the last full calendar year (default)
python main.py

# Scan a specific year
python main.py --year 2024

# Dry-run: classify but do not save files
python main.py --dry-run

# Custom output directory
python main.py --output-dir /data/invoices

# Verbose logging
python main.py --log-level DEBUG
```

---

## Output Structure

```
invoices/
└── 2024/
    ├── acme_gmbh/
    │   ├── RE-2024-001_2024-03-15_119.00_EUR.pdf
    │   └── RE-2024-002_2024-04-01_238.00_EUR.pdf
    └── unknown_vendor/
        └── invoice.pdf

invoices_summary_2024.csv
processed.json
```

### CSV columns

| Column | Description |
|---|---|
| `vendor` | Vendor name extracted by AI |
| `invoice_number` | Invoice / Rechnungsnummer |
| `date` | Invoice date (ISO 8601) |
| `total_amount` | Gross total |
| `currency` | ISO 4217 currency code |
| `original_filename` | Attachment filename from email |
| `email_subject` | Subject line of the source email |
| `email_date` | Date header of the source email |
| `saved_path` | Local path of the saved file |

---

## Project Structure

```
mailinvoice/
├── main.py          — CLI entry point, orchestration
├── imap_client.py   — IMAP connection, email iteration, attachment dispatch
├── extractor.py     — Text extraction (PDF / image OCR / DOCX)
├── classifier.py    — OpenAI invoice classification
├── storage.py       — File persistence, UID tracking, CSV summary
├── utils.py         — Logging setup, filename sanitization
├── processed.json   — Persisted set of processed email UIDs
├── requirements.txt
├── .env.example
└── README.md
```

---

## Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `IMAP_HOST` | ✅ | — | IMAP server hostname |
| `IMAP_PORT` | — | `993` | IMAP SSL port |
| `IMAP_USER` | ✅ | — | Login username / email address |
| `IMAP_PASSWORD` | ✅ | — | Login password or app password |
| `IMAP_FOLDER` | — | `INBOX` | Mailbox folder to scan |
| `OPENAI_API_KEY` | ✅ | — | OpenAI API key (`sk-…`) |

---

## Limits & Behaviour

| Constraint | Value |
|---|---|
| Maximum attachment size | 20 MB |
| Maximum message size (pre-screen) | 60 MB |
| Maximum text sent to AI | 8 000 characters |
| AI model | `gpt-4o-mini` |
| AI retry on bad JSON | 1 retry (2 attempts total) |
| File types processed | `.pdf` `.png` `.jpg` `.jpeg` `.docx` |

---

## Re-running Safely

`processed.json` stores UIDs grouped by year.  Re-running the scanner skips
all UIDs already in that file.  To re-process everything, delete
`processed.json` or remove the relevant year key.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Missing required environment variables` | `.env` not found / incomplete | Copy `.env.example` → `.env` and fill values |
| `Could not select IMAP folder` | Wrong folder name | Check `IMAP_FOLDER`; Gmail uses `[Gmail]/All Mail` |
| `pytesseract not installed` | Missing system binary | Install `tesseract-ocr` (see Quick Start) |
| `OpenAI authentication failed` | Invalid API key | Verify `OPENAI_API_KEY` in `.env` |
| `pdfplumber failed` | Corrupted / scanned PDF | PyPDF2 fallback is tried automatically |
| No invoices detected | Very short extracted text | Check if PDFs are text-based; enable `--log-level DEBUG` |
