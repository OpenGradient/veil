# OpenGradient Local

**Drop-in, self-verifying confidential inference for AI agents.**

Point any OpenAI SDK at `og-local` with one env var. It routes your prompts to
OpenGradient's network of attestable TEE gateways and **cryptographically
verifies every response before a single token reaches your code** — so you trust
math, not us, the host, or the network. Your agent's code doesn't change.

## Quickstart

```sh
# install (needs Python 3.11+; uv grabs one for you)
uv tool install opengradient-local        # or: pipx install opengradient-local

# run — logs you in (browser) the first time, then serves in the background
og-local
```

Point your agent at it:

```sh
export OPENAI_BASE_URL=http://127.0.0.1:11434/v1
export OPENAI_API_KEY=og-local            # ignored; your Chat login authenticates
```

```python
from openai import OpenAI

client = OpenAI()  # picks up the env vars above
r = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Explain TEE attestation in one line."}],
)
print(r.choices[0].message.content)
```

That's it. Every response is verified before you see it — check the
`X-OpenGradient-Verified: true` header (and the `opengradient_verification` block
on the body). Streaming works too; it's verified before the first token replays.

Useful commands: `og-local stop`, `og-local status`, `og-local endpoint` (prints
the env vars), `og-local logout`. Run `sudo og-local endpoint` once if you'd like
a prettier `http://opengradient.inference:11434/v1` base URL.

---

## How it works

```
  your agent ──OpenAI SDK──▶ og-local ──HPKE-encrypted──▶ relay ──▶ TEE gateway
                                 ▲         (sees only ciphertext)     (runs the LLM,
                                 │                                     signs in-enclave)
                                 └──── verifies the enclave's signature, then replies
```

1. **Discover** — picks a TEE from the on-chain registry (endpoint, encryption
   key, signing key, `pcrHash`).
2. **Encrypt** — HPKE/Oblivious-HTTP-encrypts the request; the relay (which pays
   per call against your Chat account) only sees ciphertext.
3. **Verify** — checks the enclave's RSA-PSS signature over the request/response
   hashes before handing anything back.

**Trust chain:** `reproducible build → PCRs → on-chain registry (pcrHash +
signing key) → per-response signature`. The registry only admits a TEE whose
Nitro attestation matches a known-good build. Pin it tighter with
`--expected-pcr <hash>` to refuse any gateway whose `pcrHash` differs.

The protocol (registry discovery, OHTTP, verification) lives in the
**OpenGradient SDK** (`OhttpRelayClient`, `TEERegistry`, `verify_response`), so
this process and the web client share one implementation. This repo adds login +
the local OpenAI-compatible server.

## Commands

| Command | What it does |
|---------|--------------|
| `og-local` | Set up on first run, then serve (detached). The one command you need. |
| `og-local stop` | Stop the background server. |
| `og-local status` | Login + network config + whether the server is running. |
| `og-local endpoint` | Print the agent env vars (`sudo` to also map `opengradient.inference`). |
| `og-local update` | Update og-local to the latest version. |
| `og-local login` | Authorize / re-authorize this device. |
| `og-local setup` | Re-run the setup wizard. |
| `og-local serve -f` | Run blocking in the foreground (for systemd/Docker). |
| `og-local logout` | Remove the saved session. |

## Lifecycle

- **Background by default.** Setup/login runs in the foreground, then it detaches
  and frees your terminal. Logs: `~/.opengradient/local/server.log`. Use
  `--foreground` to block instead.
- **Stays signed in.** The access token auto-refreshes. If you sign out in the
  Chat app, the next request tells you to run `og-local login`.
- **Survives a dead node.** If the chosen TEE goes offline, it reselects another
  from the registry and retries once.

## Configuration

Session + prefs live in `~/.opengradient/local/` (override with `OG_LOCAL_HOME`).

| Env var | Flag | Default | Purpose |
|---------|------|---------|---------|
| `OG_LOCAL_PORT` | `--port` | `11434` | Bind port. |
| `OG_LOCAL_HOST` | `--host` | `127.0.0.1` | Bind host. |
| `OG_LOCAL_TEE_ID` | `--tee-id` | — | Pin a specific registry TEE. |
| `OG_LOCAL_EXPECTED_PCR_HASH` | `--expected-pcr` | — | Refuse any TEE whose `pcrHash` differs. |
| `OG_LOCAL_APP_URL` | `--app-url` | `https://chat.opengradient.ai` | Chat app origin for login. |

## Notes & limitations

- **OpenAI-compatible only** (`/v1/chat/completions`, `/v1/models`); an Anthropic
  `/v1/messages` shim is a planned follow-up.
- **Verify-before-emit** trades a little first-token latency for the guarantee
  that no unverified token leaves the machine.
- **Payment via your Chat account** (the relay settles x402 server-side) — no
  wallet or key lives here. For a self-custodial wallet path, see the SDK's
  `og.LLM`.

## Development

```sh
git clone https://github.com/OpenGradient/local && cd local
uv sync --all-groups
uv run pytest
uv run ruff check . && uv run mypy og_local
```

Protocol-level crypto is tested in the SDK repo against the real tee-gateway
recipient code, guaranteeing wire compatibility.
