"""
Storage layer: persists invoice files, tracks processed UIDs, writes CSV summary.

Directory layout:
  <base_dir>/<year>/<vendor_sanitized>/<invoice_number>_<date>_<amount>_<currency>.<ext>

Tracking file:
  processed.json  — per-year set of already-processed email UIDs

Summary file:
  invoices_summary_<year>.csv
"""
import csv
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from classifier import ClassificationResult
from utils import sanitize_filename

logger = logging.getLogger(__name__)

_PROCESSED_FILE = Path("processed.json")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class InvoiceRecord:
    vendor: str
    invoice_number: str
    date: str
    total_amount: str
    currency: str
    original_filename: str
    email_subject: str
    email_date: str
    saved_path: str


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class Storage:
    """Manages invoice persistence, UID deduplication, and the CSV summary."""

    def __init__(
        self,
        base_dir: Path,
        year: int,
        dry_run: bool = False,
        account_label: str = "",
    ) -> None:
        self.base_dir = base_dir
        self.year = year
        self.dry_run = dry_run
        self._account_label = account_label

        # When a label is set, namespace both the CSV name and the UID key so
        # multiple accounts never collide in the same processed.json.
        if account_label:
            self.summary_path = Path(f"invoices_summary_{account_label}_{year}.csv")
            self._year_key = f"{account_label}:{year}"
        else:
            self.summary_path = Path(f"invoices_summary_{year}.csv")
            self._year_key = str(year)

        self._processed_all: dict[str, list[str]] = self._load_processed_file()
        self._processed_set: set[str] = set(
            self._processed_all.get(self._year_key, [])
        )
        self._records: list[InvoiceRecord] = []

        # Public counters
        self.processed_count: int = 0
        self.attachment_count: int = 0
        self.invoice_count: int = 0
        self.error_count: int = 0

    # ------------------------------------------------------------------
    # UID tracking
    # ------------------------------------------------------------------

    @staticmethod
    def _load_processed_file() -> dict[str, list[str]]:
        if _PROCESSED_FILE.exists():
            try:
                with _PROCESSED_FILE.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, dict):
                        logger.debug(f"Loaded processed UIDs from {_PROCESSED_FILE}")
                        return data
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    f"Could not read {_PROCESSED_FILE} ({exc}) — starting with empty state"
                )
        return {}

    def _persist_processed(self) -> None:
        self._processed_all[self._year_key] = sorted(self._processed_set)
        try:
            with _PROCESSED_FILE.open("w", encoding="utf-8") as fh:
                json.dump(self._processed_all, fh, indent=2)
        except OSError as exc:
            logger.error(f"Could not write {_PROCESSED_FILE}: {exc}")

    def is_processed(self, uid: str) -> bool:
        return uid in self._processed_set

    def mark_processed(self, uid: str) -> None:
        self._processed_set.add(uid)
        self._persist_processed()

    # ------------------------------------------------------------------
    # Counters
    # ------------------------------------------------------------------

    def increment_processed(self) -> None:
        self.processed_count += 1

    def increment_attachments(self) -> None:
        self.attachment_count += 1

    def increment_errors(self) -> None:
        self.error_count += 1

    @property
    def records(self) -> list[InvoiceRecord]:
        """Read-only view of all invoice records collected this run."""
        return list(self._records)

    # ------------------------------------------------------------------
    # Invoice saving
    # ------------------------------------------------------------------

    def save_invoice(
        self,
        filename: str,
        data: bytes,
        classification: ClassificationResult,
        email_subject: str,
        email_date: str,
    ) -> None:
        """
        Persist an invoice attachment to the structured directory.

        Path: <base_dir>/<year>/<invoice_number>_<date>_<amount>_<currency>.<ext>
        Multi-account: <base_dir>/<label>/<year>/<invoice_number>_…

        All invoices for the same year land in a single flat folder.
        Vendor is still captured in the filename and the CSV summary.
        Falls back to the original filename when metadata is incomplete.
        """
        ext = Path(filename).suffix.lower() or ".pdf"
        # Multi-account: base_dir / label / year
        # Single-account: base_dir / year
        if self._account_label:
            output_dir = self.base_dir / self._account_label / str(self.year)
        else:
            output_dir = self.base_dir / str(self.year)

        # Build output filename from metadata when available
        has_meta = (
            classification.invoice_number not in ("unknown", "")
            and classification.date not in ("unknown", "")
        )
        if has_meta:
            parts = [
                sanitize_filename(classification.invoice_number),
                sanitize_filename(classification.date),
                sanitize_filename(classification.total_amount),
                sanitize_filename(classification.currency),
            ]
            out_name = "_".join(parts) + ext
        else:
            out_name = sanitize_filename(Path(filename).stem) + ext

        out_path = output_dir / out_name

        # Enforce path containment — prevent any traversal attack
        try:
            out_path.resolve().relative_to(output_dir.resolve())
        except ValueError:
            logger.error(
                f"Path traversal detected for '{filename}' → '{out_path}' — skipping"
            )
            self.error_count += 1
            return

        if not self.dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)

            # Avoid silent overwrites: append counter suffix when needed
            if out_path.exists():
                stem, counter = out_path.stem, 1
                while out_path.exists():
                    out_path = output_dir / f"{stem}_{counter}{ext}"
                    counter += 1

            try:
                out_path.write_bytes(data)
                logger.info(f"Saved invoice → {out_path}")
            except OSError as exc:
                logger.error(f"Could not write invoice to '{out_path}': {exc}")
                self.error_count += 1
                return

        self.invoice_count += 1
        self._records.append(
            InvoiceRecord(
                vendor=classification.vendor,
                invoice_number=classification.invoice_number,
                date=classification.date,
                total_amount=classification.total_amount,
                currency=classification.currency,
                original_filename=filename,
                email_subject=email_subject,
                email_date=email_date,
                saved_path=str(out_path) if not self.dry_run else "(dry-run)",
            )
        )

    # ------------------------------------------------------------------
    # CSV summary
    # ------------------------------------------------------------------

    _CSV_FIELDS = [
        "vendor",
        "invoice_number",
        "date",
        "total_amount",
        "currency",
        "original_filename",
        "email_subject",
        "email_date",
        "saved_path",
    ]

    def write_summary(self) -> None:
        """Write all invoice records to a CSV file."""
        if not self._records:
            logger.info("No invoices detected — summary CSV not written")
            return

        try:
            with self.summary_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=self._CSV_FIELDS)
                writer.writeheader()
                for rec in self._records:
                    writer.writerow(
                        {
                            "vendor": rec.vendor,
                            "invoice_number": rec.invoice_number,
                            "date": rec.date,
                            "total_amount": rec.total_amount,
                            "currency": rec.currency,
                            "original_filename": rec.original_filename,
                            "email_subject": rec.email_subject,
                            "email_date": rec.email_date,
                            "saved_path": rec.saved_path,
                        }
                    )
            logger.info(
                f"Summary CSV written → {self.summary_path} "
                f"({len(self._records)} record(s))"
            )
        except OSError as exc:
            logger.error(f"Failed to write CSV summary: {exc}")
