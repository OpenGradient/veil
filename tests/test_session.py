"""Session persistence + auto-reload.

The long-running background server loads the session once at startup. These tests
pin down the behavior that lets a fresh ``og-veil login`` reach that running
server: the in-memory session re-reads ``session.json`` when another process
rewrites it, instead of forever using its stale (expired/revoked) token.
"""

from __future__ import annotations

import json
import time

import pytest

from veil.config import session_path
from veil.session import AuthError, Session


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OG_VEIL_HOME", str(tmp_path))
    return tmp_path


def _bundle(access_token: str, *, expires_at: float) -> dict:
    return {
        "type": "opengradient-cli-auth",
        "access_token": access_token,
        "refresh_token": "refresh-xyz",
        "expires_at": expires_at,
        "user": {"email": "me@example.com"},
        "config": {
            "supabase_url": "https://supabase.example",
            "supabase_anon_key": "anon",
            "chat_api_base_url": "https://chat.example",
        },
    }


def _write_session(data: dict) -> None:
    session_path().write_text(json.dumps(data))


def test_fresh_login_on_disk_is_picked_up_without_restart(home):
    # The server loaded a session whose token is already expired and whose refresh
    # token would be rejected upstream — the exact state behind the reported 401.
    _write_session(_bundle("stale-token", expires_at=time.time() - 3600))
    session = Session.load()

    # The user runs `og-veil login`, which writes a brand-new, valid session to
    # disk from another process. The running server must adopt it.
    _write_session(_bundle("fresh-token", expires_at=time.time() + 3600))

    # No refresh network call should be needed: the reloaded token is still valid.
    assert session.access_token() == "fresh-token"
    assert session.user_email == "me@example.com"


def test_unchanged_session_is_not_reloaded_and_no_refresh(home):
    _write_session(_bundle("good-token", expires_at=time.time() + 3600))
    session = Session.load()

    # Make any refresh attempt explode so the test fails loudly if one happens.
    def _boom():
        raise AssertionError("should not refresh a still-valid token")

    session._refresh = _boom  # type: ignore[assignment]
    assert session.access_token() == "good-token"


def test_refresh_writes_back_without_triggering_self_reload(home):
    # A token that's expired in memory but has no fresh login on disk must still go
    # through refresh; the write-back must not look like an external change.
    _write_session(_bundle("expired-token", expires_at=time.time() - 3600))
    session = Session.load()

    calls = {"n": 0}

    def _fake_refresh():
        calls["n"] += 1
        session._data["access_token"] = "refreshed-token"
        session._data["expires_at"] = time.time() + 3600
        session.save()

    session._refresh = _fake_refresh  # type: ignore[assignment]
    assert session.access_token() == "refreshed-token"
    # A second call sees its own write (unchanged mtime), so it neither reloads nor
    # refreshes again.
    assert session.access_token() == "refreshed-token"
    assert calls["n"] == 1


def test_load_without_session_file_raises(home):
    with pytest.raises(AuthError):
        Session.load()
