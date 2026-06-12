"""Runtime configuration and on-disk paths.

Almost all of the *network* configuration (chat-api relay URL, TEE registry RPC
and address, Supabase endpoint for token refresh) is delivered at login time
inside the CLI-auth bundle — see :mod:`veil.session`. This module only holds
the handful of local knobs the operator controls directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Relay endpoint on the chat-api (the OpenGradient OHTTP relay). The relay holds
# the x402 wallet and pays per request; the agent's account credits are settled
# server-side against the session token, so no wallet ever lives in this process.
OHTTP_RELAY_PATH = "/api/v1/chat/ohttp"

# Default chat-app web origin used to start the browser login flow. Override with
# OG_VEIL_APP_URL or `og-veil login --app-url ...`.
DEFAULT_APP_URL = os.getenv("OG_VEIL_APP_URL", "https://chat.opengradient.ai")


def config_home() -> Path:
    """Directory holding the saved login session.

    Defaults to ``~/.opengradient/local`` and is overridable with
    ``OG_VEIL_HOME`` (handy for tests and for running several identities).
    """
    override = os.getenv("OG_VEIL_HOME")
    base = Path(override) if override else Path.home() / ".opengradient" / "local"
    base.mkdir(parents=True, exist_ok=True)
    return base


def session_path() -> Path:
    return config_home() / "session.json"


@dataclass
class ServerConfig:
    """Settings for the local OpenAI-compatible server."""

    host: str = "127.0.0.1"
    port: int = 11434

    # Optional PCR pin. The on-chain registry only admits TEEs whose attestation
    # was verified with a matching reproducible-build PCR set, so trusting the
    # registry's signing key already ties responses to known code. Setting this
    # to the keccak256 pcrHash of *your* reproducible build adds a second, local
    # check: refuse any registry TEE whose pcrHash differs. This is what lets you
    # trust math instead of trusting the registry operator.
    expected_pcr_hash: str | None = None

    # Pin a specific tee_id (0x-prefixed) instead of randomly selecting one from
    # the registry. Useful for reproducible demos and debugging.
    pinned_tee_id: str | None = None

    @classmethod
    def from_env(cls) -> "ServerConfig":
        return cls(
            host=os.getenv("OG_VEIL_HOST", "127.0.0.1"),
            port=int(os.getenv("OG_VEIL_PORT", "11434")),
            expected_pcr_hash=_norm_hex(os.getenv("OG_VEIL_EXPECTED_PCR_HASH")),
            pinned_tee_id=_norm_hex(os.getenv("OG_VEIL_TEE_ID")),
        )

    def advertised_base_url(self) -> str:
        """The ``/v1`` base URL to tell agents to use."""
        return f"http://{self.host}:{self.port}/v1"


def _norm_hex(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    if not value.startswith("0x"):
        value = "0x" + value
    return value
