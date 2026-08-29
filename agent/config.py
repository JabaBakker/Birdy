"""Configuratie van de gezins-agent, volledig via environment-variabelen."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    workspace: Path = field(
        default_factory=lambda: Path(os.environ.get("AGENT_WORKSPACE", REPO_ROOT / "workspace"))
    )
    prompts_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("AGENT_PROMPTS_DIR", REPO_ROOT / "prompts"))
    )

    # Telegram
    bot_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    # Komma-gescheiden chat-ids die de bot mag bedienen. Leeg = setup-modus:
    # de bot antwoordt dan alleen op /start met het chat-id, zodat je het kunt invullen.
    allowed_chat_ids: list[int] = field(default_factory=lambda: [
        int(x) for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").replace(" ", "").split(",")
        if x.strip().lstrip("-").isdigit()
    ])

    agent_name: str = field(default_factory=lambda: os.environ.get("AGENT_NAME", "Fien"))

    # Vaste momenten (lokale tijd, HH:MM). Leeg = uit.
    digest_time: str = field(default_factory=lambda: os.environ.get("AGENT_DIGEST_TIME", "07:15"))
    weekly_time: str = field(default_factory=lambda: os.environ.get("AGENT_WEEKLY_TIME", "SUN 19:30"))
    proactive_time: str = field(default_factory=lambda: os.environ.get("AGENT_PROACTIVE_TIME", "13:00"))

    # Model & guardrails
    model: str = field(default_factory=lambda: os.environ.get("AGENT_MODEL") or "sonnet")
    max_turns: int = field(default_factory=lambda: _env_int("AGENT_MAX_TURNS", 20))
    daily_budget_usd: float = field(default_factory=lambda: _env_float("AGENT_DAILY_BUDGET_USD", 3.0))
    cycle_budget_usd: float = field(default_factory=lambda: _env_float("AGENT_CYCLE_BUDGET_USD", 1.0))

    def validate(self) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY ontbreekt in .env")
        if not self.bot_token:
            raise SystemExit("TELEGRAM_BOT_TOKEN ontbreekt in .env (maak een bot via @BotFather)")
        if not self.workspace.exists():
            raise SystemExit(f"Workspace niet gevonden: {self.workspace}")
