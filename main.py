#!/usr/bin/env python3
"""
Mail Invoice Scanner
====================
Connects to one or more IMAP mailboxes, scans emails from a given calendar
year, detects invoice attachments (German & English), classifies them with AI,
and downloads valid invoices into a structured directory.

Single account (env-vars, backward-compatible):
    python main.py
    python main.py --year 2024 --dry-run

Multiple accounts (JSON config file):
    cp accounts.example.json accounts.json   # fill in your details
    python main.py --accounts-file accounts.json

Setup:
    Copy .env.example → .env, fill in credentials, then:
        pip install -r requirements.txt
        python main.py
"""
import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from accounts import AccountConfig, account_from_env, load_accounts  # noqa: E402
from imap_client import IMAPClient  # noqa: E402
from storage import Storage  # noqa: E402
from tax_export import export_tax_folders  # noqa: E402
from utils import setup_logging  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    current_year = datetime.now().year
    default_year = current_year - 1

    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Scan one or more IMAP mailboxes for invoice attachments, "
            "classify them with AI (OpenAI gpt-4o-mini), and download valid "
            "invoices into a structured directory."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--year",
        type=int,
        default=default_year,
        metavar="YYYY",
        help=f"Calendar year to scan (default: {default_year}, the last full year)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("invoices"),
        metavar="DIR",
        help="Base directory for downloaded invoices",
    )
    parser.add_argument(
        "--accounts-file",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "JSON file containing one or more IMAP account definitions. "
            "See accounts.example.json for the format. "
            "When omitted, credentials are read from IMAP_* environment variables."
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Verbosity of log output",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify and report invoices but do NOT write any files",
    )
    parser.add_argument(
        "--tax-export-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Root directory for ABSETZBAR / NICHT_ABSETZBAR export folders. "
            "Defaults to the parent of --output-dir (project root). "
            "Use --no-tax-export to skip this step entirely."
        ),
    )
    parser.add_argument(
        "--no-tax-export",
        action="store_true",
        help="Skip the post-processing tax classification export step",
    )
    return parser


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_IMAP_ENV = ["IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"]
_OPENAI_ENV = ["OPENAI_API_KEY"]


def _validate_env() -> None:
    """Full validation for single-account mode (env vars only)."""
    missing = [
        v for v in (_IMAP_ENV + _OPENAI_ENV)
        if not os.environ.get(v, "").strip()
    ]
    if missing:
        print(
            f"ERROR: Missing required environment variable(s): {', '.join(missing)}\n"
            "Copy .env.example → .env and fill in your credentials.",
            file=sys.stderr,
        )
        sys.exit(1)


def _validate_openai_key() -> None:
    """Validate only the OpenAI key (multi-account mode, IMAP creds come from file)."""
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        print(
            "ERROR: OPENAI_API_KEY is not set.\n"
            "Add it to your .env file or export it in your shell.",
            file=sys.stderr,
        )
        sys.exit(1)


