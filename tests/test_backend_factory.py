"""Tests for autoswe.harness.backends.factory — dispatch on backend field."""
import pytest

from autoswe.harness.backends.base import CodingBackend
from autoswe.harness.backends.claude_code import ClaudeCodeBackend
from autoswe.harness.backends.codex import CodexBackend
from autoswe.harness.backends.factory import get_backend
from autoswe.harness.backends.pi import PiBackend


def test_get_backend_defaults_to_claude_code():
    """When backend field is missing, factory defaults to Claude Code."""
    backend = get_backend({"model": "claude-sonnet-4-6"})
    assert isinstance(backend, ClaudeCodeBackend)
    assert isinstance(backend, CodingBackend)


def test_get_backend_claude_code_by_name():
    """Explicit backend: claude_code returns ClaudeCodeBackend."""
    backend = get_backend({"backend": "claude_code", "model": "claude-opus-4-8"})
    assert isinstance(backend, ClaudeCodeBackend)


def test_get_backend_claude_code_case_insensitive():
    """backend field is case-insensitive."""
    for val in ("claude_code", "CLAUDE_CODE", "Claude_Code"):
        backend = get_backend({"backend": val})
        assert isinstance(backend, ClaudeCodeBackend)


def test_unknown_backend_raises_value_error():
    """get_backend raises ValueError for unknown backends."""
    with pytest.raises(ValueError, match="Unknown coding backend"):
        get_backend({"backend": "unknown_backend"})


def test_get_backend_resolves_all_known_backends():
    """Every KNOWN_BACKENDS profile resolves to its CodingBackend.

    Enumeration axis: all backends (claude_code, codex, pi, antigravity)
    must resolve through the factory to a CodingBackend instance.
    codex/pi/antigravity are CLI backends that require a model;
    claude_code is SDK-based.
    """
    from autoswe.core.config import KNOWN_BACKENDS
    from autoswe.harness.backends.antigravity import AntigravityBackend

    profiles = {
        "claude_code": {"backend": "claude_code"},
        "codex": {"backend": "codex", "model": "gpt-5.6-terra"},
        "pi": {"backend": "pi", "model": "claude-sonnet-4-5"},
        "antigravity": {"backend": "antigravity", "model": "gemini-4-neon-high"},
    }
    expected = {
        "claude_code": ClaudeCodeBackend,
        "codex": CodexBackend,
        "pi": PiBackend,
        "antigravity": AntigravityBackend,
    }
    for name in KNOWN_BACKENDS:
        backend = get_backend(profiles[name])
        assert isinstance(backend, expected[name]), f"{name} resolved to {type(backend)}"
        assert isinstance(backend, CodingBackend), f"{name} is not a CodingBackend"


def test_get_backend_codex_without_model_raises():
    """A codex profile with no model (or empty model) fails fast."""
    with pytest.raises(ValueError, match="missing required 'model'"):
        get_backend({"backend": "codex"})
    with pytest.raises(ValueError, match="missing required 'model'"):
        get_backend({"backend": "codex", "model": ""})
    with pytest.raises(ValueError, match="missing required 'model'"):
        get_backend({"backend": "codex", "model": "   "})


def test_get_backend_codex_with_model():
    """A codex profile with a model returns a CodexBackend."""
    backend = get_backend({"backend": "codex", "model": "gpt-5.6-terra"})
    assert isinstance(backend, CodexBackend)
    assert isinstance(backend, CodingBackend)


def test_get_backend_pi_without_model_raises():
    """A pi profile with no model (or empty model) fails fast — pi would otherwise
    silently pick a settings default, which is unacceptable for reproducibility."""
    with pytest.raises(ValueError, match="missing required 'model'"):
        get_backend({"backend": "pi"})
    with pytest.raises(ValueError, match="missing required 'model'"):
        get_backend({"backend": "pi", "model": ""})
    with pytest.raises(ValueError, match="missing required 'model'"):
        get_backend({"backend": "pi", "model": "   "})


def test_get_backend_pi_case_insensitive():
    """backend field is case-insensitive for pi too."""
    for val in ("pi", "PI", "Pi"):
        backend = get_backend({"backend": val, "model": "claude-sonnet-4-5"})
        assert isinstance(backend, PiBackend)


def test_get_backend_pi_with_model():
    """A pi profile with a model returns a PiBackend."""
    backend = get_backend({"backend": "pi", "model": "claude-sonnet-4-5"})
    assert isinstance(backend, PiBackend)
    assert isinstance(backend, CodingBackend)


def test_get_backend_pi_with_provider_and_key():
    """Extra pi profile fields (provider, api_key, thinking, approve_project) are
    tolerated — the backend resolves them at run time, not construction time."""
    backend = get_backend({
        "backend": "pi",
        "model": "gpt-5.6-terra",
        "provider": "openai",
        "api_key": "sk-test",
        "thinking": "medium",
        "approve_project": True,
    })
    assert isinstance(backend, PiBackend)


def test_get_backend_antigravity_with_model():
    """An antigravity profile with a model returns an AntigravityBackend."""
    from autoswe.harness.backends.antigravity import AntigravityBackend

    backend = get_backend({"backend": "antigravity", "model": "m"})
    assert isinstance(backend, AntigravityBackend)
    assert isinstance(backend, CodingBackend)


def test_get_backend_antigravity_without_model_raises():
    """An antigravity profile with no model fails fast — agy would otherwise
    fall back to the account's last-selected model, which is unacceptable for
    reproducibility.  The message must point at `agy models`."""
    for cfg in ({"backend": "antigravity"},
                {"backend": "antigravity", "model": ""},
                {"backend": "antigravity", "model": "   "}):
        with pytest.raises(ValueError, match="missing required 'model'") as exc:
            get_backend(cfg)
        assert "agy models" in str(exc.value)


def test_get_backend_antigravity_case_insensitive():
    """backend field is case-insensitive for antigravity too."""
    from autoswe.harness.backends.antigravity import AntigravityBackend

    for val in ("antigravity", "ANTIGRAVITY", "Antigravity"):
        backend = get_backend({"backend": val, "model": "gemini-4-neon-high"})
        assert isinstance(backend, AntigravityBackend)


def test_get_backend_antigravity_import_is_deferred():
    """Resolving a claude_code profile must not import the antigravity module."""
    import sys

    saved = sys.modules.pop("autoswe.harness.backends.antigravity", None)
    try:
        get_backend({"backend": "claude_code", "model": "x"})
        assert "autoswe.harness.backends.antigravity" not in sys.modules
    finally:
        if saved is not None:
            sys.modules["autoswe.harness.backends.antigravity"] = saved


def test_get_backend_unknown_message_lists_antigravity():
    """The unknown-backend error lists antigravity among the supported names."""
    with pytest.raises(ValueError, match="Supported backends") as exc:
        get_backend({"backend": "nope"})
    assert "antigravity" in str(exc.value)


def test_get_backend_passes_with_extra_fields():
    """Extra fields in harness_cfg are tolerated (backend just needs 'backend')."""
    backend = get_backend({
        "backend": "claude_code",
        "model": "claude-sonnet-4-6",
        "timeout": 3600,
        "api_key_env": "ANTHROPIC_API_KEY",
    })
    assert isinstance(backend, ClaudeCodeBackend)


def test_claude_code_capabilities():
    """ClaudeCodeBackend advertises the full capability set."""
    caps = ClaudeCodeBackend.capabilities()
    assert "mcp" in caps
    assert "can_use_tool" in caps
    assert "plan_permission" in caps
    assert "resume" in caps
    assert "progress_stream" in caps
