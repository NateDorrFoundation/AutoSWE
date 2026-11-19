"""Shared process-tree kill for the subprocess-shelling backends (issue #327).

The pi and Codex backends (and, once the ``antigravity`` branch merges, the
antigravity backend) all spawn a CLI subprocess that itself spawns children
(tool shells; and on Windows an npm-installed CLI runs the real node process
under a ``cmd /c`` shim).  A bare ``process.kill()`` on the timeout path
therefore leaves grandchildren orphaned.  This module is the single
implementation each backend imports — it was previously duplicated verbatim
in pi.py and codex.py, and that kind of drift is exactly what produced this
bug class.  ``antigravity.py`` on the ``antigravity`` branch carries its own
copy (introduced there in #318, not on master); whoever merges that branch
should make it the third importer of this module.  The antigravity branch is
intentionally untouched here.

Backends pair this with spawning ``start_new_session=(os.name != "nt")`` so
the child leads its own process group and a group kill can reach the tree.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess

# Grace between the SIGTERM and the SIGKILL on the process-group kill path:
# give the CLI a moment to tear down its own children before the whole group
# is force-killed.
KILL_GRACE_S = 5.0


def kill_direct(process) -> None:
    """Best-effort kill of just the direct child; never raises."""
    try:
        process.kill()
    except ProcessLookupError:
        pass


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
    own group.  Windows: ``taskkill /T /F /PID`` walks the child tree from
    the shim; if taskkill fails, is unavailable, or leaves the child alive,
    the direct child is terminated after the grace so the caller's un-timed
    ``wait()`` cannot hang.  Best-effort throughout: falls back to
    ``process.kill()`` when the group/pid is already gone; never raises
    (callers are on the timeout path and re-raise the TimeoutError after).
    """
    pid = process.pid
    if os.name == "nt":
        # /T walks the child tree (the shim's node child); /F forces.
        try:
            subprocess.run(
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
            pass
