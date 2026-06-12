"""PII redaction tests.

Detection is delegated to Presidio + spaCy (the optional [pii] extra), so these
tests skip when the dependency or spaCy model isn't installed. ``build_redactor(enabled=False)``
is checked unconditionally.
"""

from __future__ import annotations

import pytest

from veil.pii import (
    ADDRESS_TAG,
    BANK_TAG,
    EMAIL_TAG,
    PHONE_TAG,
    SSN_TAG,
    PiiSetupError,
    build_redactor,
)


def test_build_redactor_disabled_returns_none():
    # Works without the extra: disabled means no engine is constructed at all.
    assert build_redactor(enabled=False) is None


def test_tags_are_distinct():
    assert len({EMAIL_TAG, PHONE_TAG, SSN_TAG, BANK_TAG, ADDRESS_TAG}) == 5


# --- everything below needs the [pii] extra + spaCy model ------------------

pytest.importorskip("presidio_analyzer", reason="requires the [pii] extra")


def _redactor(**kw):
    try:
        return build_redactor(enabled=True, **kw)
    except PiiSetupError as exc:  # presidio present but model missing
        pytest.skip(str(exc))


@pytest.fixture(scope="module")
def R():
    return _redactor()


def test_names_are_not_redacted(R):
    # Names are deliberately left in — they over-redact third parties and spaCy
    # mislabels uncommon names. User discretion covers names.
    out = R.scrub_text("Reply to Advait about our contractor Julia Smith.")
    assert "Advait" in out and "Julia Smith" in out


def test_free_form_location_not_redacted(R):
    # Cities/countries are not redacted (only deterministic street lines are).
    out = R.scrub_text("I live in San Francisco")
    assert "San Francisco" in out


def test_phone_redacted(R):
    out = R.scrub_text("call me at +1 (415) 555-0142 tomorrow")
    assert "555-0142" not in out and PHONE_TAG in out


def test_street_address_redacted(R):
    # Street lines via the custom deterministic recognizer (no NER).
    out = R.scrub_text("ship it to 25 Park Lane South, Jersey City")
    assert "25 Park Lane South" not in out and ADDRESS_TAG in out


def test_email_redacted(R):
    out = R.scrub_text("ping me at jane.doe+x@example.co.uk please")
    assert "jane.doe" not in out and EMAIL_TAG in out


def test_ssn_redacted(R):
    # A plausible SSN — Presidio deliberately rejects textbook fakes like
    # 123-45-6789 / 078-05-1120 via its invalidate_result blacklist.
    out = R.scrub_text("my SSN is 457-55-5462")
    assert "457-55-5462" not in out and SSN_TAG in out


def test_credit_card_redacted(R):
    # Luhn-valid canonical test number.
    out = R.scrub_text("card 4111 1111 1111 1111 on file")
    assert "4111" not in out and BANK_TAG in out


def test_iban_redacted(R):
    out = R.scrub_text("send to GB82 WEST 1234 5698 7654 32 today")
    assert "WEST" not in out and BANK_TAG in out


def test_dates_are_not_redacted(R):
    # Dates are deliberately left intact.
    out = R.scrub_text("DOB: 04/12/1990. The invoice is dated 06/01/2026.")
    assert "04/12/1990" in out and "06/01/2026" in out


def test_scrub_request_string_content(R):
    body = {
        "model": "gpt-4.1",
        "messages": [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "email me at a@b.com"},
        ],
    }
    out = R.scrub_request(body)
    assert EMAIL_TAG in out["messages"][1]["content"]
    # Original body is not mutated.
    assert body["messages"][1]["content"] == "email me at a@b.com"


def test_scrub_request_multimodal_parts(R):
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "reach me at a@b.com"},
                    {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                ],
            }
        ]
    }
    out = R.scrub_request(body)
    parts = out["messages"][0]["content"]
    assert EMAIL_TAG in parts[0]["text"]
    assert parts[1] == {"type": "image_url", "image_url": {"url": "http://x/y.png"}}
