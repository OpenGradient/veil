"""Local OpenAI-compatible HTTP server.

Point any OpenAI SDK at ``http://127.0.0.1:11434/v1`` (one env var) and every
chat completion is transparently routed through OpenGradient's TEE network and
cryptographically verified before a single token is returned. The agent's code
is unchanged.

Endpoints:
  * ``POST /v1/chat/completions`` — verified chat (streaming or not).
  * ``GET  /v1/models``           — minimal model listing.
  * ``GET  /health``              — liveness + which TEE is active.
"""

from __future__ import annotations

import logging

from flask import Flask, Response, jsonify, request
from opengradient import RelayError, VerificationError
from opengradient.client.tee_verify import UnsupportedRequestError

from og_local.config import ServerConfig
from og_local.gateway import Gateway, GatewayError
from og_local.session import AuthError, Session

logger = logging.getLogger(__name__)

# A small, stable model list for clients that probe /v1/models. The gateway is
# the source of truth for what's actually routable; this is a convenience.
_KNOWN_MODELS = [
    "gpt-5.2",
    "gpt-4.1",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "gemini-3-pro-preview",
    "grok-4",
]


def create_app(gateway: Gateway) -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        tee = gateway.active_tee
        return jsonify(
            {
                "status": "ok",
                "verified_inference": True,
                "active_tee_id": tee.tee_id if tee else None,
                "active_tee_endpoint": tee.endpoint if tee else None,
            }
        )

    @app.get("/v1/models")
    def list_models():
        return jsonify(
            {
                "object": "list",
                "data": [
                    {"id": m, "object": "model", "owned_by": "opengradient"} for m in _KNOWN_MODELS
                ],
            }
        )

    @app.post("/v1/chat/completions")
    def chat_completions():
        if not request.is_json:
            return _error(415, "request must be application/json")
        body = request.get_json()
        try:
            result = gateway.chat(body)
        except UnsupportedRequestError as exc:
            return _error(400, str(exc))
        except AuthError as exc:
            return _error(401, str(exc))
        except GatewayError as exc:
            return _error(503, str(exc))
        except VerificationError as exc:
            # The strong guarantee: a response we could not verify is never
            # surfaced to the agent. Surface it as a gateway error instead.
            logger.error("TEE verification failed: %s", exc)
            return _error(502, f"TEE response verification failed: {exc}")
        except RelayError as exc:
            message = exc.message
            # The relay rejected our Chat token (signed out / expired upstream).
            if exc.status_code in (401, 403):
                message = f"{message} — your session may have expired; run `og-local login` to sign in again"
            return _error(exc.status_code if 400 <= exc.status_code < 600 else 502, message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("unexpected error handling chat completion")
            return _error(500, f"internal error: {type(exc).__name__}")

        verification = {
            "verified": True,
            "tee_id": result.proof.tee_id,
            "request_hash": result.proof.request_hash,
            "output_hash": result.proof.output_hash,
            "timestamp": result.proof.timestamp,
            "tee_host": result.proof.tee_host,
        }

        if result.stream_frames is not None:
            return _stream_response(result.stream_frames, verification)

        payload = dict(result.body)
        payload["opengradient_verification"] = verification
        resp = jsonify(payload)
        _verification_headers(resp, verification)
        return resp

    return app


def _stream_response(frames: list[str], verification: dict) -> Response:
    """Replay the already-verified SSE frames to the agent, then DONE."""

    def generate():
        for frame in frames:
            if "[DONE]" in frame:
                continue  # we emit a single DONE at the end
            yield frame
        yield "data: [DONE]\n\n"

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    _verification_headers(resp, verification)
    return resp


def _verification_headers(resp, verification: dict) -> None:
    resp.headers["X-OpenGradient-Verified"] = "true"
    resp.headers["X-OpenGradient-TEE-Id"] = verification["tee_id"]


def _error(status: int, message: str):
    # OpenAI-style error envelope so client SDKs surface a useful message.
    resp = jsonify({"error": {"message": message, "type": "opengradient_local_error"}})
    resp.status_code = status
    return resp


def serve(config: ServerConfig) -> None:
    """Load the session, resolve a TEE, and run the local server (blocking)."""
    session = Session.load()
    gateway = Gateway(session, config)
    # Resolve a TEE eagerly so misconfiguration fails fast and /health is useful.
    try:
        gateway._get_client()  # noqa: SLF001 — intentional eager warm-up
    except GatewayError as exc:
        raise SystemExit(f"could not resolve a TEE gateway: {exc}")

    app = create_app(gateway)
    tee = gateway.active_tee
    print(
        f"OpenGradient Local listening on http://{config.host}:{config.port}\n"
        f"  Verified TEE: {tee.tee_id if tee else '?'} ({tee.endpoint if tee else '?'})\n"
        f"  Signed in as: {session.user_email or 'unknown'}\n\n"
        f"Point your agent at it (OpenAI SDK):\n"
        f"  export OPENAI_BASE_URL={config.advertised_base_url()}\n"
        f"  export OPENAI_API_KEY=og-local   # ignored; the Chat session authenticates\n"
    )
    # threaded=True so streaming requests don't block health checks / other calls.
    app.run(host=config.host, port=config.port, threaded=True)