def _validate_year(year: int) -> None:
    current = datetime.now().year
    if year >= current:
        logger.error(
            f"--year {year} is not a completed calendar year "
            f"(current year is {current}). Use {current - 1} or earlier."
        )
        sys.exit(1)
    if year < 2000:
        logger.error(f"--year {year} looks invalid. Please check your argument.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Per-account processing
# ---------------------------------------------------------------------------


def _process_account(
    account: AccountConfig,
    args: argparse.Namespace,
    use_label: bool,
) -> Storage:
    """
    Run the full scan pipeline for a single *account*.

    *use_label* controls whether the account label is inserted into the
    invoice directory path (multi-account mode) or omitted (single-account /
    backward-compatible mode).

    Returns the populated :class:`Storage` so the caller can aggregate stats.
    """
    label = account.label if use_label else ""

    storage = Storage(
        base_dir=args.output_dir,
        year=args.year,
        dry_run=args.dry_run,
        account_label=label,
    )

    logger.info("-" * 60)
    logger.info(f"Account : {account.user}")
    if label:
        logger.info(f"Label   : {label}")
    logger.info(f"Server  : {account.host}:{account.port}  folder={account.folder}")
    logger.info("-" * 60)

    try:
        with IMAPClient(account) as client:
            client.process_emails(year=args.year, storage=storage)
    except Exception as exc:
        logger.exception(f"Error scanning account '{account.user}': {exc}")
        storage.increment_errors()

    storage.write_summary()
    if storage.invoice_count > 0:
        logger.info(f"Summary CSV → {storage.summary_path}")

    logger.info(
        f"Account '{account.user}' done — "
        f"processed={storage.processed_count}  "
        f"attachments={storage.attachment_count}  "
        f"invoices={storage.invoice_count}  "
        f"errors={storage.error_count}"
    )
    return storage


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    setup_logging(args.log_level)
    _validate_year(args.year)

    # Load accounts ────────────────────────────────────────────────────────
    if args.accounts_file:
        if not args.accounts_file.exists():
            print(
                f"ERROR: Accounts file not found: {args.accounts_file}\n"
                "Copy accounts.example.json → accounts.json and fill in your details.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            accounts = load_accounts(args.accounts_file)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        _validate_openai_key()
        # Always embed the label in the path when using an accounts file so
        # multiple accounts never clobber each other's files.
        use_label = True
    else:
        _validate_env()
        accounts = [account_from_env()]
        # Single env-var account: no label → backward-compatible path layout.
        use_label = False

    tax_export_root = args.tax_export_dir or args.output_dir.parent

    logger.info("=" * 60)
    logger.info("Mail Invoice Scanner")
    logger.info(f"  Scanning year  : {args.year}")
    logger.info(f"  Output dir     : {args.output_dir.resolve()}")
    logger.info(f"  Accounts       : {len(accounts)}")
    logger.info(f"  Dry-run mode   : {args.dry_run}")
    if not args.no_tax_export:
        logger.info(f"  Tax export dir : {tax_export_root.resolve()}")
    logger.info("=" * 60)

    # Scan all accounts ────────────────────────────────────────────────────
    all_storage: list[Storage] = []

    try:
        for account in accounts:
            storage = _process_account(account, args, use_label)
            all_storage.append(storage)
    except KeyboardInterrupt:
        logger.info("Interrupted by user — saving progress and exiting")
        for s in all_storage:
            s.write_summary()
        _run_tax_export(all_storage, args, tax_export_root)
        sys.exit(0)

    # Aggregate summary ────────────────────────────────────────────────────
    total_processed = sum(s.processed_count for s in all_storage)
    total_attachments = sum(s.attachment_count for s in all_storage)
    total_invoices = sum(s.invoice_count for s in all_storage)
    total_errors = sum(s.error_count for s in all_storage)

    logger.info("=" * 60)
    logger.info("All accounts complete")
    if len(all_storage) > 1:
        logger.info(f"  Accounts scanned    : {len(all_storage)}")
    logger.info(f"  Emails processed    : {total_processed}")
    logger.info(f"  Attachments scanned : {total_attachments}")
    logger.info(f"  Invoices saved      : {total_invoices}")
    logger.info(f"  Errors              : {total_errors}")
    logger.info("=" * 60)

    _run_tax_export(all_storage, args, tax_export_root)


# ---------------------------------------------------------------------------
# Tax export helper
# ---------------------------------------------------------------------------


def _run_tax_export(
    all_storage: list[Storage],
    args: argparse.Namespace,
    tax_export_root: Path,
) -> None:
    """Run post-processing tax classification export across all accounts."""
    if args.no_tax_export:
        return
    if args.dry_run:
        logger.info("Tax export skipped in dry-run mode (no files were saved)")
        return

    total_invoices = sum(s.invoice_count for s in all_storage)
    if total_invoices == 0:
        logger.info("Tax export skipped — no invoices were saved this run")
        return

    # Merge records from all accounts for richer signal
    all_records = [rec for s in all_storage for rec in s.records]

    logger.info("=" * 60)
    logger.info("Tax Classification Export")
    logger.info(f"  Source : {args.output_dir.resolve()}")
    logger.info(f"  Target : {tax_export_root.resolve()}")
    logger.info("=" * 60)

    try:
        summary = export_tax_folders(
            invoices_root=args.output_dir,
            output_root=tax_export_root,
            records=all_records,
        )
    except Exception as exc:
        logger.exception(f"Tax export failed: {exc}")
        return

    logger.info("Tax export complete")
    logger.info(f"  Total invoices processed : {summary.total}")
    logger.info(f"  Deductible (ABSETZBAR)   : {summary.deductible}")
    logger.info(f"  Not deductible           : {summary.not_deductible}")
    if summary.errors:
        logger.warning(f"  Copy errors              : {summary.errors}")
    logger.info(f"  → {tax_export_root / 'ABSETZBAR'}")
    logger.info(f"  → {tax_export_root / 'NICHT_ABSETZBAR'}")


if __name__ == "__main__":
    main()
