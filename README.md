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

On the **first run** that single command walks you through a short setup wizard:

1. **Log in** — opens a browser to authorize this device with your OpenGradient
   Chat account (the relay settles payment against your account, so no wallet or
   private key ever lives in this process).
2. **Friendly URL (optional)** — asks whether to map
   `http://opengradient.inference` → your local server so agents can use a clean
   base URL. Your choice is remembered.

Then it starts the server **in the background** and hands your terminal back
(subsequent runs skip the wizard):

```
✓ OpenGradient Local running in the background (pid 4321).
  Base URL : http://127.0.0.1:11434/v1
  Logs     : ~/.opengradient/local/server.log
  Stop     : og-local stop
```

Point your agent at it — **the only change**:

```sh
export OPENAI_BASE_URL=http://opengradient.inference:11434/v1   # or http://127.0.0.1:11434/v1
export OPENAI_API_KEY=og-local   # ignored; your Chat session authenticates
```

> Forgot your endpoint, or want the friendly `opengradient.inference` URL set up?
> Run **`og-local endpoint`** (it prints the env vars), or **`sudo og-local
> endpoint`** to also write the hosts-file mapping. Re-run the full wizard with
> `og-local setup`.

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
| `og-local` | **The one command** — first-run setup wizard if needed, then serve. |
| `og-local setup [-y]` | Re-run the setup wizard (login + friendly-URL choice). |
| `og-local serve [--port] [--tee-id] [--expected-pcr] [-f/--foreground] ...` | Serve (runs setup on first use). Detaches by default; `-f` blocks. |
| `og-local stop` | Stop the background server. |
| `og-local endpoint` | Print the agent env vars; run `sudo og-local endpoint` to map `opengradient.inference`. |
| `og-local login [--app-url URL] [--manual]` | Authorize / re-authorize this device (default `https://chat.opengradient.ai`). |
| `og-local status` | Show login, network config, and whether a background server is running. |
| `og-local logout` | Remove the saved session. |

### Background vs foreground

The server **detaches by default** — setup/login runs in the foreground, then it
backgrounds and frees your terminal. Manage it with:

```sh
og-local status     # shows "Background: running (pid …)"
og-local stop       # stops it
```

Logs go to `~/.opengradient/local/server.log`. To run blocking in the foreground
instead — e.g. under **systemd**, **Docker**, or a process manager — use
`og-local serve --foreground`.

## Staying signed in

The Chat session is managed for you over the long run:

- **Auto-refresh.** The short-lived access token is refreshed automatically (a
  minute before it expires) using the stored refresh token — you don't restart
  anything.
- **Signed out upstream?** If you sign out in the Chat app (or the session is
  revoked), the next request fails with a clear message telling you to run
  **`og-local login`** to re-authorize. Re-login overwrites the saved session in
  place; nothing else changes.
- **A gateway goes offline?** If the selected TEE becomes unreachable, the proxy
  transparently reselects another active gateway from the on-chain registry and
  retries once, so a single dead node doesn't take you down.

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
