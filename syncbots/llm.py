"""LLM configuration loader for the SyncBots deep-agent pipeline.

Loads per-role model configuration from ``llm_config.yaml`` and produces either
a ``provider:model`` string or a pre-initialized ``BaseChatModel`` suitable for
``deepagents.create_deep_agent`` and ``SubAgent.model``.

Roles map to the context-isolation topology:
  - main:        the primary upgrade agent (planning + editing)
  - diff_digest: reads huge LLVM diffs -> structured summary (cheap long-ctx)
  - log_analyst: parses long build logs -> root-cause summary (cheap long-ctx)
  - check_regen: regenerates FileCheck output (cheap)

Strong/weak model tiering (Claude Code style): configure ``strong_model`` and
``weak_model`` once and the roles route automatically -- the main (editing)
agent gets the strong model, all token-heavy subagent roles get the weak one.
Explicit per-role config still wins over the tier defaults.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

# New role names -- the only supported config keys.
AGENT_ROLES = ("main", "diff_digest", "log_analyst", "check_regen")

# Deprecated role names from the original LangGraph pipeline. When encountered
# in a config file, we raise a clear error directing the user to migrate.
_DEPRECATED_ROLE_NAMES = {
    "coder": "main",
    "analyzer": "diff_digest",
    "reporter": "log_analyst",
}

# Strong/weak tier per role: the main agent does the high-stakes reasoning and
# editing (strong model); the context-isolation subagents absorb token-heavy
# reads and mechanical work (weak = cheap model).
ROLE_MODEL_TIER = {
    "main": "strong",
    "diff_digest": "weak",
    "log_analyst": "weak",
    "check_regen": "weak",
}

DEFAULT_CONFIG_PATHS = [
    "llm_config.yaml",
    "llm_config.yml",
    os.path.expanduser("~/.config/syncbots/llm_config.yaml"),
]


@dataclass
class AgentLLMConfig:
    """Configuration for one role's LLM."""

    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key: str = ""
    base_url: str = ""
    # ``None`` means "don't send a temperature at all" -- some newer models
    # (e.g. Claude Opus on Vertex) reject the parameter outright.
    temperature: Optional[float] = None
    max_tokens: int = 8192

    def resolved_provider(self) -> str:
        provider = (self.provider or "").strip().lower()
        if not provider:
            provider = "openai_compatible" if self.base_url else "anthropic"
        return provider


@dataclass
class LLMConfigSet:
    """Full configuration for all roles."""

    default: AgentLLMConfig = field(default_factory=AgentLLMConfig)
    roles: dict[str, AgentLLMConfig] = field(default_factory=dict)
    github_token: str = ""
    # Strong/weak tier models (Claude Code style). When set, roles without an
    # explicit per-role config get the tier model on top of `default`.
    strong_model: str = ""
    weak_model: str = ""

    def get(self, role: str) -> AgentLLMConfig:
        return self.roles.get(role, self.default)

    def tier_model_for(self, role: str) -> str:
        """Return the strong/weak tier model name for *role* ('' if unset)."""
        tier = ROLE_MODEL_TIER.get(role, "weak")
        return self.strong_model if tier == "strong" else self.weak_model


_loaded_config: Optional[LLMConfigSet] = None


def set_loaded_config(config: LLMConfigSet) -> None:
    global _loaded_config
    _loaded_config = config


def get_loaded_config() -> Optional[LLMConfigSet]:
    return _loaded_config


