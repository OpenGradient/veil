"""CLI wiring: first-run login, session reuse, background-by-default, env, models, update."""

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


def test_env_prints_env_vars():
    with mock.patch("veil.daemon.running_pid", return_value=4321):
        result = CliRunner().invoke(cli.main, ["env"])
    assert "OPENAI_BASE_URL=http://127.0.0.1:11434/v1" in result.output
    assert result.exit_code == 0


def test_models_lists_known_models():
    result = CliRunner().invoke(cli.main, ["models"])
    assert result.exit_code == 0
    # Should print at least one model name derived from the SDK's TEE_LLM enum.
    from opengradient import TEE_LLM

    sample = next(iter(TEE_LLM)).value.split("/", 1)[1]
    assert sample in result.output


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


def test_restart_stops_waits_then_starts():
    with (
        mock.patch("veil.daemon.stop_background", return_value=4321) as stop,
        mock.patch("veil.daemon.wait_until_stopped", return_value=True) as wait,
        mock.patch.object(cli, "_start_server") as start,
    ):
        result = CliRunner().invoke(cli.main, ["restart"])
    assert stop.called
    assert wait.call_args.args[0] == 4321, "should wait on the stopped pid"
    assert start.called and start.call_args.kwargs["foreground"] is False
    assert "Stopped background server (pid 4321)" in result.output
    assert result.exit_code == 0


def test_restart_starts_fresh_when_nothing_running():
    with (
        mock.patch("veil.daemon.stop_background", return_value=None),
        mock.patch("veil.daemon.wait_until_stopped") as wait,
        mock.patch.object(cli, "_start_server") as start,
    ):
        result = CliRunner().invoke(cli.main, ["restart"])
    assert not wait.called, "no running server → nothing to wait for"
    assert start.called
    assert "No background server was running" in result.output
    assert result.exit_code == 0


def test_restart_errors_if_old_process_lingers():
    with (
        mock.patch("veil.daemon.stop_background", return_value=99),
        mock.patch("veil.daemon.wait_until_stopped", return_value=False),
        mock.patch.object(cli, "_start_server") as start,
    ):
        result = CliRunner().invoke(cli.main, ["restart"])
    assert not start.called, "must not start a new server while the old one lingers"
    assert result.exit_code != 0
    assert "did not exit" in result.output
