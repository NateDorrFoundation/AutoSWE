"""Shared process-tree kill for the subprocess-shelling backends (issue #327).

The pi and Codex backends (and, once the ``antigravity`` branch merges, the
antigravity backend) all spawn a CLI subprocess that itself spawns children
(tool shells; and on Windows an npm-installed CLI runs the real node process
under a ``cmd /c`` shim).  Before #327, ``pi.py`` and ``codex.py`` killed
that tree with a bare ``process.kill()`` on the timeout path, which leaves
grandchildren orphaned; ``antigravity.py`` on the ``antigravity`` branch
(introduced there in #318, not on master) carried the first tree-kill
implementation.  This module is now the single implementation both backends
import, and whoever merges ``antigravity`` should make it the third importer
so the copies cannot drift apart again.  The antigravity branch is
intentionally untouched here.

Backends pair this with spawning ``start_new_session=(os.name != "nt")`` so
the child leads its own process group and a group kill can reach the tree.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time

# Grace between the SIGTERM and the SIGKILL on the process-group kill path:
# give the CLI a moment to tear down its own children before the whole group
# is force-killed.  Also bounds the group-emptiness re-check below.
KILL_GRACE_S = 5.0

# Module-local platform indirection: tests monkeypatch this instead of the
# global ``os.name`` singleton (which would flip every other library onto the
# Windows code path for the duration of the test).
_IS_NT = os.name == "nt"


def kill_direct(process) -> None:
    """Best-effort kill of just the direct child; never raises."""
    try:
        process.kill()
    except ProcessLookupError:
        pass


async def _sigkill_group_survivors(pgid: int, budget: float) -> None:
    """SIGKILL any group member that outlived the direct child (issue #327).

    The direct child's exit does not prove the group is empty: a grandchild
    that traps SIGTERM (``trap '' TERM``) survives the group SIGTERM and
    would be orphaned the moment its parent dies.  ``os.killpg(pgid, 0)``
    succeeds while the group still has members, so poll it (0.5 s, bounded
    by *budget*) and SIGKILL any survivor — SIGKILL cannot be trapped, so
    the re-poll confirms the group empties.  Best-effort: once the budget
    is spent we return (e.g. the group may hold only zombies awaiting reap
    by an intervening parent, which occupy a slot but run no code).
    """
    deadline = time.monotonic() + budget
    while True:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return  # group empty (or un-signalable — not ours to clean)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            return  # re-poll budget spent; survivors are unreapable zombies
        await asyncio.sleep(0.5)


async def kill_tree(process, grace: float = KILL_GRACE_S) -> None:
    """Kill the child process AND its whole process tree (issue #327).

    The CLI subprocess we spawn has children of its own, so a bare
    ``process.kill()`` on the timeout path orphans them — on Windows the
    ``cmd`` shim dies while the real node process keeps running.

    POSIX: the subprocess is spawned with ``start_new_session=True`` so it
    leads its own process group and ``os.killpg`` reaches the whole tree —
    SIGTERM first, then SIGKILL after *grace* seconds if it is still alive.
    The group kill is guarded on the child actually being the group leader
    (``pgid == pid``), so a spawn that forgot the flag can never signal our
    own group.  After the direct child is gone the group is *re-checked*
    (``_sigkill_group_survivors``): a SIGTERM-trapping grandchild is
    SIGKILLed too.  Windows: ``taskkill /T /F /PID`` (already tree-wide)
    runs off the event loop via ``asyncio.to_thread``; if taskkill fails,
    is unavailable, or leaves the child alive, the direct child is
    terminated after the grace so the caller's un-timed ``wait()`` cannot
    hang.  Best-effort throughout: falls back to ``process.kill()`` when
    the group/pid is already gone; never raises (callers are on the timeout
    path and re-raise the TimeoutError after).
    """
    pid = process.pid
    if _IS_NT:
        # /T walks the child tree (the shim's node child); /F forces.
        # to_thread keeps the (blocking) taskkill launch off the event loop.
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        except OSError:
            pass  # taskkill unavailable — the fallback below still kills the child
        # taskkill with check=False can run fine and still leave the child
        # alive (access denied, race, or the OSError above).  The caller's
        # subsequent un-timed ``await process.wait()`` would then hang, so
        # guarantee the direct child dies: give the tree-kill *grace* seconds
        # to land, then terminate the direct child ourselves.
        try:
            await asyncio.wait_for(process.wait(), timeout=grace)
        except asyncio.TimeoutError:
            kill_direct(process)
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        kill_direct(process)  # pid already gone
        return
    if pgid != pid:
        # Not our session leader (spawn without start_new_session, or a
        # test double): a group kill would hit the wrong group — kill just
        # the direct child instead.
        kill_direct(process)
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return  # group already exited; the caller's wait() just reaps
    except PermissionError:
        kill_direct(process)
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
    except asyncio.TimeoutError:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # group vanished mid-flight; the survivor check confirms
    # The direct child being gone does NOT prove the tree is gone: a
    # SIGTERM-trapping grandchild survives its parent (review MEDIUM).
    # Re-check the group and SIGKILL survivors (no-op when empty).
    await _sigkill_group_survivors(pgid, grace)
