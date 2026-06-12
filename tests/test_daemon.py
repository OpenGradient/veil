"""Background-mode (daemon) behavior and its CLI wiring."""

from __future__ import annotations

from unittest import mock

import pytest
from click.testing import CliRunner

from veil import cli, daemon


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OG_VEIL_HOME", str(tmp_path))
    return tmp_path


def test_start_writes_pidfile_and_stop_removes_it(home):
    fake_proc = mock.MagicMock(pid=4321)
    with mock.patch("subprocess.Popen", return_value=fake_proc) as popen:
        pid = daemon.start_background(["--port", "11500"])
    assert pid == 4321
    assert daemon.pid_path().read_text().strip() == "4321"
    # The child is launched detached, skipping the setup wizard.
    cmd = popen.call_args.args[0]
    assert "serve" in cmd and "--skip-setup" in cmd and "11500" in cmd
    assert popen.call_args.kwargs["start_new_session"] is True

    with mock.patch("os.kill"):  # alive check + SIGTERM both succeed
        assert daemon.running_pid() == 4321
        assert daemon.stop_background() == 4321
    assert not daemon.pid_path().exists()


def test_running_pid_clears_stale_pidfile(home):
    daemon.pid_path().write_text("999999")
    with mock.patch("os.kill", side_effect=OSError):  # process not found
        assert daemon.running_pid() is None
    assert not daemon.pid_path().exists()


def test_start_refuses_when_already_running(home):
    daemon.pid_path().write_text("4321")
    with mock.patch("os.kill"):  # pretend 4321 is alive
        with pytest.raises(RuntimeError, match="already running"):
            daemon.start_background([])


def test_serve_spawns_detached_by_default(home):
    with (
        mock.patch.object(cli.Session, "load", return_value=mock.MagicMock()),
        mock.patch("veil.daemon.start_background", return_value=4321) as start,
        mock.patch("veil.server.serve") as run_server,
    ):
        result = CliRunner().invoke(cli.main, ["serve"])
    assert start.called, "should launch the detached server by default"
    assert not run_server.called, "must not run the blocking server in the foreground"
    assert result.exit_code == 0


def test_stop_command(home):
    with mock.patch("veil.daemon.stop_background", return_value=4321):
        result = CliRunner().invoke(cli.main, ["stop"])
    assert "4321" in result.output
    assert result.exit_code == 0
