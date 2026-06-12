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
from opengradient import TEE_LLM, RelayError, VerificationError
from opengradient.client.tee_verify import UnsupportedRequestError

from veil.config import ServerConfig
from veil.gateway import Gateway, GatewayError
from veil.pii import PiiSetupError
from veil.session import AuthError, Session

# Per-request PII-scrub control. Clients flip scrubbing on/off without restarting
# the proxy: a default header set once in the client config (the easy path), or a
# body field via the OpenAI SDK's ``extra_body`` (per call). Either overrides the
# server's ``--pii-scrub`` default; the body field wins if both are present.
_PII_HEADER = "X-OpenGradient-PII-Scrub"
_PII_BODY_FIELD = "pii_scrub"
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

logger = logging.getLogger(__name__)

# Model list for clients that probe /v1/models. Derived from the SDK's canonical
# ``TEE_LLM`` enum so it stays in sync with the network as models are added or
# retired — no hand-maintained copy to go stale. Each enum value is
# ``provider/model``; callers (and the gateway) use the bare model name, which is
# what the SDK sends over the wire (``model.split("/")[1]``). The gateway remains
# the source of truth for what's actually routable; this is a convenience.
_KNOWN_MODELS = [m.value.split("/", 1)[1] for m in TEE_LLM]


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
        # Resolve the scrub preference before stripping our control field so it
        # never reaches the TEE as an unknown OpenAI parameter.
        scrub = _scrub_preference(body, request.headers)
        if isinstance(body, dict):
            body.pop(_PII_BODY_FIELD, None)
        try:
            result = gateway.chat(body, scrub=scrub)
        except PiiSetupError as exc:
            return _error(503, f"PII scrubbing was requested but is unavailable: {exc}")
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
                message = f"{message} — your session may have expired; run `og-veil login` to sign in again"
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


def _coerce_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUTHY:
            return True
        if lowered in _FALSY:
            return False
    return None


def _scrub_preference(body, headers) -> bool | None:
    """Per-request PII-scrub override, or ``None`` to fall back to the server default.

    The body field takes precedence over the header; an unrecognized value is
    ignored (treated as "no preference") rather than guessed at.
    """
    if isinstance(body, dict) and _PII_BODY_FIELD in body:
        decided = _coerce_bool(body.get(_PII_BODY_FIELD))
        if decided is not None:
            return decided
    return _coerce_bool(headers.get(_PII_HEADER))


def _error(status: int, message: str):
    # OpenAI-style error envelope so client SDKs surface a useful message.
    resp = jsonify({"error": {"message": message, "type": "veil_error"}})
    resp.status_code = status
    return resp


def serve(config: ServerConfig) -> None:
    """Load the session, resolve a TEE, and run the local server (blocking)."""
    session = Session.load()
    try:
        gateway = Gateway(session, config)
    except PiiSetupError as exc:
        # PII scrubbing was requested but the optional extra/model isn't present.
        raise SystemExit(f"PII scrubbing is enabled but unavailable: {exc}")
    # Resolve a TEE eagerly so misconfiguration fails fast and /health is useful.
    try:
        gateway._get_client()  # noqa: SLF001 — intentional eager warm-up
    except GatewayError as exc:
        raise SystemExit(f"could not resolve a TEE gateway: {exc}")

    app = create_app(gateway)
    tee = gateway.active_tee
    print(
        f"OpenGradient Veil listening on http://{config.host}:{config.port}\n"
        f"  Verified TEE: {tee.tee_id if tee else '?'} ({tee.endpoint if tee else '?'})\n"
        f"  Signed in as: {session.user_email or 'unknown'}\n\n"
        f"Point your agent at it (OpenAI SDK):\n"
        f"  export OPENAI_BASE_URL={config.advertised_base_url()}\n"
        f"  export OPENAI_API_KEY=og-veil   # ignored; the Chat session authenticates\n"
    )
    # threaded=True so streaming requests don't block health checks / other calls.
    app.run(host=config.host, port=config.port, threaded=True)
