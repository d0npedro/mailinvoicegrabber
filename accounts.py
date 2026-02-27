"""
Account configuration: load one or more IMAP accounts from a JSON file
or fall back to the single-account environment variables.

JSON format (accounts.json)::

    [
      {
        "label":    "gmail_work",
        "host":     "imap.gmail.com",
        "port":     993,
        "user":     "you@work.com",
        "password": "${GMAIL_WORK_PASSWORD}",
        "folder":   "INBOX"
      },
      {
        "label":    "outlook_personal",
        "host":     "outlook.office365.com",
        "port":     993,
        "user":     "you@personal.com",
        "password": "${OUTLOOK_PERSONAL_PASSWORD}",
        "folder":   "INBOX"
      }
    ]

Password field:
  - Plain string  →  used as-is  (acceptable for local/test use)
  - "${MY_VAR}"   →  resolved from the environment at load time
                     (recommended for production; keeps secrets out of the file)

Label rules:
  - Must be unique across all entries in the file.
  - Used as a sub-directory name under the invoices root, so it must be a
    valid directory name (letters, digits, hyphens, underscores).
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
_ENV_VAR_RE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AccountConfig:
    """Configuration for a single IMAP account."""

    label: str
    host: str
    port: int
    user: str
    password: str
    folder: str = "INBOX"

    def __repr__(self) -> str:
        # Never expose password in repr / logs
        return (
            f"AccountConfig(label={self.label!r}, host={self.host!r}, "
            f"port={self.port}, user={self.user!r}, folder={self.folder!r})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_password(raw: str, label: str) -> str:
    """
    Expand a ``${ENV_VAR}`` reference to its environment value.

    Plain strings are returned unchanged.  Raises ``ValueError`` when the
    referenced variable is not set.
    """
    m = _ENV_VAR_RE.match(raw.strip())
    if m:
        var_name = m.group(1)
        value = os.environ.get(var_name)
        if not value:
            raise ValueError(
                f"Account '{label}': password references ${{{var_name}}} "
                f"but that environment variable is not set"
            )
        return value
    return raw


def _validate_label(label: str) -> None:
    if not _LABEL_RE.match(label):
        raise ValueError(
            f"Account label {label!r} is invalid. "
            "Use only letters, digits, hyphens, and underscores (max 64 chars)."
        )


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_accounts(path: Path) -> list[AccountConfig]:
    """
    Parse *path* as a JSON array of account objects.

    Each entry must contain ``label``, ``host``, ``user``, ``password``.
    ``port`` defaults to 993 and ``folder`` defaults to ``"INBOX"``.

    Raises ``ValueError`` on any validation error so the caller can print a
    clear error message and exit.
    """
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read accounts file '{path}': {exc}") from exc

    if not isinstance(data, list) or not data:
        raise ValueError(
            f"'{path}' must contain a JSON array with at least one account entry"
        )

    accounts: list[AccountConfig] = []
    seen_labels: set[str] = set()

    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"Entry #{idx} in '{path}' is not a JSON object")

        # Required fields
        for key in ("label", "host", "user", "password"):
            if not entry.get(key):
                raise ValueError(
                    f"Entry #{idx} in '{path}' is missing required field '{key}'"
                )

        label = str(entry["label"]).strip()
        _validate_label(label)

        if label in seen_labels:
            raise ValueError(
                f"Duplicate account label '{label}' in '{path}'. Labels must be unique."
            )
        seen_labels.add(label)

        password = _resolve_password(str(entry["password"]), label)

        accounts.append(
            AccountConfig(
                label=label,
                host=str(entry["host"]).strip(),
                port=int(entry.get("port", 993)),
                user=str(entry["user"]).strip(),
                password=password,
                folder=str(entry.get("folder", "INBOX")).strip() or "INBOX",
            )
        )
        logger.debug(f"Loaded account: {accounts[-1]}")

    logger.info(f"Loaded {len(accounts)} account(s) from '{path}'")
    return accounts


def account_from_env() -> AccountConfig:
    """
    Build a single :class:`AccountConfig` from the standard environment
    variables (``IMAP_HOST``, ``IMAP_USER``, etc.).

    This is the backward-compatible single-account path used when
    ``--accounts-file`` is not provided.
    """
    return AccountConfig(
        label="",          # empty → no label sub-directory (legacy layout)
        host=os.environ["IMAP_HOST"],
        port=int(os.environ.get("IMAP_PORT", "993")),
        user=os.environ["IMAP_USER"],
        password=os.environ["IMAP_PASSWORD"],
        folder=os.environ.get("IMAP_FOLDER", "INBOX"),
    )
