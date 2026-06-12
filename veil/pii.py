"""Local, opt-in PII redaction applied *before* a prompt leaves this process.

Veil's privacy guarantee is *unlinkability*: Oblivious HTTP splits who-you-are
from what-you-ask, so the relay sees your identity but only ciphertext, and the
model provider sees the prompt but believes it came from the enclave — not you.
That holds **only if the prompt content doesn't re-identify you**. A request that
says "I'm Adam Balogh, adam@example.com, 25 Park Lane" hands your identity to the
provider through the content and undoes the cryptographic split. So this module's
job is to de-identify the *requester*: strip the things that point back to a
specific person before the prompt is HPKE-sealed to the TEE.

Detection is delegated to **Microsoft Presidio** so the recognizers are
community-maintained rather than handrolled. Presidio detects structured values
(email, phone, SSN, cards, IBANs, bank numbers) with its own regex/checksum
recognizers, and uses a spaCy NER model for the free-form identity linkers
(names, locations). This requires the optional extra plus a spaCy model:

    pip install 'opengradient-veil[pii]'
    python -m spacy download en_core_web_lg

What gets redacted (mapped to the tags below):

* names of people, and addresses / locations (spaCy NER + a street-line recognizer)
* email and phone numbers
* US SSN, credit cards (Luhn), IBANs (mod-97), US bank/routing numbers

Dates are deliberately *not* redacted — they're too entangled with legitimate
prompt content to scrub without mangling it.

This is risk-reduction, not a guarantee. It's also *blunt*: it can't tell your
own name from a name you're asking about, so it over-redacts; and even perfect
PII removal leaves residual re-identification (writing style, niche topics).
Redaction is irreversible — there is deliberately no de-anonymization step, so
the TEE's signed ``output_hash`` covers exactly what it ran.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Redaction tags. Kept human-readable so a redacted prompt still reads sensibly to
# the model (e.g. "wire it to [REDACTED_BANK_NUMBER]").
NAME_TAG = "[REDACTED_NAME]"
EMAIL_TAG = "[REDACTED_EMAIL]"
PHONE_TAG = "[REDACTED_PHONE]"
SSN_TAG = "[REDACTED_SSN]"
BANK_TAG = "[REDACTED_BANK_NUMBER]"
ADDRESS_TAG = "[REDACTED_ADDRESS]"

# spaCy model backing the NER (name/location) detection. The large English model
# is used rather than the small one: names are the core identity linker here, and
# large meaningfully improves PERSON recall across diverse names. It's a ~560 MB
# CNN model (not a transformer) and runs on CPU. Override is intentionally not
# exposed — keep the install predictable.
_SPACY_MODEL = "en_core_web_lg"

# Presidio entities we redact, each mapped to a tag. PERSON and LOCATION come from
# the spaCy NER model; the rest are Presidio's regex/checksum recognizers.
_ENTITY_TAGS = {
    "PERSON": NAME_TAG,
    "EMAIL_ADDRESS": EMAIL_TAG,
    "PHONE_NUMBER": PHONE_TAG,
    "US_SSN": SSN_TAG,
    "CREDIT_CARD": BANK_TAG,
    "IBAN_CODE": BANK_TAG,
    "US_BANK_NUMBER": BANK_TAG,
    "LOCATION": ADDRESS_TAG,
    # Custom recognizer registered below; Presidio ships nothing for street lines.
    "STREET_ADDRESS": ADDRESS_TAG,
}

# Street-address line: a house number, a few name words, then a street-type
# suffix, optionally a direction. Presidio/spaCy NER catch named places (cities,
# states) but not free-form street lines, so this closes the most common gap
# without a transformer. Compiled by Presidio with IGNORECASE.
_STREET_ADDRESS_REGEX = (
    r"\b\d{1,6}\s+(?:[A-Za-z0-9.\-']+\s+){0,4}"
    r"(?:street|st|avenue|ave|lane|ln|road|rd|boulevard|blvd|drive|dr|court|ct"
    r"|place|pl|way|terrace|ter|circle|cir|highway|hwy|parkway|pkwy|square|sq"
    r"|trail|trl|route|rte|crossing|xing|loop|row|alley|plaza|commons)\b\.?"
    r"(?:\s+(?:northeast|northwest|southeast|southwest|north|south|east|west"
    r"|ne|nw|se|sw|n|s|e|w)\b)?"
)


class PiiSetupError(Exception):
    """PII scrubbing was requested but Presidio / the spaCy model isn't installed."""


def _build_engine():  # noqa: ANN202 — Presidio types are untyped
    """Construct the Presidio analyzer/anonymizer and the per-entity plan.

    Raises :class:`PiiSetupError` with an actionable message if the optional
    dependency or its spaCy model is missing.
    """
    try:
        from presidio_analyzer import (  # type: ignore[import-not-found]
            AnalyzerEngine,
            Pattern,
            PatternRecognizer,
        )
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
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="STREET_ADDRESS",
            patterns=[Pattern(name="street_address", regex=_STREET_ADDRESS_REGEX, score=0.6)],
        )
    )
    anonymizer = AnonymizerEngine()

    operators = {
        entity: OperatorConfig("replace", {"new_value": tag})
        for entity, tag in _ENTITY_TAGS.items()
    }
    entities = list(_ENTITY_TAGS)

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
