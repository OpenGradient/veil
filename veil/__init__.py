"""OpenGradient Veil — self-verifying private-inference proxy for AI agents.

Point any OpenAI-compatible SDK at this process (one env var) and prompts are
routed through OpenGradient's decentralized network of attestable AWS Nitro TEE
gateways. Every response is cryptographically verified against the enclave's
attested signing key *before* a single token is handed back to the agent, so the
agent trusts math against an open network — not us, the host, or the relay.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    # Single source of truth is pyproject.toml; read the installed dist metadata
    # so __version__ never drifts from the packaged version.
    __version__ = _pkg_version("opengradient-veil")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"
