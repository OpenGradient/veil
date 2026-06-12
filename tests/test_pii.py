"""PII redaction tests — the always-on regex tier and request rewriting.

The optional NER tier (Presidio + spaCy) is exercised only when the [pii] extra
is installed, so addresses are not asserted here; these cover the zero-dependency
behavior that ships in the base install.
"""

from __future__ import annotations

from veil.pii import (
    ADDRESS_TAG,
    BANK_TAG,
    DOB_TAG,
    EMAIL_TAG,
    SSN_TAG,
    Redactor,
    build_redactor,
)

# A Redactor with no NER engine loaded — pure regex tier. (If Presidio happens to
# be installed in the dev env it would only *add* address coverage, never change
# these structured-PII assertions.)
R = Redactor()


def test_email_redacted():
    assert R.scrub_text("ping me at jane.doe+x@example.co.uk please") == (
        f"ping me at {EMAIL_TAG} please"
    )


def test_dashed_ssn_redacted():
    assert R.scrub_text("my ssn is 123-45-6789") == f"my ssn is {SSN_TAG}"


def test_bare_ssn_only_with_label():
    # Bare 9-digit runs are left alone (too ambiguous)…
    assert "987654321" in R.scrub_text("order 987654321 shipped")
    # …but a labelled one is redacted, keeping the label.
    out = R.scrub_text("SSN: 987654321")
    assert "987654321" not in out and SSN_TAG in out and out.lower().startswith("ssn")


def test_valid_credit_card_redacted_invalid_kept():
    # 4111 1111 1111 1111 is the canonical Luhn-valid test number.
    assert R.scrub_text("card 4111 1111 1111 1111") == f"card {BANK_TAG}"
    # One digit off → fails Luhn → not redacted.
    assert "4111 1111 1111 1112" in R.scrub_text("card 4111 1111 1111 1112")


def test_valid_iban_redacted():
    out = R.scrub_text("send to GB82 WEST 1234 5698 7654 32 today")
    assert BANK_TAG in out and "WEST" not in out


def test_invalid_iban_kept():
    assert "GB00WEST12345698765432" in R.scrub_text("ref GB00WEST12345698765432")


def test_labelled_account_number_redacted():
    out = R.scrub_text("account number: 0012345678")
    assert "0012345678" not in out and BANK_TAG in out


def test_dob_context_redacts_only_cued_dates():
    out = R.scrub_text("DOB: 04/12/1990, meeting on 06/01/2026")
    assert DOB_TAG in out
    assert "04/12/1990" not in out
    # A non-birth date is untouched without the all-dates toggle.
    assert "06/01/2026" in out


def test_scrub_request_string_content():
    body = {
        "model": "gpt-4.1",
        "messages": [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "email me at a@b.com"},
        ],
    }
    out = R.scrub_request(body)
    assert out["messages"][1]["content"] == f"email me at {EMAIL_TAG}"
    # Original body is not mutated.
    assert body["messages"][1]["content"] == "email me at a@b.com"


def test_scrub_request_multimodal_parts():
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
    assert parts[0]["text"] == f"reach me at {EMAIL_TAG}"
    assert parts[1] == {"type": "image_url", "image_url": {"url": "http://x/y.png"}}


def test_build_redactor_disabled_returns_none():
    assert build_redactor(enabled=False) is None
    assert build_redactor(enabled=True) is not None


def test_address_tag_is_distinct():
    # Sanity: tags are unique strings so downstream tooling can grep them.
    assert len({EMAIL_TAG, SSN_TAG, BANK_TAG, DOB_TAG, ADDRESS_TAG}) == 5
