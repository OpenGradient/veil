"""Scratch helper: interactively try the PII redactor on your own text.

    uv run python scripts/pii_repl.py            # interactive: type a line, see it scrubbed
    echo "email me at a@b.com" | uv run python scripts/pii_repl.py   # or pipe input

Local-only — no login, no TEE, no network. Just the same Redactor the proxy uses.
Needs the extra:  make install-pii   (or: uv sync --extra pii)
"""

from __future__ import annotations

import sys

from veil.pii import PiiSetupError, build_redactor


def main() -> None:
    try:
        redactor = build_redactor(enabled=True)
    except PiiSetupError as exc:
        sys.exit(f"PII redaction unavailable: {exc}")
    assert redactor is not None

    if not sys.stdin.isatty():  # piped input: scrub each line and exit
        for line in sys.stdin:
            print(redactor.scrub_text(line.rstrip("\n")))
        return

    print("PII redactor — type text and press Enter (Ctrl-D / Ctrl-C to quit).\n")
    try:
        while True:
            text = input("> ")
            print(redactor.scrub_text(text))
    except (EOFError, KeyboardInterrupt):
        print()


if __name__ == "__main__":
    main()
