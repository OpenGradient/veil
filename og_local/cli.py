"""``og-local`` command-line interface."""

from __future__ import annotations

import logging
import sys

import click

from og_local.config import DEFAULT_APP_URL, ServerConfig
from og_local.session import AuthError, Session, login, login_manual


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def main(verbose: bool) -> None:
    """OpenGradient Local — drop-in, self-verifying private inference for AI agents."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@main.command()
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


# Register under the name `login` (the function name avoids shadowing the import).
main.add_command(login_cmd, name="login")


@main.command()
@click.option("--host", default=None, help="Bind host (default 127.0.0.1 / OG_LOCAL_HOST).")
@click.option("--port", type=int, default=None, help="Bind port (default 11434 / OG_LOCAL_PORT).")
@click.option("--tee-id", default=None, help="Pin a specific tee_id from the registry.")
@click.option(
    "--expected-pcr", default=None, help="Refuse any TEE whose registry pcrHash differs from this."
)
def serve(host: str | None, port: int | None, tee_id: str | None, expected_pcr: str | None) -> None:
    """Run the local OpenAI-compatible server."""
    from og_local.server import serve as run_server

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
    run_server(config)


@main.command()
def status() -> None:
    """Show the current login + network configuration."""
    try:
        session = Session.load()
    except AuthError as exc:
        raise click.ClickException(str(exc))
    cfg = session.config
    click.echo(f"Signed in as : {session.user_email or 'unknown'}")
    click.echo(f"Environment  : {cfg.app_env}")
    click.echo(f"Relay (chat) : {cfg.chat_api_base_url}")
    click.echo(
        f"TEE registry : {cfg.tee_registry_address or '(none)'} @ {cfg.tee_registry_rpc_url or '(none)'}"
    )


@main.command(name="setup-host")
def setup_host() -> None:
    """Map http://opengradient.inference to your local server (edits the hosts file)."""
    from og_local.config import FRIENDLY_HOST
    from og_local.hosts import add_entry

    added, message = add_entry(FRIENDLY_HOST)
    click.secho(("✓ " if added else "") + message, fg="green" if added else "yellow")
    if added:
        click.echo(
            "Agents can now use:  export OPENAI_BASE_URL=http://opengradient.inference:11434/v1"
        )


@main.command()
def logout() -> None:
    """Remove the saved session."""
    from og_local.config import session_path

    path = session_path()
    if path.exists():
        path.unlink()
        click.echo("Logged out.")
    else:
        click.echo("No saved session.")


if __name__ == "__main__":
    main()
