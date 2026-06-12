"""CLI wiring: first-run setup, session reuse, and background-by-default serving."""

from __future__ import annotations

from unittest import mock

from click.testing import CliRunner

from og_local import cli
from og_local.session import AuthError


def test_bare_command_sets_up_then_starts_in_background():
    """`og-local` with no session: log in, then launch the server (detached by default)."""
    with (
        mock.patch.object(cli.Session, "load", side_effect=AuthError("no session")),
        mock.patch.object(cli, "login", return_value=mock.MagicMock(user_email="me@x")) as login,
        mock.patch("og_local.hosts.entry_present", return_value=True),  # host already mapped
        mock.patch("og_local.daemon.start_background", return_value=4321) as start,
        mock.patch("og_local.server.serve") as run_server,
    ):
        result = CliRunner().invoke(cli.main, [])
    assert login.called, "should auto-login when no session exists"
    assert start.called, "should launch the detached server by default"
    assert not run_server.called, "default run must not block the terminal"
    assert result.exit_code == 0


def test_serve_reuses_existing_session_and_backgrounds():
    with (
        mock.patch.object(cli.Session, "load", return_value=mock.MagicMock()),
        mock.patch.object(cli, "login") as login,
        mock.patch("og_local.hosts.entry_present", return_value=True),
        mock.patch("og_local.daemon.start_background", return_value=4321) as start,
    ):
        result = CliRunner().invoke(cli.main, ["serve"])
    assert not login.called, "should not re-login when a session exists"
    assert start.called
    assert result.exit_code == 0


def test_serve_foreground_blocks_instead_of_detaching():
    with (
        mock.patch.object(cli.Session, "load", return_value=mock.MagicMock()),
        mock.patch("og_local.hosts.entry_present", return_value=True),
        mock.patch("og_local.daemon.start_background") as start,
        mock.patch("og_local.server.serve") as run_server,
    ):
        result = CliRunner().invoke(cli.main, ["serve", "--foreground"])
    assert run_server.called, "--foreground should run the blocking server"
    assert not start.called
    assert result.exit_code == 0


def test_setup_wizard_maps_host_with_yes_then_starts_background():
    with (
        mock.patch.object(cli.Session, "load", return_value=mock.MagicMock()),
        mock.patch("og_local.hosts.entry_present", return_value=False),
        mock.patch("og_local.hosts.add_entry", return_value=(True, "mapped")) as add_entry,
        mock.patch("og_local.config.save_prefs"),
        mock.patch("og_local.config.load_prefs", return_value={}),
        mock.patch("og_local.daemon.start_background", return_value=4321) as start,
    ):
        result = CliRunner().invoke(cli.main, ["setup", "--yes"])
    assert add_entry.called, "wizard with --yes should map the friendly host"
    assert start.called, "wizard with --yes should start the server"
    assert result.exit_code == 0


def test_serve_non_interactive_does_not_prompt_or_map():
    """In a non-tty (no --yes, no saved pref), the host step is skipped, not blocking."""
    with (
        mock.patch.object(cli.Session, "load", return_value=mock.MagicMock()),
        mock.patch("og_local.hosts.entry_present", return_value=False),
        mock.patch("og_local.config.load_prefs", return_value={}),
        mock.patch("og_local.hosts.add_entry") as add_entry,
        mock.patch("og_local.daemon.start_background", return_value=4321),
    ):
        result = CliRunner().invoke(cli.main, ["serve"])
    assert not add_entry.called, "must not edit hosts file without consent"
    assert result.exit_code == 0
