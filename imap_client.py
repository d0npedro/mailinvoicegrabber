"""
IMAP client: connects to the mailbox, iterates emails in a date range,
streams attachments, and coordinates extraction + classification.

Design choices:
- UID-based search and fetch (stable across reconnects)
- Emails fetched one at a time (memory-efficient for large mailboxes)
- RFC822.SIZE pre-check to skip oversized messages early
- Attachment parts decoded individually from MIME tree
"""
import email
import email.header
import imaplib
import logging
from email.message import Message
from typing import Generator, Optional

from accounts import AccountConfig
from classifier import InvoiceClassifier
from extractor import TextExtractor
from storage import Storage
from utils import is_allowed_extension

logger = logging.getLogger(__name__)

_ALLOWED_EXTENSIONS: set[str] = {".pdf", ".png", ".jpg", ".jpeg", ".docx"}
_MAX_ATTACHMENT_BYTES: int = 20 * 1024 * 1024  # 20 MB
_MAX_MESSAGE_BYTES: int = 60 * 1024 * 1024     # pre-screen: skip emails > 60 MB
_PROGRESS_INTERVAL: int = 50                    # log progress every N emails


class IMAPClient:
    """
    Manages an IMAP4_SSL session for a single :class:`~accounts.AccountConfig`.

    Usage::

        with IMAPClient(account) as client:
            client.process_emails(year=2024, storage=storage)
    """

    def __init__(self, account: AccountConfig) -> None:
        self._host: str = account.host
        self._port: int = account.port
        self._user: str = account.user
        self._password: str = account.password
        self._folder: str = account.folder
        self._conn: Optional[imaplib.IMAP4_SSL] = None
        self._extractor = TextExtractor()
        self._classifier = InvoiceClassifier()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "IMAPClient":
        self._connect()
        return self

    def __exit__(self, *_args: object) -> None:
        self._disconnect()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        logger.info(f"Connecting to {self._host}:{self._port} as {self._user}")
        self._conn = imaplib.IMAP4_SSL(self._host, self._port)
        self._conn.login(self._user, self._password)

        # Quote folder names that contain spaces
        folder = (
            f'"{self._folder}"'
            if " " in self._folder
            else self._folder
        )
        status, msgs = self._conn.select(folder, readonly=True)
        if status != "OK":
            raise RuntimeError(
                f"Could not select IMAP folder '{self._folder}': {msgs}"
            )
        count = msgs[0].decode() if msgs and msgs[0] else "?"
        logger.info(f"Selected folder '{self._folder}' ({count} messages total)")

    def _disconnect(self) -> None:
        if self._conn:
            try:
                self._conn.close()
                self._conn.logout()
                logger.debug("IMAP connection closed")
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Error during disconnect (non-fatal): {exc}")
            finally:
                self._conn = None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _search_uids(self, year: int) -> list[bytes]:
        """Return all UIDs for emails sent in the given calendar year."""
        since = f"01-Jan-{year}"
        before = f"01-Jan-{year + 1}"
        criteria = f"SINCE {since} BEFORE {before}"
        logger.debug(f"IMAP UID SEARCH: {criteria}")

        assert self._conn is not None
        typ, data = self._conn.uid("search", None, criteria)
        if typ != "OK":
            raise RuntimeError(f"UID SEARCH failed — server returned: {typ}")

        if not data or not data[0]:
            return []
        return data[0].split()

    # ------------------------------------------------------------------
    # Main processing loop
    # ------------------------------------------------------------------

    def process_emails(self, year: int, storage: Storage) -> None:
        """
        Iterate every email UID in *year*, skip already-processed ones,
        and coordinate attachment extraction and classification.
        """
        uids = self._search_uids(year)
        total = len(uids)
        logger.info(f"Found {total} email(s) in {year} to inspect")

        for idx, uid in enumerate(uids, 1):
            uid_str = uid.decode()

            if storage.is_processed(uid_str):
                logger.debug(f"[{idx}/{total}] UID {uid_str} already processed — skipping")
                continue

            try:
                self._process_single_email(uid=uid, uid_str=uid_str, storage=storage)
            except Exception as exc:
                logger.error(
                    f"[{idx}/{total}] Unhandled error for UID {uid_str}: {exc}",
                    exc_info=True,
                )
                storage.increment_errors()
            finally:
                # Always mark as processed so we never re-fetch this UID
                storage.mark_processed(uid_str)

            if idx % _PROGRESS_INTERVAL == 0:
                logger.info(
                    f"Progress {idx}/{total} — "
                    f"invoices: {storage.invoice_count} | "
                    f"errors: {storage.error_count}"
                )

    # ------------------------------------------------------------------
    # Single-email handling
    # ------------------------------------------------------------------

    def _process_single_email(
        self, uid: bytes, uid_str: str, storage: Storage
    ) -> None:
        assert self._conn is not None

        # 1. Pre-screen by message size to avoid downloading huge messages
        size = self._fetch_size(uid)
        if size is not None and size > _MAX_MESSAGE_BYTES:
            logger.warning(
                f"UID {uid_str}: message is {size // 1024 // 1024} MB "
                f"(limit {_MAX_MESSAGE_BYTES // 1024 // 1024} MB) — skipping"
            )
            return

        # 2. Fetch full message
        typ, data = self._conn.uid("fetch", uid, "(RFC822)")
        if typ != "OK" or not data or data[0] is None:
            logger.warning(f"UID {uid_str}: fetch failed — {typ}")
            return

        raw = data[0][1]
        if not isinstance(raw, bytes):
            logger.warning(f"UID {uid_str}: unexpected fetch payload type")
            return

        msg = email.message_from_bytes(raw)

        subject = self._decode_header_value(str(msg.get("Subject", "(no subject)")))
        email_date = str(msg.get("Date", ""))

        # 3. Walk MIME parts, yield qualifying attachments
        found_attachments = False
        for attach_filename, attach_data in self._iter_attachments(msg):
            found_attachments = True
            storage.increment_attachments()
            logger.info(
                f"UID {uid_str} | attachment '{attach_filename}' "
                f"({len(attach_data) // 1024} KB) | subject: {subject[:60]!r}"
            )
            try:
                self._handle_attachment(
                    filename=attach_filename,
                    data=attach_data,
                    email_subject=subject,
                    email_date=email_date,
                    storage=storage,
                )
            except Exception as exc:
                logger.error(
                    f"UID {uid_str}: error handling '{attach_filename}': {exc}",
                    exc_info=True,
                )
                storage.increment_errors()

        if not found_attachments:
            logger.debug(f"UID {uid_str}: no qualifying attachments")

        storage.increment_processed()

    # ------------------------------------------------------------------
    # Attachment iteration
    # ------------------------------------------------------------------

    def _iter_attachments(
        self, msg: Message
    ) -> Generator[tuple[str, bytes], None, None]:
        """
        Yield ``(filename, raw_bytes)`` for every attachment that:
          - has an extractable filename
          - uses an allowed extension
          - does not exceed the size limit
        """
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue

            filename = self._get_part_filename(part)
            if not filename:
                continue

            if not is_allowed_extension(filename, _ALLOWED_EXTENSIONS):
                logger.debug(f"Extension not allowed: {filename!r}")
                continue

            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes) or not payload:
                logger.debug(f"Empty or non-bytes payload for '{filename}'")
                continue

            if len(payload) > _MAX_ATTACHMENT_BYTES:
                logger.warning(
                    f"Attachment '{filename}' is {len(payload) // 1024 // 1024} MB "
                    f"— exceeds 20 MB limit, skipping"
                )
                continue

            yield filename, payload

    @staticmethod
    def _get_part_filename(part: Message) -> Optional[str]:
        """
        Extract the decoded filename from a MIME part.

        Checks both Content-Disposition and Content-Type parameters.
        """
        raw_name = part.get_filename()
        if not raw_name:
            # Some senders put the name in Content-Type: type/subtype; name="..."
            for key, val in (part.get_params() or []):
                if key.lower() == "name":
                    raw_name = val
                    break

        if not raw_name:
            return None

        return IMAPClient._decode_header_value(raw_name).strip() or None

    # ------------------------------------------------------------------
    # Attachment processing
    # ------------------------------------------------------------------

    def _handle_attachment(
        self,
        filename: str,
        data: bytes,
        email_subject: str,
        email_date: str,
        storage: Storage,
    ) -> None:
        """Extract text → classify → save if invoice."""
        text = self._extractor.extract(filename=filename, data=data)
        if not text:
            logger.warning(f"No text extracted from '{filename}' — cannot classify")
            return

        result = self._classifier.classify(text=text)
        if result is None:
            logger.warning(f"Classification returned None for '{filename}' — skipping")
            return

        if not result.is_invoice:
            logger.debug(f"'{filename}' → not an invoice")
            return

        logger.info(
            f"Invoice confirmed: vendor={result.vendor!r}  "
            f"number={result.invoice_number!r}  "
            f"date={result.date}  "
            f"amount={result.total_amount} {result.currency}"
        )

        storage.save_invoice(
            filename=filename,
            data=data,
            classification=result,
            email_subject=email_subject,
            email_date=email_date,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fetch_size(self, uid: bytes) -> Optional[int]:
        """Fetch only the RFC822.SIZE of an email (cheap, no body download)."""
        assert self._conn is not None
        typ, data = self._conn.uid("fetch", uid, "(RFC822.SIZE)")
        if typ != "OK" or not data or not data[0]:
            return None
        try:
            # Response: b'<uid> (RFC822.SIZE <n>)'
            token = data[0].decode()
            return int(token.split("RFC822.SIZE")[1].strip().rstrip(")").split()[0])
        except (IndexError, ValueError):
            return None

    @staticmethod
    def _decode_header_value(raw: str) -> str:
        """Decode an RFC 2047-encoded header value to a plain string."""
        try:
            from email.header import decode_header, make_header

            return str(make_header(decode_header(raw)))
        except Exception:
            return raw
