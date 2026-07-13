"""Unit tests for strong/weak model tiering (Claude Code style role routing)."""

from __future__ import annotations

import syncbots.llm as llm
from syncbots.llm import AgentLLMConfig, LLMConfigSet


def _capture_models(monkeypatch):
    created: dict[str, str] = {}

    def fake_anthropic(cfg):
        created["model"] = cfg.model
        return object()

    monkeypatch.setattr(llm, "_create_anthropic", fake_anthropic)
    return created


def _tier_config(**kw):
    return LLMConfigSet(
        default=AgentLLMConfig(provider="anthropic", model="default-model"), **kw,
    )


def test_main_gets_strong_model(monkeypatch):
    created = _capture_models(monkeypatch)
    cfg = _tier_config(strong_model="claude-opus-4-8", weak_model="claude-haiku-4-5")
    llm.create_model_for_role("main", cfg)
    assert created["model"] == "claude-opus-4-8"


def test_subagent_roles_get_weak_model(monkeypatch):
    created = _capture_models(monkeypatch)
    cfg = _tier_config(strong_model="claude-opus-4-8", weak_model="claude-haiku-4-5")
    for role in ("diff_digest", "log_analyst", "check_regen"):
        llm.create_model_for_role(role, cfg)
        assert created["model"] == "claude-haiku-4-5", role


def test_explicit_role_config_beats_tier(monkeypatch):
    created = _capture_models(monkeypatch)
    cfg = _tier_config(strong_model="claude-opus-4-8", weak_model="claude-haiku-4-5")
    cfg.roles["diff_digest"] = AgentLLMConfig(provider="anthropic", model="my-special-model")
    llm.create_model_for_role("diff_digest", cfg)
    assert created["model"] == "my-special-model"


def test_override_model_beats_everything(monkeypatch):
    created = _capture_models(monkeypatch)
    cfg = _tier_config(strong_model="claude-opus-4-8", weak_model="claude-haiku-4-5")
    llm.create_model_for_role("main", cfg, override_model="forced-model")
    assert created["model"] == "forced-model"


def test_no_tier_falls_back_to_default(monkeypatch):
    created = _capture_models(monkeypatch)
    llm.create_model_for_role("main", _tier_config())
    assert created["model"] == "default-model"


def test_missing_weak_model_falls_back_to_default(monkeypatch):
    created = _capture_models(monkeypatch)
    cfg = _tier_config(strong_model="claude-opus-4-8")  # weak unset
    llm.create_model_for_role("log_analyst", cfg)
    assert created["model"] == "default-model"


def test_load_yaml_tier_models(tmp_path):
    p = tmp_path / "llm_config.yaml"
    p.write_text(
        "default:\n  provider: anthropic\n  model: base\n"
        "strong_model: claude-opus-4-8\nweak_model: claude-haiku-4-5\n"
    )
    cfg = llm.load_llm_config(str(p))
    assert cfg.strong_model == "claude-opus-4-8"
    assert cfg.weak_model == "claude-haiku-4-5"
    assert cfg.tier_model_for("main") == "claude-opus-4-8"
    assert cfg.tier_model_for("check_regen") == "claude-haiku-4-5"
