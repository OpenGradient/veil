"""OpenGradient Local — self-verifying private-inference proxy for AI agents.

Point any OpenAI-compatible SDK at this process (one env var) and prompts are
routed through OpenGradient's decentralized network of attestable AWS Nitro TEE
gateways. Every response is cryptographically verified against the enclave's
attested signing key *before* a single token is handed back to the agent, so the
agent trusts math against an open network — not us, the host, or the relay.
"""

__version__ = "0.1.0"
