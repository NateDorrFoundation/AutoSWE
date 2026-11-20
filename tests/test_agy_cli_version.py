"""Guard: the ``agy`` CLI autoSWE would actually invoke must not fall below its floor.

Sibling of ``test_cli_version.py`` (Claude Code) — same shape, same skip
posture. The binary is resolved at dispatch time, either from a pinned
``cli_path`` on a harness profile or from ``PATH``, so it can drift
independently of anything pip controls (the CLI self-updates on interactive
launch, outside our control); a too-old CLI surfaces as an opaque stream
failure deep inside a run rather than as a clear "upgrade your CLI".

Floor rationale (see ``docs/autoswe/harnesses.md``): **1.5.11** is the version
the Phase 0 spike validated end-to-end (docs/autoswe/spike-antigravity.md).
The parse of the real ``agy --version`` output ("one line, no prefix") is
pinned below against the fixture-verified shape.

Resolution mirrors production (``core/config.py`` → ``AntigravityBackend._run_async``
→ ``_resolve_agy_executable``): the first antigravity profile's ``cli_path``
wins, else whatever ``agy`` is on ``PATH``. When neither resolves, the test
**skips** — agy is a user-level install that CI boxes and Codex/pi/Claude-only
deploys will not have, and CI without agy must stay green. Same posture as the
Claude CLI guard.

Bumping the floor is a one-line change: update ``MIN_AGY_VERSION`` in
``autoswe/harness/backends/antigravity.py`` (imported here) and the version
quoted in ``docs/autoswe/harnesses.md`` to match.
"""

import re
import shutil
import subprocess
import sys

import pytest

from autoswe.harness.backends.antigravity import MIN_AGY_VERSION

_VERSION_TIMEOUT_S = 30


def _vtuple(version: str) -> tuple[int, ...]:
    """Parse a dotted version into a comparable integer tuple.

    ``"1.5.12"`` → ``(1, 5, 12)``. Same stdlib-only approach as
    test_cli_version.py's ``_vtuple`` (and the backend's ``_agy_version_tuple``):
    no ``packaging`` dependency, component-wise ordering.
    """
    return tuple(int(part) for part in re.findall(r"\d+", version.split("(")[0]))


def _resolve_agy() -> str | None:
    """The binary a dispatch would invoke: first antigravity profile cli_path, else PATH.

    Mirrors the backend's resolution order (spec.cli_path / profile cli_path,
    then ``shutil.which("agy")``). A broken/absent harnesses.json is swallowed —
    the guard falls back to PATH rather than erroring.
    """
    try:
        from autoswe.core.config import load_harnesses_config

        for profile in load_harnesses_config().values():
            if profile.get("backend") == "antigravity":
                pinned = str(profile.get("cli_path") or "").strip()
                if pinned:
                    return pinned
    except Exception:
        pass
    return shutil.which("agy")


