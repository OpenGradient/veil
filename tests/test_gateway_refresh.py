"""Tests for the Gateway's periodic TEE refresh.

The gateway caches a TEE + relay client; without a periodic check, a TEE that
rotates out of the registry (or rotates its OHTTP/signing keys) would keep
failing every request until restart, because those failures don't surface as the
network error the reactive retry path looks for. These cover the loop that drops
the stale cache so the next request reselects.
"""

from __future__ import annotations

import time

from opengradient.client.tee_registry import OhttpConfig, TEEEndpoint

from veil.config import ServerConfig
from veil.gateway import Gateway


def _make_tee(
    tee_id: str = "0xabc",
    public_key: bytes = b"\x01" * 32,
    key_id: int = 1,
    signing: bytes = b"sign",
) -> TEEEndpoint:
    return TEEEndpoint(
        tee_id=tee_id,
        endpoint="https://gw.example",
        tls_cert_der=b"cert",
        payment_address="0xpay",
        signing_public_key_der=signing,
        ohttp_config=OhttpConfig(
            key_id=key_id,
            kem_id=0x0020,
            kdf_id=0x0001,
            aead_id=0x0003,
            public_key=public_key,
            key_config=b"",
            registered_at=0,
        ),
        pcr_hash="0x00",
    )


class _FakeConfig:
    tee_registry_rpc_url = "http://localhost:8545"
    tee_registry_address = "0x0000000000000000000000000000000000000000"
    chat_api_base_url = "https://chat-api.example"
    tee_registry_tee_type = None


class _FakeSession:
    config = _FakeConfig()

    @staticmethod
    def auth_headers() -> dict:
        return {}


class _FakeRegistry:
    """Stands in for TEERegistry; returns a canned active-TEE list."""

    def __init__(self, active):
        self._active = active
        self.calls = 0

    def get_active_tees_by_type(self, tee_type):
        self.calls += 1
        return list(self._active)


def _gateway(active, **config_kwargs) -> Gateway:
    gw = Gateway(_FakeSession(), ServerConfig(**config_kwargs))
    gw._registry = _FakeRegistry(active)
    return gw


# --- _tee_still_current --------------------------------------------------------


def test_still_current_when_id_and_keys_match():
    tee = _make_tee()
    assert Gateway._tee_still_current(tee, [_make_tee()]) is True


def test_not_current_when_rotated_out():
    tee = _make_tee(tee_id="0xabc")
    assert Gateway._tee_still_current(tee, [_make_tee(tee_id="0xdef")]) is False
    assert Gateway._tee_still_current(tee, []) is False


def test_not_current_when_ohttp_key_rotated():
    tee = _make_tee(public_key=b"\x01" * 32)
    rotated = _make_tee(public_key=b"\x02" * 32)
    assert Gateway._tee_still_current(tee, [rotated]) is False


def test_not_current_when_signing_key_rotated():
    tee = _make_tee(signing=b"old")
    rotated = _make_tee(signing=b"new")
    assert Gateway._tee_still_current(tee, [rotated]) is False


# --- _refresh_once -------------------------------------------------------------


def test_refresh_keeps_client_when_tee_unchanged():
    gw = _gateway([_make_tee()])
    gw._tee = _make_tee()
    sentinel = object()
    gw._client = sentinel

    gw._refresh_once()

    assert gw._client is sentinel
    assert gw._tee is not None


def test_refresh_drops_client_when_tee_rotated_out():
    gw = _gateway([])  # registry no longer lists our TEE
    gw._tee = _make_tee()
    gw._client = object()

    gw._refresh_once()

    assert gw._client is None
    assert gw._tee is None


def test_refresh_noop_before_any_tee_resolved():
    gw = _gateway([_make_tee()])
    # No TEE resolved yet — nothing to check, and the registry shouldn't be hit.
    gw._refresh_once()
    assert gw._registry.calls == 0


# --- loop lifecycle ------------------------------------------------------------


def test_loop_disabled_when_interval_not_positive():
    gw = _gateway([_make_tee()], tee_refresh_interval=0)
    gw.start_refresh_loop()
    assert gw._refresh_thread is None


def test_loop_drops_stale_tee_then_stops():
    gw = _gateway([], tee_refresh_interval=0.02)
    gw._tee = _make_tee()
    gw._client = object()
    gw.start_refresh_loop()
    try:
        deadline = time.monotonic() + 2.0
        while gw._tee is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gw._tee is None
        assert gw._client is None
    finally:
        gw.stop_refresh_loop()
    assert gw._refresh_thread is None
