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
from typing import Sequence

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
        # Background loop that periodically re-checks the registry and drops the
        # cached TEE once it rotates out / rotates keys (see ``start_refresh_loop``).
        self._stop_refresh = threading.Event()
        self._refresh_thread: threading.Thread | None = None
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

    # --- periodic registry refresh -----------------------------------------
    def start_refresh_loop(self) -> None:
        """Start the background loop that re-checks the registry, if enabled.

        The cached TEE/client is otherwise only reselected reactively, when a
        request raises a network error (see :meth:`chat`). That misses the case the
        operator hits in practice: the registry rotates a gateway out (or rotates
        its OHTTP/signing keys) while this process holds a stale endpoint. Those
        failures surface as ``RelayError``/``VerificationError``, which the reactive
        path doesn't retry — so without this loop the proxy keeps hammering a dead
        TEE indefinitely. This mirrors the SDK's ``RegistryTEEConnection`` refresh.

        Idempotent and a no-op when ``tee_refresh_interval <= 0``.
        """
        if self._config.tee_refresh_interval <= 0:
            return
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            return
        self._stop_refresh.clear()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop, name="veil-tee-refresh", daemon=True
        )
        self._refresh_thread.start()

    def stop_refresh_loop(self) -> None:
        """Signal the background refresh loop to exit and wait briefly for it."""
        self._stop_refresh.set()
        thread = self._refresh_thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._refresh_thread = None

    def _refresh_loop(self) -> None:
        interval = self._config.tee_refresh_interval
        # Event.wait doubles as the sleep so ``stop_refresh_loop`` wakes us promptly.
        while not self._stop_refresh.wait(interval):
            try:
                self._refresh_once()
            except Exception:  # noqa: BLE001 — never let the loop die on a transient error
                logger.warning(
                    "Background TEE refresh failed; will retry next cycle.", exc_info=True
                )

    def _refresh_once(self) -> None:
        """Drop the cached client if its TEE is no longer active/unchanged in the registry."""
        current = self._tee
        if current is None:
            return  # nothing resolved yet; the first request will select one

        active = self._registry.get_active_tees_by_type(self._tee_type)
        if self._tee_still_current(current, active):
            logger.debug(
                "Current TEE %s still active and unchanged; no refresh needed.", current.tee_id
            )
            return

        logger.info(
            "TEE %s rotated out of the registry (or rotated keys) — dropping cached gateway so the next request reselects.",
            current.tee_id,
        )
        # Only clear if we'd be clearing the same TEE we just inspected; a concurrent
        # reactive reset() may already have moved us onto a fresh one.
        with self._lock:
            if self._tee is current:
                self._client = None
                self._tee = None

    @staticmethod
    def _tee_still_current(current: TEEEndpoint, active: Sequence[TEEEndpoint]) -> bool:
        """True if ``current`` is still active with the same key material.

        Matching on ``tee_id`` alone isn't enough: a gateway can keep its id while
        rotating the OHTTP/HPKE key or its signing key, which silently breaks the
        cached client's encryption and signature checks. Compare the exact bits the
        cached :class:`OhttpRelayClient` pinned at construction.
        """
        want = current.tee_id.lower()
        for tee in active:
            if tee.tee_id.lower() != want:
                continue
            same_ohttp = (
                tee.ohttp_config is not None
                and current.ohttp_config is not None
                and tee.ohttp_config.public_key == current.ohttp_config.public_key
                and tee.ohttp_config.key_id == current.ohttp_config.key_id
            )
            same_signing = tee.signing_public_key_der == current.signing_public_key_der
            return same_ohttp and same_signing
        return False

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
