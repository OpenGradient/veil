"""Local, opt-in PII redaction applied *before* a prompt leaves this process.

Veil already keeps prompts private end-to-end (Oblivious HTTP splits identity
from content; the enclave is attested and reproducible). This module is
*defense-in-depth* on top of that: when enabled, high-impact PII is irreversibly
replaced with ``[REDACTED_*]`` tags on the agent's request before it is HPKE-
sealed to the TEE, so the raw values never leave the machine at all — useful for
compliance, data-residency, and keeping PII out of any model-side logging.

Two tiers, layered:

* **Regex (always on when enabled, zero extra deps)** — structured PII that has a
  recognizable shape: email, US SSN, and bank numbers (credit cards validated by
  Luhn, IBANs by mod-97 checksum, plus routing/account numbers when labelled).
  Dates of birth are caught by context ("DOB:", "born on …").
* **NER (optional, ``pip install 'opengradient-veil[pii]'``)** — Microsoft
  Presidio + a spaCy model adds *addresses/locations*, which are free-form prose
  that regex fundamentally cannot see. Optionally redacts *all* dates too.

This is risk-reduction, not a guarantee: NER misses a fraction of addresses every
run, and bare (unlabelled) account numbers can slip through. Redaction is
irreversible — there is deliberately no de-anonymization step, so the TEE's
signed ``output_hash`` covers exactly what it ran and nothing is restored locally.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Redaction tags. Kept human-readable so a redacted prompt still reads sensibly to
# the model (e.g. "wire it to [REDACTED_BANK_NUMBER]").
EMAIL_TAG = "[REDACTED_EMAIL]"
SSN_TAG = "[REDACTED_SSN]"
BANK_TAG = "[REDACTED_BANK_NUMBER]"
DOB_TAG = "[REDACTED_DOB]"
ADDRESS_TAG = "[REDACTED_ADDRESS]"


class PiiSetupError(Exception):
    """The NER engine was requested but its model/runtime isn't installed."""


# --- regex tier ------------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Dashed/spaced SSN, e.g. 123-45-6789 or 123 45 6789. Bare 9-digit runs are too
# ambiguous (order IDs etc.) to redact unconditionally — those are handled by the
# context detector below.
_SSN_RE = re.compile(r"\b\d{3}[-\s]\d{2}[-\s]\d{4}\b")

# Candidate card number: 13–19 digits, optionally split by spaces/hyphens. Only
# redacted if it passes the Luhn check, which kills almost all false positives.
_CC_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

# IBAN shape, allowing the common space-grouped form (GB82 WEST 1234 …);
# validated by mod-97 (spaces stripped) before redaction.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b")

# Labelled SSN: "SSN: 123456789" / "social security number 123-45-6789".
_SSN_CTX_RE = re.compile(
    r"(?i)(\b(?:ssn|social security(?:\s+number)?)\b\D{0,8})((?:\d[ -]?){8,9}\d)"
)

# Labelled bank/routing/account numbers, including bare domestic account numbers
# that have no fixed shape and are only recognizable from their label.
_BANK_CTX_RE = re.compile(
    r"(?i)(\b(?:account|acct|a/c|routing|aba|iban|swift|bic|sort\s?code|bank)\b"
    r"(?:\s*(?:number|no\.?|#))?\s*[:#]?\s*)((?:\d[ -]?){5,17}\d)"
)

