"""Configuration loading for bellwether-agent.

Every tunable number lives in config.yaml and every secret lives in .env.
This module is the only place in the codebase that reads either file, so
there is exactly one answer to the question of where any value comes from.
It validates eagerly and fails with a clear message, because a scheduled
system with nobody watching must not limp along half-configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Secrets:
    groq_api_key: str
    tavily_api_key: str
    openfigi_api_key: str | None
    gmail_address: str | None
    gmail_app_password: str | None


@dataclass(frozen=True)
class Config:
    portfolio_db: Path
    state_db: Path
    memos_dir: Path
    logs_dir: Path
    managers: dict[int, str]
    detection: dict
    agent: dict
    email: dict
    secrets: Secrets


def _require(section: dict, key: str, where: str):
    if not isinstance(section, dict) or key not in section:
        raise ConfigError(f"config.yaml is missing '{key}' under '{where}'")
    return section[key]


def load_config(config_path: Path | None = None) -> Config:
    """Load and validate config.yaml and .env, failing fast on any gap."""
    config_path = config_path or PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    paths = _require(raw, "paths", "top level")
    universe = _require(raw, "universe", "top level")
    detection = _require(raw, "detection", "top level")
    agent = _require(raw, "agent", "top level")
    email = _require(raw, "email", "top level")

    portfolio_db = Path(str(_require(paths, "portfolio_db", "paths")))
    if not portfolio_db.exists():
        raise ConfigError(
            f"Portfolio database not found at {portfolio_db}. "
            "Set paths.portfolio_db in config.yaml to the portfolio.db file "
            "produced by the 13f-portfolio-analysis project."
        )

    managers_raw = _require(universe, "managers", "universe")
    managers = {int(cik): str(name) for cik, name in managers_raw.items()}
    if not managers:
        raise ConfigError("universe.managers in config.yaml is empty")

    for key in (
        "new_position_min_weight",
        "exit_min_prior_weight",
        "concentration_top5_delta",
        "accumulation_min_managers",
        "accumulation_min_shares_increase",
        "investigation_priority",
    ):
        _require(detection, key, "detection")

    for key in (
        "model",
        "temperature",
        "max_steps_per_investigation",
        "max_llm_calls_per_run",
        "max_tavily_calls_per_run",
        "max_findings_per_run",
    ):
        _require(agent, key, "agent")

    load_dotenv(PROJECT_ROOT / ".env")
    groq_key = (os.getenv("GROQ_API_KEY") or "").strip()
    tavily_key = (os.getenv("TAVILY_API_KEY") or "").strip()
    if not groq_key:
        raise ConfigError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    if not tavily_key:
        raise ConfigError(
            "TAVILY_API_KEY is not set. Copy .env.example to .env and fill it in."
        )

    secrets = Secrets(
        groq_api_key=groq_key,
        tavily_api_key=tavily_key,
        openfigi_api_key=os.getenv("OPENFIGI_API_KEY") or None,
        gmail_address=os.getenv("GMAIL_ADDRESS") or None,
        gmail_app_password=os.getenv("GMAIL_APP_PASSWORD") or None,
    )

    return Config(
        portfolio_db=portfolio_db,
        state_db=PROJECT_ROOT / str(_require(paths, "state_db", "paths")),
        memos_dir=PROJECT_ROOT / str(_require(paths, "memos_dir", "paths")),
        logs_dir=PROJECT_ROOT / str(_require(paths, "logs_dir", "paths")),
        managers=managers,
        detection=detection,
        agent=agent,
        email=email,
        secrets=secrets,
    )
