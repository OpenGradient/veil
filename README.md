# OpenGradient Local

**Drop-in, self-verifying private inference for AI agents.**

Point any OpenAI-compatible SDK at this local process with a single env var. It
transparently routes your prompts to OpenGradient's verifiable-inference
network — a decentralized fleet of attestable AWS Nitro TEE gateways — and
**cryptographically proves, before any token leaves your machine**, that:

1. inference ran *only* inside a known enclave running our reproducible,
   publicly-verifiable code, and
2. the response is unmodified.

Your agent's code doesn't change. You're not trusting us, the host, or the
network — you're trusting **math against an open network** of reproducible-PCR
nodes (including third-party operators). That's the difference between "an
endpoint you have to trust us to use correctly" and inference that verifies
itself.

## How it works

```
  your agent ──OpenAI SDK──▶  og-local (this process, on your machine)
                                  │
                                  │  1. resolve a TEE from the on-chain registry
                                  │     (endpoint, OHTTP key, signing key, pcrHash)
                                  │  2. HPKE-encrypt the request (Oblivious HTTP)
                                  ▼
                            chat-api relay  ──(pays x402, sees only ciphertext)──▶  TEE gateway
                                  ▲                                                    │ runs the LLM,
                                  │  3. decrypt response                               │ signs it inside
                                  │  4. VERIFY the enclave's RSA-PSS signature         │ the enclave
                                  │     against the registry signing key              ▼
  your agent ◀──verified result── og-local        ◀───────────────────────────────────
```

The heavy lifting — registry discovery, Oblivious HTTP encryption, and response
verification — lives in the **OpenGradient SDK** (`opengradient.OhttpRelayClient`,
`opengradient.TEERegistry`, `opengradient.verify_response`), so this process and
the web client share one audited, non-drifting implementation. This repo adds
only what's local: login, and the OpenAI-compatible HTTP shim.

### Trust chain

```
reproducible build → PCRs → on-chain registry entry (pcrHash + signing key)
                   → per-response RSA-PSS signature
```

The registry only admits a TEE after its Nitro attestation is verified against a
known-good PCR set. `og-local` resolves a TEE from that registry and verifies the
enclave's signature on **every** response. For maximum assurance, pin the
expected reproducible-build PCR (`--expected-pcr`) and any TEE whose on-chain
`pcrHash` differs is refused outright.

## Quick start

```sh
pipx install opengradient-local      # or: pip install opengradient-local
og-local                             # does everything: logs you in, then serves
```

That single command opens a browser to authorize this device with your
OpenGradient Chat account (the relay settles payment against your account, so no
wallet or private key ever lives in this process), then starts the local server:

```
OpenGradient Local listening on http://127.0.0.1:11434
```

Then point your agent at it — **the only change**:

```sh
export OPENAI_BASE_URL=http://127.0.0.1:11434/v1
export OPENAI_API_KEY=og-local   # ignored; your Chat session authenticates
```

> **Prettier URL (optional).** `og-local --setup-host` also maps
> `http://opengradient.inference` → your local server (one-time, edits the hosts
> file), so agents can use `http://opengradient.inference:11434/v1`. Add
> `--port 80` for the clean no-port form (needs elevated privileges).

```python
from openai import OpenAI

client = OpenAI()  # picks up OPENAI_BASE_URL / OPENAI_API_KEY
resp = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Explain TEE attestation in one line."}],
)
print(resp.choices[0].message.content)
# Every response is verified before it reaches you. See the headers:
#   X-OpenGradient-Verified: true
#   X-OpenGradient-TEE-Id:  0x...
# and the `opengradient_verification` block on the JSON body.
```

Streaming works too (`stream=True`); the response is fully verified before the
first token is replayed to your agent — no unverified token ever leaves the
machine.

## Commands

| Command | Description |
|---------|-------------|
| `og-local` | **The one command** — log in if needed, then serve. |
| `og-local serve [--port] [--tee-id] [--expected-pcr] [--setup-host] ...` | Same as above, with options (auto-logs-in if no session). |
| `og-local login [--app-url URL] [--manual]` | Just authorize this device (default `https://chat.opengradient.ai`). |
| `og-local setup-host` | Just map `opengradient.inference` → `127.0.0.1` in the hosts file. |
| `og-local status` | Show login + resolved network config. |
| `og-local logout` | Remove the saved session. |

## Install from source (development)

```sh
git clone https://github.com/OpenGradient/local && cd local
uv sync --all-groups
uv run og-local --help
```

## Configuration

Login stores a session (and the network config it needs) in
`~/.opengradient/local/session.json` (override the directory with
`OG_LOCAL_HOME`). Server knobs:

| Env var | Flag | Default | Purpose |
|---------|------|---------|---------|
| `OG_LOCAL_HOST` | `--host` | `127.0.0.1` | Bind host. |
| `OG_LOCAL_PORT` | `--port` | `11434` | Bind port. |
| `OG_LOCAL_TEE_ID` | `--tee-id` | — | Pin a specific registry TEE. |
| `OG_LOCAL_EXPECTED_PCR_HASH` | `--expected-pcr` | — | Refuse any TEE whose on-chain `pcrHash` differs. |
| `OG_LOCAL_HOSTNAME` | — | `opengradient.inference` | Friendly local hostname advertised to agents. |
| `OG_LOCAL_APP_URL` | `--app-url` (login) | `https://chat.opengradient.ai` | Chat app origin for login. |

## Scope & limitations (MVP)

- **OpenAI-compatible only.** `/v1/chat/completions` and `/v1/models`. An
  Anthropic `/v1/messages` translation layer is a planned follow-up.
- **Verify-before-emit streaming.** Streaming buffers and verifies the full
  response before replaying it, trading first-token latency for the "no
  unverified token leaves the machine" guarantee.
- **Payment via Chat credentials.** The relay pays x402 server-side against your
  Chat account. A wallet/x402 path (the SDK's `og.LLM`) is the alternative for
  fully self-custodial setups.

## Development

```sh
uv sync --group test
uv run pytest          # server-layer tests
uv run ruff check .
```

The protocol-level crypto (OHTTP, signature verification, request
canonicalization) is tested in the SDK repo against the real tee-gateway
recipient code, guaranteeing wire compatibility.
