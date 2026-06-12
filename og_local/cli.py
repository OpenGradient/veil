"""``og-local`` command-line interface.

The common path is a single command: run ``og-local`` and it walks first-time
users through setup (log in, optionally map the friendly ``opengradient.inference``
hostname), remembers the choices, then starts the local server. On later runs it
just serves. Individual steps (``setup``, ``login``, ``status``, ``logout``) are
available on their own too.
"""

from __future__ import annotations

import logging
import sys

import click

from og_local.config import DEFAULT_APP_URL, FRIENDLY_HOST, ServerConfig
from og_local.session import AuthError, Session, login, login_manual


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
# Setup wizard
# ---------------------------------------------------------------------------


def _ensure_session(app_url: str, open_browser: bool) -> Session:
    """Return a valid session, running the browser login flow if none is saved."""
    try:
        return Session.load()
    except AuthError:
        click.secho(
            "Step 1/2 — authorize this device with your OpenGradient Chat account.", fg="cyan"
        )
        session = login(app_url, open_browser=open_browser)
        click.secho(f"✓ Logged in as {session.user_email or 'unknown'}", fg="green")
        return session


def _maybe_setup_friendly_host(*, interactive: bool, assume_yes: bool, force: bool) -> None:
    """Offer to map ``opengradient.inference`` -> 127.0.0.1, remembering the choice.

    Skips silently when the mapping already exists or the user previously declined;
    only prompts interactively (or auto-accepts with ``assume_yes``). ``force``
    re-asks even if a preference was saved (used by ``og-local setup``).
    """
    from og_local.config import load_prefs, save_prefs
    from og_local.hosts import add_entry, entry_present

    if entry_present(FRIENDLY_HOST):
        return

    prefs = load_prefs()
    if "friendly_host" in prefs and not force:
        if not prefs["friendly_host"]:
            return  # previously declined
        want = True
    elif assume_yes:
        want = True
        prefs["friendly_host"] = True
        save_prefs(prefs)
    elif interactive:
        click.secho("Step 2/2 — friendly local URL (optional).", fg="cyan")
        want = click.confirm(
            f"  Map http://{FRIENDLY_HOST} -> 127.0.0.1 so agents can use a clean base URL?\n"
            "  (edits your system hosts file; you may be prompted for sudo)",
            default=True,
        )
        prefs["friendly_host"] = want
        save_prefs(prefs)
    else:
        return  # non-interactive and undecided — don't prompt or persist

    if want:
        added, message = add_entry(FRIENDLY_HOST)
        click.secho(("✓ " if added else "") + message, fg="green" if added else "yellow")


def _run_setup(
    *, app_url: str, open_browser: bool, interactive: bool, assume_yes: bool, force: bool
) -> Session:
    session = _ensure_session(app_url, open_browser)
    _maybe_setup_friendly_host(interactive=interactive, assume_yes=assume_yes, force=force)
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
    """Interactive setup wizard: log in and choose your friendly local URL."""
    try:
        _run_setup(
            app_url=app_url,
            open_browser=not no_browser,
            interactive=True,
            assume_yes=yes,
            force=True,
        )
    except AuthError as exc:
        raise click.ClickException(str(exc))

    config = ServerConfig.from_env()
    click.secho("\n✓ Setup complete.", fg="green")
    click.echo(f"  Agent base URL:  export OPENAI_BASE_URL={config.advertised_base_url()}")
    if yes or click.confirm("\nStart the local server now?", default=True):
        from og_local.server import serve as run_server

        run_server(config)


@main.command()
@click.option("--host", default=None, help="Bind host (default 127.0.0.1 / OG_LOCAL_HOST).")
@click.option("--port", type=int, default=None, help="Bind port (default 11434 / OG_LOCAL_PORT).")
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
@click.option("-y", "--yes", is_flag=True, help="Accept setup defaults (non-interactive).")
def serve(
    host: str | None,
    port: int | None,
    tee_id: str | None,
    expected_pcr: str | None,
    app_url: str,
    no_browser: bool,
    yes: bool,
) -> None:
    """Set up on first run (login + friendly URL), then run the local server."""
    from og_local.server import serve as run_server

    try:
        _run_setup(
            app_url=app_url,
            open_browser=not no_browser,
            interactive=sys.stdin.isatty(),
            assume_yes=yes,
            force=False,
        )
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
    run_server(config)


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
