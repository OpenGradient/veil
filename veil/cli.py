"""``og-veil`` command-line interface.

The common path is a single command: run ``og-veil`` and it logs you in on first
use, then starts the local server in the background. Individual steps (``serve``,
``login``, ``stop``, ``status``, ``endpoint``, ``test``, ``update``, ``logout``)
are available on their own too.
"""

from __future__ import annotations

import logging
import sys

import click

from veil.config import DEFAULT_APP_URL, ServerConfig
from veil.session import AuthError, Session, login, login_manual


@click.group(invoke_without_command=True)
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """OpenGradient Local — drop-in, self-verifying private inference for AI agents.

    Run with no command to do everything: set up on first run, then start the server.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if ctx.invoked_subcommand is None:
        ctx.invoke(serve)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def _ensure_session(app_url: str, open_browser: bool) -> Session:
    """Return a valid session, running the browser login flow if none is saved."""
    try:
        return Session.load()
    except AuthError:
        click.secho("Authorizing this device with your OpenGradient Chat account…", fg="cyan")
        session = login(app_url, open_browser=open_browser)
        click.secho(f"✓ Logged in as {session.user_email or 'unknown'}", fg="green")
        return session


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@main.command()
@click.option("--app-url", default=DEFAULT_APP_URL, show_default=True, help="Chat app web origin.")
@click.option(
    "--no-browser", is_flag=True, help="During login, print the URL instead of opening a browser."
)
@click.option("-y", "--yes", is_flag=True, help="Accept defaults (non-interactive).")
def setup(app_url: str, no_browser: bool, yes: bool) -> None:
    """Log in, then optionally start the server."""
    try:
        _ensure_session(app_url, open_browser=not no_browser)
    except AuthError as exc:
        raise click.ClickException(str(exc))

    config = ServerConfig.from_env()
    click.secho("\n✓ Setup complete.", fg="green")
    click.echo(f"  Agent base URL:  export OPENAI_BASE_URL={config.advertised_base_url()}")
    if yes or click.confirm("\nStart the local server now (in the background)?", default=True):
        _start_server(config, foreground=False)


@main.command()
@click.option("--host", default=None, help="Bind host (default 127.0.0.1 / OG_VEIL_HOST).")
@click.option("--port", type=int, default=None, help="Bind port (default 11434 / OG_VEIL_PORT).")
@click.option("--tee-id", default=None, help="Pin a specific tee_id from the registry.")
@click.option(
    "--expected-pcr", default=None, help="Refuse any TEE whose registry pcrHash differs from this."
)
@click.option(
    "--app-url", default=DEFAULT_APP_URL, show_default=True, help="Chat app web origin for login."
)
@click.option(
    "--no-browser", is_flag=True, help="During login, print the URL instead of opening a browser."
)
@click.option(
    "-f",
    "--foreground",
    is_flag=True,
    help="Run in the foreground (blocking) instead of detaching. Use for systemd/containers.",
)
@click.option(
    "--skip-setup",
    is_flag=True,
    hidden=True,
    help="Internal: skip login (used by the detached child).",
)
def serve(
    host: str | None,
    port: int | None,
    tee_id: str | None,
    expected_pcr: str | None,
    app_url: str,
    no_browser: bool,
    foreground: bool,
    skip_setup: bool,
) -> None:
    """Log in on first run, then run the local server (detached by default)."""
    if not skip_setup:
        try:
            _ensure_session(app_url, open_browser=not no_browser)
        except AuthError as exc:
            raise click.ClickException(str(exc))

    config = ServerConfig.from_env()
    if host:
        config.host = host
    if port:
        config.port = port
    if tee_id:
        config.pinned_tee_id = tee_id if tee_id.startswith("0x") else "0x" + tee_id
    if expected_pcr:
        config.expected_pcr_hash = (
            expected_pcr if expected_pcr.startswith("0x") else "0x" + expected_pcr
        ).lower()

    # The detached child (--skip-setup) always runs the server in-process.
    _start_server(config, foreground=foreground or skip_setup)


def _config_flags(config: ServerConfig) -> list[str]:
    flags = ["--host", config.host, "--port", str(config.port)]
    if config.pinned_tee_id:
        flags += ["--tee-id", config.pinned_tee_id]
    if config.expected_pcr_hash:
        flags += ["--expected-pcr", config.expected_pcr_hash]
    return flags


def _start_server(config: ServerConfig, *, foreground: bool) -> None:
    """Run the server: detached in the background by default, or blocking with foreground=True."""
    if foreground:
        from veil.server import serve as run_server

        run_server(config)
        return

    from veil.daemon import log_path, running_pid, start_background

    try:
        pid = start_background(_config_flags(config))
    except RuntimeError as exc:
        # Already running — surface that clearly rather than erroring out.
        existing = running_pid()
        if existing:
            click.secho(f"OpenGradient Local is already running (pid {existing}).", fg="yellow")
            click.echo("  Stop it with: og-veil stop")
            return
        raise click.ClickException(str(exc))
    click.secho(f"✓ OpenGradient Local running in the background (pid {pid}).", fg="green")
    click.echo(f"  Base URL : {config.advertised_base_url()}")
    click.echo(f"  Logs     : {log_path()}")
    click.echo("  Stop     : og-veil stop")


@main.command()
def stop() -> None:
    """Stop the background server."""
    from veil.daemon import stop_background

    pid = stop_background()
    if pid is None:
        click.echo("No background server is running.")
    else:
        click.secho(f"✓ Stopped background server (pid {pid}).", fg="green")


@main.command()
def endpoint() -> None:
    """Print the env vars to point your agent at OpenGradient Local."""
    from veil.daemon import running_pid

    config = ServerConfig.from_env()
    click.echo("Point your agent at OpenGradient Local (one env var change):")
    click.secho(f"  export OPENAI_BASE_URL={config.advertised_base_url()}", bold=True)
    click.echo("  export OPENAI_API_KEY=og-veil   # ignored; your Chat session authenticates")
    if running_pid() is None:
        click.echo("\nThe server isn't running yet — start it with `og-veil`.")


@main.command(name="login")
@click.option("--app-url", default=DEFAULT_APP_URL, show_default=True, help="Chat app web origin.")
@click.option("--no-browser", is_flag=True, help="Don't open a browser; print the URL instead.")
@click.option(
    "--manual",
    is_flag=True,
    help="Paste the CLI-auth token instead of using the loopback callback.",
)
def login_cmd(app_url: str, no_browser: bool, manual: bool) -> None:
    """Authorize this device using your OpenGradient Chat account."""
    try:
        if manual:
            click.echo("Paste the cli-auth token JSON, then press Ctrl-D:")
            session = login_manual(sys.stdin.read())
        else:
            session = login(app_url, open_browser=not no_browser)
    except AuthError as exc:
        raise click.ClickException(str(exc))
    click.secho(f"✓ Logged in as {session.user_email or 'unknown'}", fg="green")


@main.command(name="test")
@click.argument("prompt", nargs=-1)
@click.option("--model", default="gpt-4.1", show_default=True, help="Model to send the prompt to.")
def test_cmd(prompt: tuple[str, ...], model: str) -> None:
    """Send a one-off PROMPT to the running local server and print the reply.

    Posts to the localhost OpenAI-compatible endpoint — the same one your agent
    uses — so the background server must already be running (start it with
    ``og-veil``). The reply is verified TEE output.
    """
    import requests

    text = " ".join(prompt).strip() or "Say hello from a verified TEE in one short sentence."

    config = ServerConfig.from_env()
    base_url = config.advertised_base_url()
    body = {"model": model, "messages": [{"role": "user", "content": text}]}
    try:
        resp = requests.post(f"{base_url}/chat/completions", json=body, timeout=120)
    except requests.exceptions.RequestException as exc:
        raise click.ClickException(
            f"could not reach the local server at {base_url} ({type(exc).__name__}) "
            "— is it running? start it with `og-veil`"
        )

    if resp.status_code != 200:
        message = resp.text
        try:
            message = resp.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            pass
        raise click.ClickException(f"server returned {resp.status_code}: {message}")

    data = resp.json()
    content = ""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = ""

    click.secho(f"> {text}", fg="cyan")
    click.echo(content or "(empty response)")
    verification = data.get("opengradient_verification") or {}
    tee_id = verification.get("tee_id") or resp.headers.get("X-OpenGradient-TEE-Id", "?")
    click.secho(f"\n✓ verified — tee_id={tee_id}", fg="green")


@main.command()
def status() -> None:
    """Show the current login + network configuration."""
    try:
        session = Session.load()
    except AuthError as exc:
        raise click.ClickException(str(exc))
    from veil.daemon import running_pid

    from veil import __version__

    cfg = session.config
    click.echo(f"Version      : {__version__}")
    click.echo(f"Signed in as : {session.user_email or 'unknown'}")
    click.echo(f"Environment  : {cfg.app_env}")
    click.echo(f"Relay (chat) : {cfg.chat_api_base_url}")
    click.echo(
        f"TEE registry : {cfg.tee_registry_address or '(none)'} @ {cfg.tee_registry_rpc_url or '(none)'}"
    )
    pid = running_pid()
    click.echo(f"Background   : running (pid {pid})" if pid else "Background   : not running")


def _update_command() -> list[str]:
    """Pick the right upgrade command based on how this CLI was installed."""
    import shutil

    pkg = "opengradient-veil"
    location = (__file__ or "").replace("\\", "/")
    if "/uv/tools/" in location and shutil.which("uv"):
        return ["uv", "tool", "upgrade", pkg]
    if "/pipx/" in location and shutil.which("pipx"):
        return ["pipx", "upgrade", pkg]
    return [sys.executable, "-m", "pip", "install", "--upgrade", pkg]


@main.command()
def update() -> None:
    """Update og-veil to the latest version from PyPI."""
    import subprocess

    cmd = _update_command()
    click.echo(f"Updating via: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise click.ClickException(
            f"update failed: {exc}\nTry manually, e.g.:  uv tool upgrade opengradient-veil"
        )
    click.secho("✓ Updated. Restart the server to pick it up:  og-veil stop && og-veil", fg="green")


@main.command()
def logout() -> None:
    """Remove the saved session."""
    from veil.config import session_path

    path = session_path()
    if path.exists():
        path.unlink()
        click.echo("Logged out.")
    else:
        click.echo("No saved session.")


if __name__ == "__main__":
    main()
