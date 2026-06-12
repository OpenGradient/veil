"""Server-layer tests: OpenAI-shaped responses, streaming replay, and the
guarantee that an unverifiable response never reaches the agent.

The gateway is stubbed (the SDK's OHTTP/verification path is tested in the SDK's
own suite), so these focus on the local HTTP surface.
"""

from __future__ import annotations


from opengradient import RelayError, VerificationError, VerifiedChatResponse
from opengradient.client.tee_verify import TeeProof

from veil.pii import PiiSetupError
from veil.server import create_app


def _proof():
    return TeeProof(
        tee_id="0xabc",
        request_hash="11" * 32,
        output_hash="22" * 32,
        timestamp=1_700_000_000,
        signature="sig",
        signing_key_pem="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n",
        tee_host="https://gw.example",
    )


class _StubGateway:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.active_tee = type("T", (), {"tee_id": "0xabc", "endpoint": "https://gw.example"})()
        self.last_body = None
        self.last_scrub = "unset"

    def chat(self, body, *, scrub=None):
        self.last_body = body
        self.last_scrub = scrub
        if self._error:
            raise self._error
        return self._result


def _client(gateway):
    app = create_app(gateway)
    app.config.update(TESTING=True)
    return app.test_client()


def test_non_streaming_returns_openai_shape_with_verification():
    body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
        ],
        "tee_signature": "sig",
        "tee_id": "0xabc",
    }
    result = VerifiedChatResponse(body=body, content="hi", proof=_proof())
    client = _client(_StubGateway(result=result))

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4.1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.headers["X-OpenGradient-Verified"] == "true"
    data = resp.get_json()
    assert data["choices"][0]["message"]["content"] == "hi"
    assert data["opengradient_verification"]["verified"] is True
    assert data["opengradient_verification"]["tee_id"] == "0xabc"


def test_streaming_replays_frames_then_done():
    frames = [
        'data: {"choices":[{"delta":{"content":"Hello "},"index":0}]}\n\n',
        'data: {"choices":[{"delta":{"content":"world"},"index":0}]}\n\n',
        'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}],"tee_signature":"sig"}\n\n',
        "data: [DONE]\n\n",
    ]
    result = VerifiedChatResponse(
        body={}, content="Hello world", proof=_proof(), stream_frames=frames
    )
    client = _client(_StubGateway(result=result))

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4.1", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == 200
    assert resp.headers["X-OpenGradient-Verified"] == "true"
    text = resp.get_data(as_text=True)
    assert "Hello " in text and "world" in text
    # Exactly one DONE, emitted by us at the end.
    assert text.count("[DONE]") == 1
    assert text.rstrip().endswith("data: [DONE]")


def test_verification_failure_is_not_leaked():
    client = _client(_StubGateway(error=VerificationError("RSA-PSS signature verification failed")))
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4.1", "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status_code == 502
    data = resp.get_json()
    assert "verification failed" in data["error"]["message"].lower()
    # No assistant content of any kind in a failed-verification response.
    assert "choices" not in data


def test_relay_error_status_is_propagated():
    client = _client(_StubGateway(error=RelayError(402, "payment required")))
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4.1", "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status_code == 402
    assert "payment required" in resp.get_json()["error"]["message"]


def test_relay_401_suggests_relogin():
    client = _client(_StubGateway(error=RelayError(401, "unauthorized")))
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4.1", "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status_code == 401
    assert "og-veil login" in resp.get_json()["error"]["message"]


def test_non_json_request_rejected():
    client = _client(_StubGateway())
    resp = client.post("/v1/chat/completions", data="not json", content_type="text/plain")
    assert resp.status_code == 415


def _ok_result():
    body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
        ],
    }
    return VerifiedChatResponse(body=body, content="hi", proof=_proof())


def test_pii_scrub_defaults_to_none_override():
    # No header / body field → no per-request override; gateway uses its default.
    gw = _StubGateway(result=_ok_result())
    _client(gw).post(
        "/v1/chat/completions",
        json={"model": "gpt-4.1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert gw.last_scrub is None


def test_pii_scrub_body_field_overrides_and_is_stripped():
    gw = _StubGateway(result=_ok_result())
    _client(gw).post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "hi"}],
            "pii_scrub": True,
        },
    )
    assert gw.last_scrub is True
    # The control field must not be forwarded to the TEE.
    assert "pii_scrub" not in gw.last_body


def test_pii_scrub_header_override():
    gw = _StubGateway(result=_ok_result())
    _client(gw).post(
        "/v1/chat/completions",
        json={"model": "gpt-4.1", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-OpenGradient-PII-Scrub": "true"},
    )
    assert gw.last_scrub is True


def test_pii_scrub_body_field_beats_header():
    gw = _StubGateway(result=_ok_result())
    _client(gw).post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "hi"}],
            "pii_scrub": False,
        },
        headers={"X-OpenGradient-PII-Scrub": "true"},
    )
    assert gw.last_scrub is False


def test_pii_setup_error_fails_closed():
    # Scrubbing requested but the extra isn't installed → error, never a leak.
    client = _client(_StubGateway(error=PiiSetupError("install the [pii] extra")))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "x"}],
            "pii_scrub": True,
        },
    )
    assert resp.status_code == 503
    assert "pii" in resp.get_json()["error"]["message"].lower()
    assert "choices" not in resp.get_json()


def test_models_and_health():
    client = _client(_StubGateway())
    assert client.get("/v1/models").get_json()["object"] == "list"
    health = client.get("/health").get_json()
    assert health["status"] == "ok"
    assert health["active_tee_id"] == "0xabc"
