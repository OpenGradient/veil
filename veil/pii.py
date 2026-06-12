"""Local, opt-in PII redaction applied *before* a prompt leaves this process.

Veil already keeps prompts private end-to-end (Oblivious HTTP splits identity
from content; the enclave is attested and reproducible). This module is
*defense-in-depth* on top of that: when enabled, high-impact PII is irreversibly
replaced with ``[REDACTED_*]`` tags on the agent's request before it is HPKE-
sealed to the TEE, so the raw values never leave the machine at all — useful for
compliance, data-residency, and keeping PII out of any model-side logging.

Detection is delegated to **Microsoft Presidio** rather than handrolled patterns,
so the recognizers are community-maintained, versioned, and carry confidence
scores + context-word boosting. Note that Presidio detects the *structured*
types (email, SSN, cards, IBANs, bank numbers) with its own regex/checksum
recognizers — spaCy's statistical model is only used for free-form
*addresses/locations*. Because of that, this feature requires the optional extra:

    pip install 'opengradient-veil[pii]'
    python -m spacy download en_core_web_sm

What gets redacted (mapped to the tags below):

* email, US SSN, credit cards (Luhn), IBANs (mod-97), US bank/routing numbers
* addresses / locations (spaCy NER)

Dates are deliberately *not* redacted — they're too entangled with legitimate
prompt content to scrub without mangling it.

This is risk-reduction, not a guarantee: NER misses a fraction of addresses every
run, and bare (unlabelled) account numbers can slip through. Redaction is
irreversible — there is deliberately no de-anonymization step, so the TEE's signed
``output_hash`` covers exactly what it ran and nothing is restored locally.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Redaction tags. Kept human-readable so a redacted prompt still reads sensibly to
# the model (e.g. "wire it to [REDACTED_BANK_NUMBER]").
EMAIL_TAG = "[REDACTED_EMAIL]"
SSN_TAG = "[REDACTED_SSN]"
BANK_TAG = "[REDACTED_BANK_NUMBER]"
ADDRESS_TAG = "[REDACTED_ADDRESS]"

# spaCy model backing the NER (address) detection. The small English model is the
# lightweight choice; recall climbs with the medium/large models at a large size
# cost. Override is intentionally not exposed — keep the install predictable.
_SPACY_MODEL = "en_core_web_sm"

# Presidio's built-in structured recognizers we rely on, each mapped to a tag.
_STRUCTURED_OPERATORS = {
    "EMAIL_ADDRESS": EMAIL_TAG,
    "US_SSN": SSN_TAG,
    "CREDIT_CARD": BANK_TAG,
    "IBAN_CODE": BANK_TAG,
    "US_BANK_NUMBER": BANK_TAG,
    "LOCATION": ADDRESS_TAG,
}


class PiiSetupError(Exception):
    """PII scrubbing was requested but Presidio / the spaCy model isn't installed."""


def _build_engine():  # noqa: ANN202 — Presidio types are untyped
    """Construct the Presidio analyzer/anonymizer and the per-entity plan.

    Raises :class:`PiiSetupError` with an actionable message if the optional
    dependency or its spaCy model is missing.
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
    except ImportError as exc:
        raise PiiSetupError(
            "PII scrubbing needs the optional extra. Install it with: "
            "pip install 'opengradient-veil[pii]' and install the spaCy model "
            f"'{_SPACY_MODEL}' (e.g. `python -m spacy download {_SPACY_MODEL}` or the model wheel)."
        ) from exc

    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": _SPACY_MODEL}],
        }
    )
    try:
        nlp_engine = provider.create_engine()
    except Exception as exc:  # noqa: BLE001 — spaCy model not downloaded yet
        raise PiiSetupError(
            f"the spaCy model '{_SPACY_MODEL}' is not installed; run: "
            f"python -m spacy download {_SPACY_MODEL}"
        ) from exc

    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
    anonymizer = AnonymizerEngine()

    operators = {
        entity: OperatorConfig("replace", {"new_value": tag})
        for entity, tag in _STRUCTURED_OPERATORS.items()
    }
    entities = list(_STRUCTURED_OPERATORS)

    def analyze(text: str):  # noqa: ANN202
        return analyzer.analyze(text=text, entities=entities, language="en")

    return analyze, anonymizer, operators


class Redactor:
    """Presidio-backed PII redactor. Built eagerly so misconfiguration fails fast."""

    def __init__(self):
        self._analyze, self._anonymizer, self._operators = _build_engine()

    def scrub_text(self, text: str) -> str:
        if not text:
            return text
        results = self._analyze(text)
        if not results:
            return text
        return self._anonymizer.anonymize(
            text=text, analyzer_results=results, operators=self._operators
        ).text

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
                    if isinstance(p, dict)
                    and p.get("type") == "text"
                    and isinstance(p.get("text"), str)
                    else p
                    for p in content
                ]
                msg = {**msg, "content": parts}
            scrubbed_messages.append(msg)

        return {**body, "messages": scrubbed_messages}


def build_redactor(*, enabled: bool) -> Redactor | None:
    """Construct a :class:`Redactor` when PII scrubbing is enabled, else ``None``.

    Raises :class:`PiiSetupError` if enabled but the ``[pii]`` extra / spaCy model
    isn't installed, so the operator gets a clear message at startup rather than a
    silent no-op.
    """
    if not enabled:
        return None
    return Redactor()