def _resolve_config_path(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"LLM config file not found: {explicit}")
    env_path = os.environ.get("LLM_CONFIG_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    for candidate in DEFAULT_CONFIG_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def _parse_agent_config(data: dict) -> AgentLLMConfig:
    raw_provider = data.get("provider")
    provider = (raw_provider.strip() if isinstance(raw_provider, str) else "") or ""
    if data.get("backend") == "claudecode":
        raise ValueError(
            "llm_config: 'backend: claudecode' is no longer supported. "
            "Remove the 'backend' field -- SyncBots uses deepagents built-in tools. "
            "If you had a 'coder' section, rename it to 'main'."
        )
    raw_temp = data.get("temperature")
    temperature = float(raw_temp) if raw_temp is not None else None
    return AgentLLMConfig(
        provider=provider,
        model=data.get("model") or "claude-sonnet-4-6",
        api_key=(data.get("api_key") or "").strip(),
        base_url=(data.get("base_url") or "").strip(),
        temperature=temperature,
        max_tokens=int(data.get("max_tokens", 8192)),
    )


def load_llm_config(config_path: Optional[str] = None) -> LLMConfigSet:
    """Load LLM configuration from a YAML file (or defaults if none found)."""
    path = _resolve_config_path(config_path)
    if path is None:
        logger.info("No LLM config file found, using defaults (env ANTHROPIC_API_KEY)")
        return LLMConfigSet()

    logger.info("Loading LLM config from %s", path)
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    cfg = LLMConfigSet()
    if "default" in raw:
        cfg.default = _parse_agent_config(raw["default"])

    # New role names take priority.
    for role in AGENT_ROLES:
        if role in raw and raw[role]:
            cfg.roles[role] = _parse_agent_config(raw[role])

    # Reject deprecated role names with a clear migration message.
    for legacy, new_name in _DEPRECATED_ROLE_NAMES.items():
        if legacy in raw and raw[legacy]:
            raise ValueError(
                f"llm_config: deprecated role name '{legacy}' found. "
                f"Rename it to '{new_name}' in your llm_config.yaml. "
                f"Role mapping: coder→main, analyzer→diff_digest, reporter→log_analyst."
            )

    cfg.strong_model = (raw.get("strong_model") or "").strip()
    cfg.weak_model = (raw.get("weak_model") or "").strip()

    if raw.get("github_token"):
        cfg.github_token = raw["github_token"]
    return cfg


def apply_github_token(config: LLMConfigSet) -> None:
    if config.github_token and not os.environ.get("GITHUB_TOKEN"):
        os.environ["GITHUB_TOKEN"] = config.github_token
        logger.info("Set GITHUB_TOKEN from config file")


def create_model_for_role(
    role: str,
    config: Optional[LLMConfigSet] = None,
    override_model: Optional[str] = None,
) -> BaseChatModel:
    """Create a ``BaseChatModel`` for a role, usable by create_deep_agent/SubAgent.

    Model resolution priority:
      1. *override_model* (global ``--model`` flag);
      2. explicit per-role config (``roles[role].model``);
      3. strong/weak tier model (``strong_model`` for main, ``weak_model`` for
         subagent roles), layered on the default connection settings;
      4. the default config's model.
    """
    if config is None:
        config = _loaded_config or load_llm_config()
    cfg = config.get(role)

    resolved_model = ""
    if override_model:
        resolved_model = override_model
    elif role not in config.roles:
        resolved_model = config.tier_model_for(role)

    if resolved_model and resolved_model != cfg.model:
        cfg = AgentLLMConfig(
            provider=cfg.provider, model=resolved_model, api_key=cfg.api_key,
            base_url=cfg.base_url, temperature=cfg.temperature, max_tokens=cfg.max_tokens,
        )
        logger.debug("Role %s -> model %s", role, resolved_model)

    provider = cfg.resolved_provider()
    if provider == "anthropic":
        return _create_anthropic(cfg)
    if provider == "openai":
        return _create_openai(cfg)
    if provider == "openai_compatible":
        return _create_openai_compatible(cfg)
    raise ValueError(f"Unknown provider '{provider}' for role '{role}'")


def _create_anthropic(cfg: AgentLLMConfig) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic

    kwargs: dict[str, Any] = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
    }
    if cfg.temperature is not None:
        kwargs["temperature"] = cfg.temperature
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    if cfg.base_url:
        base = cfg.base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        kwargs["base_url"] = base
    return ChatAnthropic(**kwargs)


def _create_openai(cfg: AgentLLMConfig) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
    }
    if cfg.temperature is not None:
        kwargs["temperature"] = cfg.temperature
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    return ChatOpenAI(**kwargs)


def _create_openai_compatible(cfg: AgentLLMConfig) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    if not cfg.base_url:
        raise ValueError("base_url is required for openai_compatible provider")
    kwargs: dict[str, Any] = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "base_url": cfg.base_url,
    }
    if cfg.temperature is not None:
        kwargs["temperature"] = cfg.temperature
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    return ChatOpenAI(**kwargs)
