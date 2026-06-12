# OpenGradient Veil

**Drop-in confidential, self-verifying inference for AI agents.**

Point any OpenAI SDK at `og-veil` with one env var. Your prompts are encrypted via Oblivious HTTP
end-to-end to an attested TEE enclave, and every response is cryptographically
verified before it reaches your agent, keeping every prompt private and verifiable. You trust math - not us, the host, or the
network. Your agent's code doesn't change.

- **Private & unlinkable** - Oblivious HTTP splits *who you are* from *what you
  ask* across two parties that never share both. The relay sees your identity (IP & account) but only ciphertext - never your prompt. The enclave sees your prompt
  but only the relay's IP - never you. So no one, including OpenGradient, can tie
  a user to a prompt (unless the relay and enclave collude).
- **Verified** - each response is signed *inside* the enclave and checked on your
  machine, proving it ran in known, reproducible code and wasn't tampered with.
  Nothing unverified ever reaches your agent.

## Quickstart

**Requirements:** a [chat.opengradient.ai](https://chat.opengradient.ai)
account - the first run logs you in through it, and the relay bills inference
against that account (no wallet or key lives here). Prompts run on OpenGradient's
decentralized network of attested TEE gateways and OHTTP proxy; `og-veil` just discovers,
encrypts to, and verifies them locally.

```sh
# install (needs Python 3.11+; uv grabs one for you)
uv tool install opengradient-veil        # or: pipx install opengradient-veil

# run — logs you in (browser) the first time, then serves in the background
og-veil

# check it end-to-end — sends a one-off prompt through the verified TEE path
og-veil test "Explain TEE attestation in one line."
```

`og-veil test` posts to the same local endpoint your agent uses and prints the
reply plus its `tee_id`, so it's the quickest way to confirm the whole path
(login → encrypt → enclave → verify) works before wiring up your agent.

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

Useful commands: `og-veil test` (send a one-off prompt to check the path),
`og-veil stop`, `og-veil status`, `og-veil env` (re-prints the env vars),
`og-veil models` (list available models), `og-veil update`, `og-veil logout`.

### Use it with Hermes Agent

[Hermes Agent](https://hermes-agent.nousresearch.com) speaks OpenAI out of the
box, so pointing it at `og-veil` routes every call through the verified TEE path.
With `og-veil` running, set a custom endpoint — either via the CLI:

```sh
hermes config set model.provider custom
hermes config set model.base_url http://127.0.0.1:11434/v1
hermes config set OPENAI_API_KEY og-veil      # ignored; your Chat login authenticates
hermes config set model.default claude-sonnet-4-6
```

…or by editing `~/.hermes/config.yaml` directly:

```yaml
model:
  default: claude-sonnet-4-6
  provider: custom
  base_url: http://127.0.0.1:11434/v1
  api_key: og-veil
```

Now `hermes` runs against attested, end-to-end-encrypted inference with no other
changes. Confirm it's flowing through the enclave with `og-veil status`.

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
| `og-veil test ["prompt"]` | Send a one-off prompt to the running server and print the verified reply. |
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
| `OG_VEIL_PII_SCRUB` | `--pii-scrub` | off | Redact high-impact PII from prompts locally before they leave the machine. |

### Local PII redaction (opt-in)

Veil's privacy guarantee is *unlinkability* — OHTTP splits *who you are* from
*what you ask*, so the model provider sees your prompt but believes it came from
the enclave, not you. That holds only if the prompt content doesn't re-identify
you. Local PII redaction strips the **concrete, unambiguous identifiers** — when
enabled, they're irreversibly replaced with `[REDACTED_*]` tags *before* the
prompt is encrypted to the TEE, so they never leave your machine. It's a
peace-of-mind backstop for the hard data, not a replacement for your own
discretion.

Detection is delegated to **Microsoft Presidio** (community-maintained
recognizers) rather than handrolled patterns, so it ships as an optional extra.
Install it once:

```sh
pip install 'opengradient-veil[pii]'
# fetch the spaCy model from the release wheel (avoids issues with `spacy download`
# in some environments):
pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
```

(`make install-pii` does both.) Then enable it:

```sh
og-veil --pii-scrub        # or: export OG_VEIL_PII_SCRUB=1
```

What gets redacted (each replaced with a `[REDACTED_*]` tag):

- **email, phone numbers** — contact identity.
- **US SSN, bank numbers** — credit cards (Luhn), IBANs (mod-97), and US
  bank/routing numbers, via Presidio's regex/checksum recognizers.
- **street addresses** — a deterministic street-line recognizer.

**Names, free-form locations (cities/countries), and dates are deliberately left
in.** Those rely on statistical NER that over-redacts the third-party names real
prompts are full of ("reply to *Advait* about *Julia*") and often mislabels
uncommon names — wrecking the prompt for little gain. Everything redacted here is
pattern/checksum-based: deterministic, no name guessing. (The small spaCy model
is just Presidio's tokenizer; no NER entities are redacted.)

If `--pii-scrub` is set but the extra/model isn't installed, the server refuses
to start with an actionable message rather than silently sending PII.

Redaction is **irreversible** — there's no de-anonymization step, so the TEE's
signed `output_hash` covers exactly what it ran. It's risk-reduction, not a
guarantee: because names and free-form text are left in, you stay responsible for
what you choose to disclose, and residual signals (writing style, niche topics)
can still re-identify you.

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