def _installed_version(agy: str) -> str | None:
    """Return the CLI's reported version string, or None if it cannot be queried."""
    try:
        proc = subprocess.run(
            [agy, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or proc.stderr).strip() or None


def test_agy_cli_meets_pinned_floor():
    """The resolvable agy CLI must be >= MIN_AGY_VERSION."""
    agy = _resolve_agy()
    if agy is None:
        pytest.skip(
            "no agy binary resolved (no antigravity profile cli_path and none on PATH) — "
            "CLI floor not applicable (agy is a user-level install; e.g. CI runners and "
            "Claude/Codex/pi-only deploys have no agy)."
        )

    reported = _installed_version(agy)
    if reported is None:
        pytest.skip(f"could not query `{agy} --version` — CLI floor not enforceable here.")

    parsed = _vtuple(reported)
    assert parsed, f"could not parse a version out of `{agy} --version` output: {reported!r}"
    assert parsed >= _vtuple(MIN_AGY_VERSION), (
        f"agy CLI {reported!r} (resolved to {agy}) is below the pinned floor "
        f"{MIN_AGY_VERSION} (see docs/autoswe/harnesses.md). The CLI self-updates on "
        f"interactive launch — run `agy` once to update, or reinstall from the "
        f"Antigravity installer."
    )


# --- The guard's own tests -------------------------------------------------
# A version guard that can only ever pass or skip is not a guard. These pin the
# three behaviours that matter: it parses the CLI's real output format, it fails
# on a too-old binary, and it skips (never fails) when there is nothing to check.


def _self():
    """This module, for monkeypatching its own module-level helpers."""
    return sys.modules[__name__]


@pytest.mark.parametrize(
    "reported,expected",
    [
        ("1.5.12", (1, 5, 12)),
        ("1.5.11", (1, 5, 11)),
        ("  1.10.4\n", (1, 10, 4)),
    ],
)
def test_vtuple_parses_cli_output(reported, expected):
    assert _vtuple(reported) == expected


def test_vtuple_orders_by_component_not_lexically():
    """1.5.9 < 1.5.11 — the trap a string comparison would fall into."""
    assert _vtuple("1.5.9") < _vtuple("1.5.11")
    assert _vtuple("1.6.0") > _vtuple("1.5.99")


def test_guard_fails_on_cli_below_floor(monkeypatch):
    monkeypatch.setattr(_self(), "_resolve_agy", lambda: "/usr/local/bin/agy")
    monkeypatch.setattr(_self(), "_installed_version", lambda agy: "1.5.10")
    with pytest.raises(AssertionError, match="below the pinned floor"):
        test_agy_cli_meets_pinned_floor()


def test_guard_passes_on_cli_at_floor(monkeypatch):
    monkeypatch.setattr(_self(), "_resolve_agy", lambda: "/usr/local/bin/agy")
    monkeypatch.setattr(_self(), "_installed_version", lambda agy: MIN_AGY_VERSION)
    test_agy_cli_meets_pinned_floor()


def test_guard_skips_when_no_agy_resolves(monkeypatch):
    """CI boxes and non-antigravity deploys have no agy binary — that is not a failure."""
    monkeypatch.setattr(_self(), "_resolve_agy", lambda: None)
    with pytest.raises(pytest.skip.Exception):
        test_agy_cli_meets_pinned_floor()


def test_guard_skips_when_version_cannot_be_queried(monkeypatch):
    monkeypatch.setattr(_self(), "_resolve_agy", lambda: "/usr/local/bin/agy")
    monkeypatch.setattr(_self(), "_installed_version", lambda agy: None)
    with pytest.raises(pytest.skip.Exception):
        test_agy_cli_meets_pinned_floor()


def test_pinned_profile_cli_path_wins_over_path(monkeypatch):
    """Resolution mirrors production: an antigravity profile cli_path beats PATH."""
    monkeypatch.setattr(
        "autoswe.core.config.load_harnesses_config",
        lambda: {"agy-live": {"backend": "antigravity", "cli_path": "/opt/pinned/agy"}},
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/agy")
    assert _resolve_agy() == "/opt/pinned/agy"


def test_falls_back_to_path_when_unpinned(monkeypatch):
    monkeypatch.setattr(
        "autoswe.core.config.load_harnesses_config",
        lambda: {"agy-live": {"backend": "antigravity", "cli_path": ""}},
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/agy")
    assert _resolve_agy() == "/usr/bin/agy"


def test_non_antigravity_profiles_do_not_supply_the_path(monkeypatch):
    """A pi profile's cli_path is pi's binary — never the agy floor's answer."""
    monkeypatch.setattr(
        "autoswe.core.config.load_harnesses_config",
        lambda: {"pi-opus": {"backend": "pi", "cli_path": "/opt/pi/bin/pi"}},
    )
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert _resolve_agy() is None


def test_resolution_survives_unloadable_config(monkeypatch):
    """A broken/absent harnesses.json must not turn the guard into an error."""
    def boom():
        raise RuntimeError("no config")

    monkeypatch.setattr("autoswe.core.config.load_harnesses_config", boom)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/agy")
    assert _resolve_agy() == "/usr/bin/agy"
