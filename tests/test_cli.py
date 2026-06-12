"""CLI wiring: first-run login, session reuse, background-by-default, endpoint, update."""

from __future__ import annotations

from unittest import mock

from click.testing import CliRunner

from veil import cli
from veil.session import AuthError


def test_bare_command_logs_in_then_starts_in_background():
    with (
        mock.patch.object(cli.Session, "load", side_effect=AuthError("no session")),
        mock.patch.object(cli, "login", return_value=mock.MagicMock(user_email="me@x")) as login,
        mock.patch("veil.daemon.start_background", return_value=4321) as start,
        mock.patch("veil.server.serve") as run_server,
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
        mock.patch("veil.daemon.start_background", return_value=4321) as start,
    ):
        result = CliRunner().invoke(cli.main, ["serve"])
    assert not login.called, "should not re-login when a session exists"
    assert start.called
    assert result.exit_code == 0


def test_serve_foreground_blocks_instead_of_detaching():
    with (
        mock.patch.object(cli.Session, "load", return_value=mock.MagicMock()),
        mock.patch("veil.daemon.start_background") as start,
        mock.patch("veil.server.serve") as run_server,
    ):
        result = CliRunner().invoke(cli.main, ["serve", "--foreground"])
    assert run_server.called, "--foreground should run the blocking server"
    assert not start.called
    assert result.exit_code == 0


def test_setup_with_yes_starts_background():
    with (
        mock.patch.object(cli.Session, "load", return_value=mock.MagicMock()),
        mock.patch("veil.daemon.start_background", return_value=4321) as start,
    ):
        result = CliRunner().invoke(cli.main, ["setup", "--yes"])
    assert start.called
    assert result.exit_code == 0


def test_endpoint_prints_env_vars():
    with mock.patch("veil.daemon.running_pid", return_value=4321):
        result = CliRunner().invoke(cli.main, ["endpoint"])
    assert "OPENAI_BASE_URL=http://127.0.0.1:11434/v1" in result.output
    assert result.exit_code == 0


def test_test_command_posts_prompt_to_localhost_and_prints_reply():
    resp = mock.MagicMock(
        status_code=200,
        headers={"X-OpenGradient-TEE-Id": "0xabc"},
    )
    resp.json.return_value = {
        "choices": [{"message": {"content": "hello from the TEE"}}],
        "opengradient_verification": {"tee_id": "0xabc"},
    }
    with mock.patch("requests.post", return_value=resp) as post:
        out = CliRunner().invoke(cli.main, ["test", "ping", "the", "enclave"])
    assert out.exit_code == 0
    # Posts to the localhost OpenAI-compatible endpoint with the joined prompt.
    url = post.call_args.args[0]
    assert url == "http://127.0.0.1:11434/v1/chat/completions"
    body = post.call_args.kwargs["json"]
    assert body["messages"][0]["content"] == "ping the enclave"
    assert "hello from the TEE" in out.output
    assert "0xabc" in out.output


def test_test_command_reports_server_not_running():
    import requests

    with mock.patch("requests.post", side_effect=requests.exceptions.ConnectionError()):
        out = CliRunner().invoke(cli.main, ["test", "hi"])
    assert out.exit_code != 0
    assert "is it running" in out.output


def test_test_command_surfaces_server_error():
    resp = mock.MagicMock(status_code=503)
    resp.json.return_value = {"error": {"message": "no usable TEE"}}
    with mock.patch("requests.post", return_value=resp):
        out = CliRunner().invoke(cli.main, ["test", "hi"])
    assert out.exit_code != 0
    assert "no usable TEE" in out.output


def test_update_runs_upgrade_command():
    with mock.patch("subprocess.run") as run:
        result = CliRunner().invoke(cli.main, ["update"])
    assert run.called
    assert run.call_args.args[0][-1] == "opengradient-veil"
    assert result.exit_code == 0


def test_update_surfaces_failure():
    import subprocess

    with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "x")):
        result = CliRunner().invoke(cli.main, ["update"])
    assert result.exit_code != 0
    assert "update failed" in result.output
