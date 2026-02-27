#!/usr/bin/env python3
"""
Mail Invoice Scanner
====================
Connects to an IMAP mailbox, scans emails from a given calendar year,
detects invoice attachments (German & English), classifies them with AI,
and downloads valid invoices into a structured directory.

Usage:
    python main.py               # scans last full calendar year
    python main.py --year 2024   # scans a specific year
    python main.py --dry-run     # classify but do not save files

Setup:
    Copy .env.example → .env and fill in credentials, then:
        pip install -r requirements.txt
        python main.py
"""
import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Load .env BEFORE importing any module that reads env vars
from dotenv import load_dotenv

load_dotenv()

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
            "Scan an IMAP mailbox for invoice attachments, classify them with AI "
            "(OpenAI gpt-4o-mini), and download valid invoices into a structured directory."
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
            "Defaults to the parent of --output-dir (i.e. the project root). "
            "Pass --no-tax-export to skip this step."
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

_REQUIRED_ENV = ["IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "OPENAI_API_KEY"]


def _validate_env() -> None:
    missing = [v for v in _REQUIRED_ENV if not os.environ.get(v, "").strip()]
    if missing:
        print(
            f"ERROR: Missing required environment variable(s): {', '.join(missing)}\n"
            "Copy .env.example → .env and fill in your credentials.",
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
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    setup_logging(args.log_level)
    _validate_env()
    _validate_year(args.year)

    tax_export_root = args.tax_export_dir or args.output_dir.parent

    logger.info("=" * 60)
    logger.info("Mail Invoice Scanner")
    logger.info(f"  Scanning year  : {args.year}")
    logger.info(f"  Output dir     : {args.output_dir.resolve()}")
    logger.info(f"  Dry-run mode   : {args.dry_run}")
    if not args.no_tax_export:
        logger.info(f"  Tax export dir : {tax_export_root.resolve()}")
    logger.info("=" * 60)

    storage = Storage(
        base_dir=args.output_dir,
        year=args.year,
        dry_run=args.dry_run,
    )

    try:
        with IMAPClient() as client:
            client.process_emails(year=args.year, storage=storage)

    except KeyboardInterrupt:
        logger.info("Interrupted by user — saving progress and exiting")
        storage.write_summary()
        _run_tax_export(storage, args, tax_export_root)
        sys.exit(0)

    except Exception as exc:
        logger.exception(f"Fatal error: {exc}")
        storage.write_summary()
        sys.exit(1)

    # Final summary
    logger.info("=" * 60)
    logger.info("Scan complete")
    logger.info(f"  Emails processed    : {storage.processed_count}")
    logger.info(f"  Attachments scanned : {storage.attachment_count}")
    logger.info(f"  Invoices saved      : {storage.invoice_count}")
    logger.info(f"  Errors              : {storage.error_count}")
    logger.info("=" * 60)

    storage.write_summary()

    if storage.invoice_count > 0:
        logger.info(f"Summary CSV → {storage.summary_path}")

    _run_tax_export(storage, args, tax_export_root)


def _run_tax_export(storage: Storage, args: argparse.Namespace, tax_export_root: Path) -> None:
    """Run the post-processing tax classification export (unless skipped)."""
    if args.no_tax_export:
        return
    if args.dry_run:
        logger.info("Tax export skipped in dry-run mode (no files were saved)")
        return
    if storage.invoice_count == 0:
        logger.info("Tax export skipped — no invoices were saved this run")
        return

    logger.info("=" * 60)
    logger.info("Tax Classification Export")
    logger.info(f"  Source : {args.output_dir.resolve()}")
    logger.info(f"  Target : {tax_export_root.resolve()}")
    logger.info("=" * 60)

    try:
        summary = export_tax_folders(
            invoices_root=args.output_dir,
            output_root=tax_export_root,
            records=storage.records,
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
