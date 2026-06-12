"""CLI wiring: first-run setup wizard, session reuse, and the one-command path."""

from __future__ import annotations

from unittest import mock

from click.testing import CliRunner

from og_local import cli
from og_local.session import AuthError


def test_bare_command_sets_up_then_serves():
    """`og-local` with no session should log in, then serve."""
    with (
        mock.patch.object(cli.Session, "load", side_effect=AuthError("no session")),
        mock.patch.object(cli, "login", return_value=mock.MagicMock(user_email="me@x")) as login,
        mock.patch("og_local.hosts.entry_present", return_value=True),  # host already mapped
        mock.patch("og_local.server.serve") as serve,
    ):
        result = CliRunner().invoke(cli.main, [])
    assert login.called, "should auto-login when no session exists"
    assert serve.called, "should start the server after setup"
    assert result.exit_code == 0


def test_serve_reuses_existing_session():
    with (
        mock.patch.object(cli.Session, "load", return_value=mock.MagicMock()),
        mock.patch.object(cli, "login") as login,
        mock.patch("og_local.hosts.entry_present", return_value=True),
        mock.patch("og_local.server.serve") as serve,
    ):
        result = CliRunner().invoke(cli.main, ["serve"])
    assert not login.called, "should not re-login when a session exists"
    assert serve.called
    assert result.exit_code == 0


def test_setup_wizard_maps_host_with_yes_then_starts():
    with (
        mock.patch.object(cli.Session, "load", return_value=mock.MagicMock()),
        mock.patch("og_local.hosts.entry_present", return_value=False),
        mock.patch("og_local.hosts.add_entry", return_value=(True, "mapped")) as add_entry,
        mock.patch("og_local.config.save_prefs"),
        mock.patch("og_local.config.load_prefs", return_value={}),
        mock.patch("og_local.server.serve") as serve,
    ):
        result = CliRunner().invoke(cli.main, ["setup", "--yes"])
    assert add_entry.called, "wizard with --yes should map the friendly host"
    assert serve.called, "wizard with --yes should start the server"
    assert result.exit_code == 0


def test_serve_non_interactive_does_not_prompt_or_map():
    """In a non-tty (no --yes, no saved pref), the host step is skipped, not blocking."""
    with (
        mock.patch.object(cli.Session, "load", return_value=mock.MagicMock()),
        mock.patch("og_local.hosts.entry_present", return_value=False),
        mock.patch("og_local.config.load_prefs", return_value={}),
        mock.patch("og_local.hosts.add_entry") as add_entry,
        mock.patch("og_local.server.serve") as serve,
    ):
        # CliRunner stdin is not a tty, so serve() treats it as non-interactive.
        result = CliRunner().invoke(cli.main, ["serve"])
    assert not add_entry.called, "must not edit hosts file without consent"
    assert serve.called
    assert result.exit_code == 0
