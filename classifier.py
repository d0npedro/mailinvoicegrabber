"""
AI-based invoice classification using the OpenAI API (gpt-4o-mini).

Classification flow:
  1. Send extracted document text with a structured system prompt.
  2. Receive JSON response specifying invoice metadata.
  3. Retry once on invalid/missing JSON; skip file after two failures.
"""
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a financial document classifier.
The document may be in German or English.

Detect whether this document is an invoice.

Recognize German invoice keywords such as:
Rechnung, Rechnungsnummer, Rechnungsdatum,
Betrag, Gesamtbetrag, MwSt, Umsatzsteuer,
Netto, Brutto, Zahlungsziel, IBAN, Steuernummer.

Respond ONLY in valid JSON with this exact structure:

{
  "is_invoice": true,
  "vendor": "Company Name GmbH",
  "invoice_number": "RE-2024-001",
  "date": "2024-03-15",
  "total_amount": "119.00",
  "currency": "EUR"
}

Rules:
- "is_invoice": boolean — true only when you are confident this is an invoice.
- "vendor": the issuing company/person name, or "unknown" if not found.
- "invoice_number": the invoice/Rechnungsnummer, or "unknown".
- "date": ISO-8601 date (YYYY-MM-DD) of the invoice, or "unknown".
- "total_amount": gross total as a plain decimal string (no currency symbol), or "0".
- "currency": ISO 4217 code (EUR, USD, GBP …), or "EUR" as default.
- If uncertain about invoice status, set is_invoice to false.
"""

_MODEL = "gpt-4o-mini"
_MAX_TOKENS = 350
_TEMPERATURE = 0.0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ClassificationResult:
    is_invoice: bool
    vendor: str
    invoice_number: str
    date: str
    total_amount: str
    currency: str

    @classmethod
    def not_invoice(cls) -> "ClassificationResult":
        return cls(
            is_invoice=False,
            vendor="unknown",
            invoice_number="unknown",
            date="unknown",
            total_amount="0",
            currency="EUR",
        )


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class InvoiceClassifier:
    """Classifies document text as invoice/non-invoice using OpenAI."""

    def __init__(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set in the environment")

        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, timeout=45.0)

    def classify(self, text: str) -> Optional[ClassificationResult]:
        """
        Classify *text* as invoice or not.

        Retries once on bad JSON.  Returns ``None`` if both attempts fail
        or an unrecoverable API error occurs.
        """
        for attempt in range(1, 3):
            try:
                result = self._call_api(text)
                if result is not None:
                    return result
                logger.debug(f"Classification attempt {attempt}/2 returned None — retrying")
            except Exception as exc:  # noqa: BLE001 — handled per type below
                if not self._handle_api_error(exc, attempt):
                    return None  # unrecoverable

        logger.error("Classification failed after 2 attempts — skipping attachment")
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _call_api(self, text: str) -> Optional[ClassificationResult]:
        """Send request to OpenAI and parse the JSON response."""
        response = self._client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Document text:\n\n{text}"},
            ],
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            response_format={"type": "json_object"},  # openai v2 TypedDict, dict accepted
        )

        content = response.choices[0].message.content
        if not content:
            logger.warning("OpenAI returned an empty response")
            return None

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning(f"JSON parse error: {exc} | raw: {content[:300]}")
            return None

        return self._build_result(data)

    def _build_result(self, data: dict) -> Optional[ClassificationResult]:
        """Validate and coerce the parsed JSON dict into a ClassificationResult."""
        try:
            return ClassificationResult(
                is_invoice=bool(data.get("is_invoice", False)),
                vendor=str(data.get("vendor") or "unknown").strip() or "unknown",
                invoice_number=str(data.get("invoice_number") or "unknown").strip()
                or "unknown",
                date=str(data.get("date") or "unknown").strip() or "unknown",
                total_amount=str(data.get("total_amount") or "0").strip() or "0",
                currency=str(data.get("currency") or "EUR").strip().upper() or "EUR",
            )
        except Exception as exc:
            logger.warning(f"Failed to build ClassificationResult: {exc}")
            return None

    def _handle_api_error(self, exc: Exception, attempt: int) -> bool:
        """
        Log the error and decide whether to retry.

        Returns True if the caller should retry, False if it should abort.
        """
        from openai import APIConnectionError, AuthenticationError, RateLimitError

        if isinstance(exc, AuthenticationError):
            logger.error("OpenAI authentication failed — check OPENAI_API_KEY")
            return False  # no point retrying
        if isinstance(exc, RateLimitError):
            logger.error("OpenAI rate limit exceeded — skipping this attachment")
            return False
        if isinstance(exc, APIConnectionError):
            logger.warning(f"OpenAI connection error (attempt {attempt}/2): {exc}")
            return attempt < 2  # retry once
        # Generic API error
        logger.warning(f"OpenAI API error (attempt {attempt}/2): {exc}")
        return attempt < 2
