"""
Tax Classification Export
=========================
Post-processing step that copies every saved invoice into one of two folders:

  output_root/ABSETZBAR/       — likely tax-deductible (§ 9 EStG, home-office / Arbeitsmittel)
  output_root/NICHT_ABSETZBAR/ — not tax-deductible

Classification is keyword-based: the vendor name, original filename, and email
subject extracted during the main run are searched for work-related terms
common to a German employed software developer.  No second AI call is made.

Typical call after the invoice pipeline has finished::

    from tax_export import export_tax_folders
    summary = export_tax_folders(
        invoices_root=Path("invoices"),
        output_root=Path("."),
        records=storage.records,       # optional but recommended
    )
    print(f"Deductible: {summary.deductible}  Not deductible: {summary.not_deductible}")
"""
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from storage import InvoiceRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword catalogue
# ---------------------------------------------------------------------------

# Each entry is a lowercase substring; matching is case-insensitive.
_DEDUCTIBLE_KEYWORDS: frozenset[str] = frozenset(
    [
        # ── Hardware / peripherals ───────────────────────────────────────────
        "pc",
        "laptop",
        "notebook",
        "computer",
        "monitor",
        "display",
        "grafik",
        "graphics",
        "mainboard",
        "motherboard",
        "ram",
        "arbeitsspeicher",
        "ssd",
        "festplatte",
        "hard drive",
        "tastatur",
        "keyboard",
        "maus",
        "mouse",
        "headset",
        "webcam",
        "mikrofon",
        "microphone",
        "drucker",
        "printer",
        "scanner",
        "usb hub",
        "docking",
        "dockingstation",
        "kvm",
        "netzteil",
        "power supply",
        "ups",
        "netzwerk",
        "router",
        "switch",
        "netzwerkkabel",
        # ── Software & cloud services ────────────────────────────────────────
        "software",
        "license",
        "licence",
        "lizenz",
        "subscription",
        "abonnement",
        "developer tool",
        "entwicklerwerkzeug",
        "ide",
        "editor",
        "plugin",
        "extension",
        "github",
        "gitlab",
        "bitbucket",
        "jira",
        "confluence",
        "slack",
        "figma",
        "jetbrains",
        "visual studio",
        "vscode",
        "xcode",
        "aws",
        "azure",
        "gcp",
        "google cloud",
        "cloud",
        "hosting",
        "server",
        "domain",
        "ssl",
        "vpn",
        # ── Education & training ─────────────────────────────────────────────
        "online course",
        "onlinekurs",
        "kurs",
        "weiterbildung",
        "fortbildung",
        "schulung",
        "training",
        "seminar",
        "workshop",
        "certification",
        "zertifizierung",
        "book",
        "buch",
        "fachbuch",
        "ebook",
        "tutorial",
        "udemy",
        "coursera",
        "pluralsight",
        "linkedin learning",
        "o'reilly",
        "oreilly",
        "manning",
        "packt",
        "apress",
        # ── Home-office furniture & equipment ────────────────────────────────
        "desk",
        "schreibtisch",
        "office chair",
        "bürostuhl",
        "buerostuhl",
        "ergonomisch",
        "ergonomic",
        "monitor arm",
        "monitorarm",
        "tischhalterung",
        "stehpult",
        "standing desk",
        "höhenverstellbar",
        "regal",
        "shelf",
        "aktenschrank",
        "filing cabinet",
        "schreibtischlampe",
        "desk lamp",
        "beleuchtung",
        "lighting",
    ]
)

_FOLDER_DEDUCTIBLE = "ABSETZBAR"
_FOLDER_NOT_DEDUCTIBLE = "NICHT_ABSETZBAR"

