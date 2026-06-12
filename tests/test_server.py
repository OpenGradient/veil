"""Server-layer tests: OpenAI-shaped responses, streaming replay, and the
guarantee that an unverifiable response never reaches the agent.

The gateway is stubbed (the SDK's OHTTP/verification path is tested in the SDK's
own suite), so these focus on the local HTTP surface.
"""

from __future__ import annotations


from opengradient import RelayError, VerificationError, VerifiedChatResponse
from opengradient.client.tee_verify import TeeProof

from og_local.server import create_app


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

    def chat(self, body):
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


def test_non_json_request_rejected():
    client = _client(_StubGateway())
    resp = client.post("/v1/chat/completions", data="not json", content_type="text/plain")
    assert resp.status_code == 415


def test_models_and_health():
    client = _client(_StubGateway())
    assert client.get("/v1/models").get_json()["object"] == "list"
    health = client.get("/health").get_json()
    assert health["status"] == "ok"
    assert health["active_tee_id"] == "0xabc"