# A date in common numeric or "Month DD, YYYY" forms — only redacted when it
# follows a birth-date cue, so ordinary dates in the prompt are left intact.
_DATE = (
    r"(?:\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?"
    r"|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
    r"\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})"
)
_DOB_CTX_RE = re.compile(
    r"(?i)(\b(?:d\.?o\.?b\.?|date\s+of\s+birth|born(?:\s+on)?|birth\s?date|birthday)\b\W{0,6})("
    + _DATE
    + r")"
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _iban_ok(candidate: str) -> bool:
    s = candidate.upper().replace(" ", "")
    rearranged = s[4:] + s[:4]
    # Letters → 10..35; the whole thing mod 97 must equal 1 for a valid IBAN.
    converted = "".join(str(int(c, 36)) if c.isalpha() else c for c in rearranged)
    try:
        return int(converted) % 97 == 1
    except ValueError:
        return False


def _redact_cc(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return BANK_TAG
    return m.group(0)


def _redact_iban(m: re.Match[str]) -> str:
    return BANK_TAG if _iban_ok(m.group(0)) else m.group(0)


def _keep_label(tag: str) -> Callable[[re.Match[str]], str]:
    """Replace only the value group, preserving the leading label (group 1)."""

    def repl(m: re.Match[str]) -> str:
        return m.group(1) + tag

    return repl


# Order matters: validate-and-redact the strongly-shaped values first, then the
# label-anchored ones, so a labelled card number is still caught by the Luhn pass.
_Replacement = str | Callable[[re.Match[str]], str]
_REGEX_PASSES: list[tuple[re.Pattern[str], _Replacement]] = [
    (_EMAIL_RE, EMAIL_TAG),
    (_IBAN_RE, _redact_iban),
    (_CC_RE, _redact_cc),
    (_SSN_RE, SSN_TAG),
    (_SSN_CTX_RE, _keep_label(SSN_TAG)),
    (_BANK_CTX_RE, _keep_label(BANK_TAG)),
    (_DOB_CTX_RE, _keep_label(DOB_TAG)),
]


def _regex_scrub(text: str) -> str:
    for pattern, repl in _REGEX_PASSES:
        text = pattern.sub(repl, text)
    return text


# --- NER tier (optional, Presidio) -----------------------------------------


def _load_ner(redact_all_dates: bool) -> Callable[[str], str] | None:
    """Build a Presidio-backed scrubber for addresses (and optionally all dates).

    Returns ``None`` if the optional ``[pii]`` extra isn't installed. Raises
    :class:`PiiSetupError` if Presidio is present but its spaCy model isn't, so
    the operator gets an actionable message instead of a cryptic stack trace.
    """
    try:
        from presidio_analyzer import AnalyzerEngine  # type: ignore[import-not-found]
        from presidio_analyzer.nlp_engine import (  # type: ignore[import-not-found]
            NlpEngineProvider,
        )
        from presidio_anonymizer import AnonymizerEngine  # type: ignore[import-not-found]
        from presidio_anonymizer.entities import (  # type: ignore[import-not-found]
            OperatorConfig,
        )
    except ImportError:
        return None

    model = "en_core_web_sm"
    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": model}],
        }
    )
    try:
        nlp_engine = provider.create_engine()
    except Exception as exc:  # noqa: BLE001 — spaCy model not downloaded yet
        raise PiiSetupError(
            f"the spaCy model '{model}' is not installed; run: "
            f"python -m spacy download {model}"
        ) from exc

    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
    anonymizer = AnonymizerEngine()

    entities = ["LOCATION"]
    operators = {"LOCATION": OperatorConfig("replace", {"new_value": ADDRESS_TAG})}
    if redact_all_dates:
        entities.append("DATE_TIME")
        operators["DATE_TIME"] = OperatorConfig("replace", {"new_value": DOB_TAG})

    def run(text: str) -> str:
        results = analyzer.analyze(text=text, entities=entities, language="en")
        if not results:
            return text
        return anonymizer.anonymize(text=text, analyzer_results=results, operators=operators).text

    return run


# --- public surface ---------------------------------------------------------


class Redactor:
    """Applies the regex tier (always) and the NER tier (when available)."""

    def __init__(self, *, redact_all_dates: bool = False):
        self._ner: Callable[[str], str] | None = None
        try:
            self._ner = _load_ner(redact_all_dates)
        except PiiSetupError as exc:
            logger.warning(
                "PII: %s — continuing with regex-only redaction (no address coverage)", exc
            )
        if self._ner is None:
            logger.warning(
                "PII: address/NER redaction unavailable — install the extra for it: "
                "pip install 'opengradient-veil[pii]'"
            )

    def scrub_text(self, text: str) -> str:
        if not text:
            return text
        # Regex first so structured values become tags; the NER pass then runs on
        # the partially-redacted text purely for free-form addresses/dates.
        text = _regex_scrub(text)
        if self._ner is not None:
            text = self._ner(text)
        return text

    def scrub_request(self, body: dict) -> dict:
        """Return a copy of an OpenAI chat-completions body with message text redacted.

        Handles both string content and the multimodal ``[{"type": "text", ...}]``
        content-part form; non-text parts (images, audio) are passed through.
        """
        messages = body.get("messages")
        if not isinstance(messages, list):
            return body

        scrubbed_messages = []
        for msg in messages:
            if not isinstance(msg, dict):
                scrubbed_messages.append(msg)
                continue
            content = msg.get("content")
            if isinstance(content, str):
                msg = {**msg, "content": self.scrub_text(content)}
            elif isinstance(content, list):
                parts = [
                    {**p, "text": self.scrub_text(p["text"])}
                    if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
                    else p
                    for p in content
                ]
                msg = {**msg, "content": parts}
            scrubbed_messages.append(msg)

        return {**body, "messages": scrubbed_messages}


def build_redactor(*, enabled: bool, redact_all_dates: bool = False) -> Redactor | None:
    """Construct a :class:`Redactor` when PII scrubbing is enabled, else ``None``."""
    if not enabled:
        return None
    return Redactor(redact_all_dates=redact_all_dates)
