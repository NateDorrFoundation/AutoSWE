"""Factory — dispatch harness_cfg to the correct CodingBackend implementation.

Mirrors the provider factory pattern (``providers/factory.py``):
dispatch on the ``backend`` field in the harness profile dict.
"""
from __future__ import annotations

from autoswe.core.logging_utils import log
from autoswe.harness.backends.base import CodingBackend
from autoswe.harness.backends.claude_code import ClaudeCodeBackend

# -- Public API --


def get_backend(harness_cfg: dict) -> CodingBackend:
    """Return a CodingBackend for the given harness profile configuration.

    The ``backend`` field in *harness_cfg* selects the implementation::

        {"backend": "claude_code", "model": "claude-sonnet-4-6"}
        {"backend": "codex", "model": "gpt-5.6-terra"}
        {"backend": "pi", "model": "claude-sonnet-4-5"}
        {"backend": "antigravity", "model": "gemini-4-neon-high"}

    Raises ``ValueError`` on unknown backend names, or when a ``codex``,
    ``pi`` or ``antigravity`` profile omits the required ``model`` field
    (none of these CLIs has a reproducible built-in default model — pi
    would silently pick a settings default and agy the account's
    last-selected model, both unacceptable for reproducibility).
    """
    backend_name = harness_cfg.get("backend", "claude_code").lower()
    if backend_name == "claude_code":
        return ClaudeCodeBackend()
    if backend_name == "codex":
        # Codex has no built-in default model — a profile must name one.
        model = str(harness_cfg.get("model") or "").strip()
        if not model:
            raise ValueError(
                "Codex harness profile is missing required 'model'. "
                "Set it to a current Codex model, e.g. 'gpt-5.6-sol', 'gpt-5.6-terra', "
                "'gpt-5.6-luna', or 'gpt-5.5'."
            )
        # Deferred import: only loads the codex submodule when a codex profile is configured,
        # avoiding ImportError on deploys that use claude_code exclusively.
        from autoswe.harness.backends.codex import CodexBackend

        return CodexBackend()
    if backend_name == "pi":
        # pi resolves a settings default model when none is given — the profile
        # must name one so a run is reproducible (same contract as Codex).
        model = str(harness_cfg.get("model") or "").strip()
        if not model:
            raise ValueError(
                "pi harness profile is missing required 'model'. "
                "Set it to a model id or 'provider/model' pattern, e.g. "
                "'claude-sonnet-4-5', 'anthropic/claude-opus-4-8', or 'gpt-5.6-sol'."
            )
        # Deferred import: only loads the pi submodule when a pi profile is configured,
        # avoiding ImportError on deploys that use claude_code exclusively.
        from autoswe.harness.backends.pi import PiBackend

        return PiBackend()
    if backend_name == "antigravity":
        # agy falls back to the account's last-selected model when --model is
        # omitted — non-reproducible, so a profile must name one (codex/pi contract).
        model = str(harness_cfg.get("model") or "").strip()
        if not model:
            raise ValueError(
                "antigravity harness profile is missing required 'model'. "
                "Set it to an `agy models` slug, e.g. 'gemini-4-neon-high', "
                "'gemini-3.8-flash-medium', or 'Orchestrator (Neon)'."
            )
        if model.startswith("Orchestrator") and harness_cfg.get("effort"):
            # docs/antigravity/models.md: effort does not apply to the
            # Orchestrator pair; the CLI reports it as not adjustable.
            log("[FACTORY] antigravity profile sets 'effort' with an "
                f"Orchestrator model ({model!r}) — effort does not apply to "
                "Orchestrator and will be ignored by the CLI.")
        # Deferred import: only loads the antigravity submodule when such a profile is configured.
        from autoswe.harness.backends.antigravity import AntigravityBackend

        return AntigravityBackend()

    raise ValueError(
        f"Unknown coding backend: '{backend_name}'. "
        "Supported backends: claude_code, codex, pi, antigravity"
    )
