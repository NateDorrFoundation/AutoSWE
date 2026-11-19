"""Tests for autoswe.harness.backends._proc_kill — the shared process-tree kill.

The kill-tree semantics (issue #327) live in ONE module now (pi.py and
codex.py import it; antigravity.py on the antigravity branch should import it
too when that branch merges), so the helper-level tests live here once.  The
per-backend wiring (spawn flag + timeout path actually calling kill_tree)
stays pinned in test_pi_backend.py / test_codex_backend.py.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from unittest.mock import Mock

import pytest

import autoswe.harness.backends._proc_kill as _proc_kill
from autoswe.harness.backends._proc_kill import KILL_GRACE_S, kill_tree


class _StubbornProcess:
    """A process that ignores every signal (its wait() never returns)."""

    pid = 4321
    killed = False

    def kill(self):
        self.killed = True

    async def wait(self):
        await asyncio.sleep(999)  # never exits on its own


# ---------- POSIX process-group path ----------


@pytest.mark.skipif(os.name == "nt", reason="POSIX killpg path (Windows covered by the taskkill tests)")
def test_kill_tree_sigterm_then_sigkill(monkeypatch):
    """kill_tree escalates SIGTERM → SIGKILL after the grace when the group
    ignores SIGTERM (issue #327)."""
    signals: list[int] = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append(sig))

    proc = _StubbornProcess()
    asyncio.run(kill_tree(proc, grace=0.05))
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert proc.killed is False  # the direct kill is not the POSIX mechanism


@pytest.mark.skipif(os.name == "nt", reason="POSIX killpg path (Windows covered by the taskkill tests)")
def test_kill_tree_falls_back_when_not_group_leader(monkeypatch):
    """kill_tree must NOT killpg a group it does not lead: with
    pgid != pid the group kill would hit our own process group, so it falls
    back to the direct-child kill (defensive guard, issue #327)."""
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1)  # not the leader
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    class _Child:
        pid = 4321
        killed = False

        def kill(self):
            self.killed = True

        async def wait(self):
            return 0

    proc = _Child()
    asyncio.run(kill_tree(proc, grace=0.05))
    assert killpg_calls == []
    assert proc.killed is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics (issue #327)")
def test_kill_tree_kills_real_child_process():
    """Real process tree: a grandchild (``sleep``) spawned by the direct
    child dies with the group kill.  Before the fix (bare ``process.kill()``)
    the grandchild survived as an orphan."""
    async def _go():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import subprocess, time\n"
            "child = subprocess.Popen(['sleep', '61'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(61)\n",
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        child_pid = None
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
            child_pid = int(line.strip())
            await kill_tree(proc, grace=1.0)
            rc = await asyncio.wait_for(proc.wait(), timeout=5)
            assert rc is not None and rc < 0, f"direct child should die by signal, got {rc}"
            # The grandchild must be gone too: poll until ESRCH.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("orphaned grandchild survived the process-group kill")
        finally:
            for pid in (proc.pid, child_pid):
                if pid is None:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    asyncio.run(_go())


# ---------- Windows taskkill path ----------
#
# Cross-platform: os.name and subprocess.run are patched, so the Windows
# branch runs on POSIX CI hosts too.


class _ExitableProcess:
    """Windows-path stand-in: wait() returns at once (taskkill worked)."""

    pid = 4567
    killed = False

    def kill(self):
        self.killed = True

    async def wait(self):
        return -9


class _SurvivorProcess:
    """Windows-path stand-in: taskkill "ran" but the child stays alive."""

    pid = 4567
    killed = False

    def kill(self):
        self.killed = True

    async def wait(self):
        await asyncio.sleep(999)  # survives the taskkill


def _patch_nt(monkeypatch):
    monkeypatch.setattr(_proc_kill.os, "name", "nt")


def test_kill_tree_windows_taskkill(monkeypatch):
    """Happy path: taskkill /T /F walks the child tree, reaching the node
    child under the ``cmd /c`` shim; no bare process.kill() needed."""
    runs: list[list] = []
    _patch_nt(monkeypatch)
    monkeypatch.setattr(
        _proc_kill.subprocess, "run",
        lambda cmd, **kw: runs.append(cmd) or Mock(returncode=0),
    )

    proc = _ExitableProcess()
    asyncio.run(kill_tree(proc))
    assert runs == [["taskkill", "/T", "/F", "/PID", "4567"]]
    assert proc.killed is False  # the tree-kill landed; no direct kill


def test_kill_tree_windows_taskkill_ineffective_falls_back(monkeypatch):
    """Review finding (M2): taskkill can run and still leave the child alive
    (access denied, race).  After the grace, the direct child must still be
    killed — the caller's un-timed ``await process.wait()`` may not hang."""
    _patch_nt(monkeypatch)
    monkeypatch.setattr(
        _proc_kill.subprocess, "run",
        lambda cmd, **kw: Mock(returncode=128),  # taskkill ran, failed
    )

    proc = _SurvivorProcess()
    asyncio.run(kill_tree(proc, grace=0.05))
    assert proc.killed is True  # direct kill rescued the hung wait()


def test_kill_tree_windows_taskkill_unavailable_falls_back(monkeypatch):
    """Review finding (M2), unavailable variant: taskkill not on PATH raises
    FileNotFoundError from subprocess.run — kill_tree must not propagate it
    and must still terminate the direct child after the grace."""
    def _boom(cmd, **kw):
        raise FileNotFoundError("taskkill")

    _patch_nt(monkeypatch)
    monkeypatch.setattr(_proc_kill.subprocess, "run", _boom)

    proc = _SurvivorProcess()
    asyncio.run(kill_tree(proc, grace=0.05))  # must not raise
    assert proc.killed is True


def test_kill_tree_grace_constant():
    """The shared default grace stays at the documented 5 s."""
    assert KILL_GRACE_S == 5.0
