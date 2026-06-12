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
from veil.pii import PiiSetupError, Redactor
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

        # Local PII redaction, applied to a request before it is encrypted to the
        # TEE. ``config.pii_scrub`` is the *default*; individual requests can flip
        # it on/off (see Gateway.chat / the server's header+body parsing). The
        # Presidio engine is built lazily and cached: if scrubbing is the server
        # default we build it eagerly below so misconfiguration fails fast at
        # startup, otherwise it's built on the first request that asks for it.
        self._pii_default = config.pii_scrub
        self._redactor: Redactor | None = None
        self._redactor_error: PiiSetupError | None = None
        self._redactor_lock = threading.Lock()

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

        # Fail fast at startup if scrubbing is the server default but unavailable,
        # rather than at the first request.
        if self._pii_default:
            self._ensure_redactor()

    # --- PII redaction -----------------------------------------------------
    def _ensure_redactor(self) -> Redactor:
        """Build (once) and return the Presidio redactor, or raise PiiSetupError.

        The failure is cached so a missing ``[pii]`` extra doesn't pay the import
        cost on every request — and so the proxy fails closed rather than ever
        forwarding a prompt it was asked to scrub.
        """
        with self._redactor_lock:
            if self._redactor_error is not None:
                raise self._redactor_error
            if self._redactor is None:
                try:
                    self._redactor = Redactor()
                except PiiSetupError as exc:
                    self._redactor_error = exc
                    raise
            return self._redactor

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
    def chat(self, body: dict, *, scrub: bool | None = None) -> VerifiedChatResponse:
        # Resolve the per-request preference over the server default, then redact
        # PII locally before anything is sealed to the enclave. Done once, outside
        # the retry loop, so a gateway re-selection resends the already-scrubbed
        # body rather than re-scrubbing. Raises PiiSetupError if scrubbing was
        # requested but the [pii] extra isn't installed (fail closed).
        if scrub if scrub is not None else self._pii_default:
            body = self._ensure_redactor().scrub_request(body)
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
