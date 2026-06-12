"""CLI wiring: the one-command path logs in (when needed) then serves."""

from __future__ import annotations

from unittest import mock

from click.testing import CliRunner

from og_local import cli
from og_local.session import AuthError


def test_bare_command_logs_in_then_serves():
    """`og-local` with no subcommand should reach the serve flow."""
    with (
        mock.patch.object(cli.Session, "load", side_effect=AuthError("no session")),
        mock.patch.object(cli, "login") as login,
        mock.patch("og_local.server.serve") as serve,
    ):
        result = CliRunner().invoke(cli.main, [])
    assert login.called, "should auto-login when no session exists"
    assert serve.called, "should start the server after login"
    assert result.exit_code == 0


def test_serve_skips_login_when_session_exists():
    with (
        mock.patch.object(cli.Session, "load", return_value=mock.MagicMock()),
        mock.patch.object(cli, "login") as login,
        mock.patch("og_local.server.serve") as serve,
    ):
        result = CliRunner().invoke(cli.main, ["serve"])
    assert not login.called, "should not log in again when a session exists"
    assert serve.called
    assert result.exit_code == 0


def test_serve_setup_host_flag_maps_hostname():
    with (
        mock.patch.object(cli.Session, "load", return_value=mock.MagicMock()),
        mock.patch("og_local.hosts.add_entry", return_value=(True, "mapped")) as add_entry,
        mock.patch("og_local.server.serve"),
    ):
        result = CliRunner().invoke(cli.main, ["serve", "--setup-host"])
    assert add_entry.called
    assert result.exit_code == 0
