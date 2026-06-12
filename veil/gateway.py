"""Bridge between a saved Chat session and the OpenGradient verified-inference SDK.

Resolves a TEE from the on-chain registry (honoring an optional pinned tee_id or
reproducible-build PCR), then sends OpenAI-style chat requests through the
chat-api OHTTP relay using the SDK's :class:`opengradient.OhttpRelayClient`. Every
response is verified against the enclave's registry signing key before it is
returned — nothing unverified ever leaves this process.
"""

from __future__ import annotations

import logging
import random
import threading

import requests
from opengradient import OhttpRelayClient, TEERegistry, VerifiedChatResponse
from opengradient.client.tee_registry import TEE_TYPE_LLM_PROXY, TEEEndpoint

from veil.config import OHTTP_RELAY_PATH, ServerConfig
from veil.pii import build_redactor
from veil.session import Session

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """No usable TEE could be resolved from the registry."""


class Gateway:
    """Resolves a TEE and runs verified chat completions through the relay."""

    def __init__(self, session: Session, config: ServerConfig):
        self._session = session
        self._config = config
        self._lock = threading.Lock()
        self._client: OhttpRelayClient | None = None
        self._tee: TEEEndpoint | None = None
        # Optional local PII redaction, applied to the request before it is
        # encrypted to the TEE. ``None`` when disabled (the default).
        self._redactor = build_redactor(enabled=config.pii_scrub)

        cfg = session.config
        if not cfg.tee_registry_rpc_url or not cfg.tee_registry_address:
            raise GatewayError(
                "the Chat session did not include a TEE registry RPC + address; "
                "this build of the relay does not support the registry path"
            )
        self._registry = TEERegistry(cfg.tee_registry_rpc_url, cfg.tee_registry_address)
        self._relay_url = cfg.chat_api_base_url.rstrip("/") + OHTTP_RELAY_PATH
        self._tee_type = (
            cfg.tee_registry_tee_type
            if cfg.tee_registry_tee_type is not None
            else TEE_TYPE_LLM_PROXY
        )

    # --- TEE resolution ----------------------------------------------------
    def _select_tee(self) -> TEEEndpoint:
        tees = [
            t
            for t in self._registry.get_active_tees_by_type(self._tee_type)
            if t.ohttp_config is not None
            and len(t.ohttp_config.public_key) == 32
            and t.signing_public_key_der
        ]
        if not tees:
            raise GatewayError("the TEE registry has no active OHTTP-capable gateways")

        if self._config.expected_pcr_hash:
            want = self._config.expected_pcr_hash.lower()
            tees = [t for t in tees if t.pcr_hash.lower() == want]
            if not tees:
                raise GatewayError(
                    f"no registry gateway matches the pinned PCR hash {self._config.expected_pcr_hash} "
                    "— refusing to use an unverified enclave"
                )

        if self._config.pinned_tee_id:
            want = self._config.pinned_tee_id.lower()
            for t in tees:
                if t.tee_id.lower() == want:
                    return t
            raise GatewayError(
                f"pinned tee_id {self._config.pinned_tee_id} not found among active gateways"
            )

        return random.choice(tees)

    def _get_client(self) -> OhttpRelayClient:
        with self._lock:
            if self._client is None:
                self._tee = self._select_tee()
                logger.info(
                    "Selected TEE %s (%s) pcr=%s",
                    self._tee.tee_id,
                    self._tee.endpoint,
                    self._tee.pcr_hash,
                )
                self._client = OhttpRelayClient(
                    self._relay_url,
                    self._tee,
                    auth_headers=self._session.auth_headers,
                )
            return self._client

    def reset(self) -> None:
        """Drop the cached TEE/client so the next call reselects (e.g. after a failure)."""
        with self._lock:
            self._client = None
            self._tee = None

    @property
    def active_tee(self) -> TEEEndpoint | None:
        return self._tee

    # --- inference ---------------------------------------------------------
    def chat(self, body: dict) -> VerifiedChatResponse:
        # Redact PII locally before anything is sealed to the enclave. Done once,
        # outside the retry loop, so a gateway re-selection resends the already-
        # scrubbed body rather than re-scrubbing.
        if self._redactor is not None:
            body = self._redactor.scrub_request(body)
        try:
            return self._chat_once(body)
        except requests.exceptions.RequestException as exc:
            # The selected TEE became unreachable (offline, rotated out, network
            # blip). Drop it, pick another active gateway from the registry, and
            # retry once — so a single dead node doesn't take the proxy down.
            logger.warning(
                "TEE request failed (%s) — reselecting a gateway and retrying", type(exc).__name__
            )
            self.reset()
            return self._chat_once(body)

    def _chat_once(self, body: dict) -> VerifiedChatResponse:
        client = self._get_client()
        if body.get("stream"):
            return client.stream_chat_completion(body)
        return client.chat_completion(body)
