"""PID-file lock at startup — prevent concurrent bot instances."""
from __future__ import annotations

import os

import pytest

from polybot import cli as cli_module


def test_acquire_writes_pid(tmp_path, monkeypatch):
    pid_file = tmp_path / "bot.pid"
    monkeypatch.setattr(cli_module, "PID_FILE", pid_file)

    cli_module._acquire_instance_lock()

    assert pid_file.read_text().strip() == str(os.getpid())


def test_acquire_refuses_when_other_process_alive(tmp_path, monkeypatch):
    pid_file = tmp_path / "bot.pid"
    monkeypatch.setattr(cli_module, "PID_FILE", pid_file)

    # Stash a PID that always reports alive — os.kill(pid, 0) returns cleanly.
    fake_pid = 999_999
    pid_file.write_text(str(fake_pid))

    def fake_kill(pid: int, sig: int) -> None:
        assert pid == fake_pid and sig == 0  # liveness probe
        return None  # success ⇒ alive

    monkeypatch.setattr(cli_module.os, "kill", fake_kill)

    with pytest.raises(SystemExit, match=f"pid={fake_pid}"):
        cli_module._acquire_instance_lock()

    # File untouched — the live instance keeps its own PID.
    assert pid_file.read_text().strip() == str(fake_pid)


def test_acquire_overwrites_stale_pid_file(tmp_path, monkeypatch):
    pid_file = tmp_path / "bot.pid"
    monkeypatch.setattr(cli_module, "PID_FILE", pid_file)

    stale_pid = 777_777
    pid_file.write_text(str(stale_pid))

    def dead(pid: int, sig: int) -> None:
        raise ProcessLookupError()

    monkeypatch.setattr(cli_module.os, "kill", dead)

    cli_module._acquire_instance_lock()
    assert pid_file.read_text().strip() == str(os.getpid())


def test_release_removes_own_pid_file(tmp_path, monkeypatch):
    pid_file = tmp_path / "bot.pid"
    monkeypatch.setattr(cli_module, "PID_FILE", pid_file)
    pid_file.write_text(str(os.getpid()))

    cli_module._release_instance_lock()
    assert not pid_file.exists()


def test_release_leaves_other_pid_alone(tmp_path, monkeypatch):
    """If the PID file points to a different process (the live instance), don't delete it."""
    pid_file = tmp_path / "bot.pid"
    monkeypatch.setattr(cli_module, "PID_FILE", pid_file)
    pid_file.write_text("12345")  # not us

    cli_module._release_instance_lock()
    assert pid_file.exists() and pid_file.read_text().strip() == "12345"