_INVOICE_EXTENSIONS: frozenset[str] = frozenset(
    [".pdf", ".png", ".jpg", ".jpeg", ".docx"]
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TaxExportSummary:
    """Result counters returned by :func:`export_tax_folders`."""

    total: int
    deductible: int
    not_deductible: int
    errors: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_deductible(signal: str) -> bool:
    """Return True if *signal* contains at least one deductible keyword."""
    lowered = signal.lower()
    return any(kw in lowered for kw in _DEDUCTIBLE_KEYWORDS)


def _signal_from_record(record: "InvoiceRecord") -> str:
    """Build a classification signal string from an InvoiceRecord."""
    return " ".join(
        filter(
            None,
            [
                record.vendor,
                record.original_filename,
                record.email_subject,
                record.invoice_number,
            ],
        )
    )


def _signal_from_path(path: Path, invoices_root: Path) -> str:
    """
    Build a classification signal from the file path alone.

    Uses the vendor directory name and the file stem as signal, which is
    sufficient when no in-memory records are available (e.g. standalone run).
    """
    try:
        relative = path.relative_to(invoices_root)
        parts = list(relative.parent.parts) + [path.stem]
    except ValueError:
        parts = [path.stem]
    return " ".join(parts)


def _safe_copy(src: Path, dest_dir: Path) -> Optional[Path]:
    """
    Copy *src* to *dest_dir* using shutil.copy2 (preserves metadata).

    Appends an integer suffix if the destination filename already exists.
    Returns the final destination path, or None on error.
    """
    dest = dest_dir / src.name
    if dest.exists():
        stem, ext, counter = src.stem, src.suffix, 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{counter}{ext}"
            counter += 1
    try:
        shutil.copy2(src, dest)
        return dest
    except OSError as exc:
        logger.error(f"copy2 failed: {src} → {dest_dir}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def export_tax_folders(
    invoices_root: Path,
    output_root: Path,
    records: Optional[list] = None,  # list[InvoiceRecord] — optional but recommended
) -> TaxExportSummary:
    """
    Copy every saved invoice into ABSETZBAR or NICHT_ABSETZBAR.

    Parameters
    ----------
    invoices_root:
        Root directory that contains the saved invoices
        (e.g. ``Path("invoices")``).  Scanned recursively.
    output_root:
        Parent directory for the two export folders.  Typically the project
        root (``Path(".")``).  Created if it does not exist.
    records:
        Optional list of :class:`storage.InvoiceRecord` objects from the
        current run.  When supplied, the vendor name and email subject from
        the AI classification are used as the primary deductibility signal,
        which is more accurate than path-only heuristics.

    Returns
    -------
    TaxExportSummary
        Counts of total, deductible, not-deductible, and errored files.
    """
    deductible_dir = output_root / _FOLDER_DEDUCTIBLE
    not_deductible_dir = output_root / _FOLDER_NOT_DEDUCTIBLE
    deductible_dir.mkdir(parents=True, exist_ok=True)
    not_deductible_dir.mkdir(parents=True, exist_ok=True)

    # Build a fast lookup: saved_path (str) → signal string
    path_to_signal: dict[str, str] = {}
    if records:
        for rec in records:
            if rec.saved_path and rec.saved_path != "(dry-run)":
                path_to_signal[rec.saved_path] = _signal_from_record(rec)

    # Collect invoice files
    if not invoices_root.exists():
        logger.warning(
            f"invoices_root '{invoices_root}' does not exist — tax export skipped"
        )
        return TaxExportSummary(total=0, deductible=0, not_deductible=0, errors=0)

    invoice_files = sorted(
        p
        for p in invoices_root.rglob("*")
        if p.is_file() and p.suffix.lower() in _INVOICE_EXTENSIONS
    )

    if not invoice_files:
        logger.info("No invoice files found in '%s' — tax export skipped", invoices_root)
        return TaxExportSummary(total=0, deductible=0, not_deductible=0, errors=0)

    total = deductible = not_deductible = errors = 0

    for invoice_path in invoice_files:
        total += 1

        # Prefer record-based signal; fall back to path heuristic
        signal = path_to_signal.get(
            str(invoice_path), _signal_from_path(invoice_path, invoices_root)
        )

        if _is_deductible(signal):
            dest_dir = deductible_dir
            deductible += 1
            label = _FOLDER_DEDUCTIBLE
        else:
            dest_dir = not_deductible_dir
            not_deductible += 1
            label = _FOLDER_NOT_DEDUCTIBLE

        dest = _safe_copy(invoice_path, dest_dir)
        if dest is None:
            errors += 1
            total -= 1  # don't count failed copies in totals
            deductible -= label == _FOLDER_DEDUCTIBLE
            not_deductible -= label == _FOLDER_NOT_DEDUCTIBLE
        else:
            logger.debug(f"[{label}] {invoice_path.name} → {dest}")

    return TaxExportSummary(
        total=total,
        deductible=deductible,
        not_deductible=not_deductible,
        errors=errors,
    )
