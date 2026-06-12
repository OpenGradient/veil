"""Login + session management against the OpenGradient Chat app.

The local proxy does not hold a wallet or any payment key. Instead it borrows the
user's Chat account: the user authorizes this device through the Chat app's
``/cli-auth`` page (the same flow the web client documents), which hands back a
Supabase session token plus the public network config (chat-api relay URL, TEE
registry RPC + address). The relay settles x402 payment server-side against that
account, so credentials — not coins — gate inference.

This module:
  * runs the loopback callback that receives the CLI-auth bundle,
  * persists it to ``~/.opengradient/local/session.json``,
  * refreshes the Supabase access token when it expires.
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from og_local.config import session_path

BUNDLE_TYPE = "opengradient-cli-auth"


class AuthError(Exception):
    """Login failed or no valid session is available."""


@dataclass
class NetworkConfig:
    """Public config delivered in the CLI-auth bundle."""

    app_env: str
    supabase_url: str
    supabase_anon_key: str
    chat_api_base_url: str
    tee_registry_rpc_url: Optional[str]
    tee_registry_address: Optional[str]
    tee_registry_tee_type: Optional[int]

    @classmethod
    def from_dict(cls, d: dict) -> "NetworkConfig":
        return cls(
            app_env=d.get("app_env", "production"),
            supabase_url=_require(d, "supabase_url"),
            supabase_anon_key=_require(d, "supabase_anon_key"),
            chat_api_base_url=_require(d, "chat_api_base_url"),
            tee_registry_rpc_url=d.get("tee_registry_rpc_url"),
            tee_registry_address=d.get("tee_registry_address"),
            tee_registry_tee_type=d.get("tee_registry_tee_type"),
        )


class Session:
    """A persisted Chat session: tokens + network config, with auto-refresh."""

    def __init__(self, data: dict):
        self._data = data
        self.config = NetworkConfig.from_dict(data.get("config", {}))

    # --- persistence -------------------------------------------------------
    @classmethod
    def load(cls) -> "Session":
        path = session_path()
        if not path.exists():
            raise AuthError("not logged in — run `og-local login` first")
        try:
            return cls(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthError(f"could not read saved session: {exc}") from exc

    def save(self) -> None:
        path = session_path()
        path.write_text(json.dumps(self._data, indent=2))
        try:
            path.chmod(0o600)  # the file holds a live session token
        except OSError:
            pass

    # --- accessors ---------------------------------------------------------
    @property
    def user_email(self) -> Optional[str]:
        return (self._data.get("user") or {}).get("email")

    def auth_headers(self) -> dict:
        """Headers proving the Chat session to the relay (refreshing if needed)."""
        return {"Authorization": f"Bearer {self.access_token()}"}

    def access_token(self) -> str:
        if self._is_expired():
            self._refresh()
        token = self._data.get("access_token")
        if not token:
            raise AuthError("session has no access token — run `og-local login` to sign in again")
        return token

    def _is_expired(self) -> bool:
        expires_at = self._data.get("expires_at")
        if not isinstance(expires_at, (int, float)):
            return False  # no expiry info; assume valid and let the relay 401 if not
        return time.time() >= float(expires_at) - 60  # refresh a minute early

    def _refresh(self) -> None:
        refresh_token = self._data.get("refresh_token")
        if not refresh_token:
            raise AuthError("session expired — run `og-local login` to sign in again")
        url = f"{self.config.supabase_url}/auth/v1/token?grant_type=refresh_token"
        try:
            resp = requests.post(
                url,
                params={"grant_type": "refresh_token"},
                json={"refresh_token": refresh_token},
                headers={
                    "apikey": self.config.supabase_anon_key,
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
        except requests.RequestException as exc:
            raise AuthError(
                f"couldn't reach the auth server to refresh your session: {exc}"
            ) from exc

        # A revoked/expired refresh token (e.g. you signed out in the Chat app)
        # comes back as 400/401 — that needs a fresh interactive login, not a retry.
        if resp.status_code in (400, 401, 403):
            raise AuthError(
                "your OpenGradient Chat session was signed out or expired — "
                "run `og-local login` to sign in again"
            )
        try:
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise AuthError(f"failed to refresh session: {exc}") from exc

        self._data["access_token"] = body.get("access_token")
        self._data["refresh_token"] = body.get("refresh_token", refresh_token)
        if body.get("expires_at") is not None:
            self._data["expires_at"] = body["expires_at"]
        elif body.get("expires_in") is not None:
            self._data["expires_at"] = int(time.time()) + int(body["expires_in"])
        self.save()


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


def login(app_url: str, *, open_browser: bool = True, timeout: float = 300.0) -> Session:
    """Run the browser CLI-auth flow and persist the resulting session.

    Spins up a loopback listener, opens ``<app_url>/cli-auth?redirect_uri=...``,
    and waits for the Chat app to POST the session bundle back to this machine.

    Args:
        app_url: The Chat app web origin (e.g. ``https://chat.opengradient.ai``).
        open_browser: Whether to open the system browser automatically.
        timeout: How long to wait for the callback, in seconds.

    Returns:
        The saved :class:`Session`.

    Raises:
        AuthError: On timeout or an invalid bundle.
    """
    received: dict[str, Any] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default logging
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def do_OPTIONS(self):  # noqa: N802 — CORS preflight from the app origin
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            ok = True
            try:
                received.update(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                ok = False
            self.send_response(200 if ok else 400)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}' if ok else b'{"ok": false}')
            if ok:
                done.set()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}"
    auth_url = f"{app_url.rstrip('/')}/cli-auth?{urlencode({'redirect_uri': redirect_uri})}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        print(f"Opening browser to authorize this device:\n  {auth_url}\n")
        if open_browser:
            webbrowser.open(auth_url)
        print("Waiting for authorization… (Ctrl-C to cancel; or use `og-local login --manual`)")
        if not done.wait(timeout=timeout):
            raise AuthError("timed out waiting for browser authorization")
    finally:
        server.shutdown()

    return _finish_login(received)


def login_manual(bundle_json: str) -> Session:
    """Persist a session from a pasted CLI-auth bundle (browser-can't-reach-loopback fallback)."""
    try:
        data = json.loads(bundle_json)
    except json.JSONDecodeError as exc:
        raise AuthError(f"pasted bundle is not valid JSON: {exc}") from exc
    return _finish_login(data)


def _finish_login(data: dict) -> Session:
    if data.get("type") != BUNDLE_TYPE:
        raise AuthError(f"unexpected bundle type: {data.get('type')!r}")
    if not data.get("access_token") or "config" not in data:
        raise AuthError("bundle is missing an access token or config")
    session = Session(data)
    # Validate the config fields up front so failures surface at login, not first use.
    _ = session.config
    session.save()
    return session


def _require(d: dict, key: str) -> str:
    value = d.get(key)
    if not value:
        raise AuthError(f"CLI-auth config is missing '{key}'")
    return value
