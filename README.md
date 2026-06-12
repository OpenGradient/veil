# OpenGradient Veil

**Drop-in confidential, self-verifying inference for AI agents.**

Point any OpenAI SDK at `og-veil` with one env var. Your prompts are encrypted
end-to-end to an attested TEE enclave, and every response is cryptographically
verified before it reaches your code. You trust math — not us, the host, or the
network. Your agent's code doesn't change.

- **Private & unlinkable** - Oblivious HTTP splits *who you are* from *what you
  ask* across two parties that never share both. The relay sees your identity (IP & account) but only ciphertext - never your prompt. The enclave sees your prompt
  but only the relay's IP - never you. So no one, including OpenGradient, can tie
  a user to a prompt (unless the relay and enclave collude).
- **Verified** - each response is signed *inside* the enclave and checked on your
  machine, proving it ran in known, reproducible code and wasn't tampered with.
  Nothing unverified ever reaches your agent.

## Quickstart

```sh
# install (needs Python 3.11+; uv grabs one for you)
uv tool install opengradient-local        # or: pipx install opengradient-local

# run — logs you in (browser) the first time, then serves in the background
og-veil
```

Point your agent at it:

```sh
export OPENAI_BASE_URL=http://127.0.0.1:11434/v1 
export OPENAI_API_KEY=og-veil            # ignored; your Chat login authenticates
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

Useful commands: `og-veil stop`, `og-veil status`, `og-veil endpoint` (re-prints
the env vars), `og-veil update`, `og-veil logout`.

---

## How it works

```
  your agent ──OpenAI SDK──▶ og-veil ──HPKE-encrypted──▶ relay ──▶ TEE gateway
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

### Why Oblivious HTTP?

Plain TLS to the gateway would hide your prompt from the *network*, but the
gateway itself still sees both your IP and your prompt — it can build a profile
of you. OHTTP fixes that by interposing a relay and splitting knowledge between
two parties that never share both halves:

| | sees your identity (IP/account) | sees your prompt |
|--|:--:|:--:|
| **Relay** (chat-api) | ✅ | ❌ (ciphertext only) |
| **TEE enclave** | ❌ (only the relay's IP) | ✅ |

Your request is HPKE-sealed to the enclave's key *before* it leaves your machine,
so the relay can route and bill it without ever decrypting it; the enclave
decrypts and runs it but only ever talks to the relay, so it can't see who you
are. Linking a person to a prompt would require the relay and the enclave to
collude — and the enclave's code is attested and reproducible, so it provably
doesn't log or phone home. (The relay still sees timing/size; that's inherent to
any proxy.)

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
| `og-veil` | Set up on first run, then serve (detached). The one command you need. |
| `og-veil stop` | Stop the background server. |
| `og-veil status` | Login + network config + whether the server is running. |
| `og-veil update` | Update og-veil to the latest version. |
| `og-veil login` | Authorize / re-authorize this device. |
| `og-veil setup` | Re-run the setup wizard. |
| `og-veil serve -f` | Run blocking in the foreground (for systemd/Docker). |
| `og-veil logout` | Remove the saved session. |

## Lifecycle

- **Background by default.** Setup/login runs in the foreground, then it detaches
  and frees your terminal. Logs: `~/.opengradient/local/server.log`. Use
  `--foreground` to block instead.
- **Stays signed in.** The access token auto-refreshes. If you sign out in the
  Chat app, the next request tells you to run `og-veil login`.
- **Survives a dead node.** If the chosen TEE goes offline, it reselects another
  from the registry and retries once.

## Configuration

Session + prefs live in `~/.opengradient/local/` (override with `OG_VEIL_HOME`).

| Env var | Flag | Default | Purpose |
|---------|------|---------|---------|
| `OG_VEIL_PORT` | `--port` | `11434` | Bind port. |
| `OG_VEIL_HOST` | `--host` | `127.0.0.1` | Bind host. |
| `OG_VEIL_TEE_ID` | `--tee-id` | — | Pin a specific registry TEE. |
| `OG_VEIL_EXPECTED_PCR_HASH` | `--expected-pcr` | — | Refuse any TEE whose `pcrHash` differs. |
| `OG_VEIL_APP_URL` | `--app-url` | `https://chat.opengradient.ai` | Chat app origin for login. |

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
uv run ruff check . && uv run mypy veil
```

Protocol-level crypto is tested in the SDK repo against the real tee-gateway
recipient code, guaranteeing wire compatibility.
