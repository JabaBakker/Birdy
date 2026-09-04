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

    # Slack (Socket Mode). Beide tokens gezet = Slack-adapter aan.
    slack_bot_token: str = field(default_factory=lambda: os.environ.get("SLACK_BOT_TOKEN", ""))
    slack_app_token: str = field(default_factory=lambda: os.environ.get("SLACK_APP_TOKEN", ""))
    # Komma-gescheiden member-ids die de bot mag bedienen. Leeg = setup-modus:
    # de bot antwoordt dan alleen in DM met het member-id, zodat je het kunt invullen.
    slack_allowed_member_ids: list[str] = field(default_factory=lambda: [
        x.strip() for x in os.environ.get("SLACK_ALLOWED_MEMBER_IDS", "").split(",") if x.strip()
    ])
    slack_channel_birdy: str = field(default_factory=lambda: os.environ.get("SLACK_CHANNEL_BIRDY", ""))
    slack_channel_briefing: str = field(
        default_factory=lambda: os.environ.get("SLACK_CHANNEL_BRIEFING", "")
    )

    # Google Drive documentenhub (map-id van "Birdy 2.0"; leeg = inbox-poll uit)
    drive_root_folder_id: str = field(
        default_factory=lambda: os.environ.get("DRIVE_ROOT_FOLDER_ID", "")
    )
    drive_inbox_poll_min: int = field(default_factory=lambda: _env_int("AGENT_DRIVE_INBOX_POLL_MIN", 5))

    # Kiosk-dashboard (muurtablet). Token gezet = dashboard aan; ontsluiting via Tailscale.
    dashboard_token: str = field(default_factory=lambda: os.environ.get("DASHBOARD_TOKEN", ""))
    dashboard_port: int = field(default_factory=lambda: _env_int("DASHBOARD_PORT", 8811))
    # Geld-tab: pincode (4-8 cijfers) die per apparaat één keer gevraagd wordt; leeg = tab uit
    dashboard_geld_pin: str = field(default_factory=lambda: os.environ.get("DASHBOARD_GELD_PIN", "").strip())
    # Namen voor de kleurcodering in de weekweergave (komma-gescheiden)
    dashboard_personen: list[str] = field(default_factory=lambda: [
        x.strip() for x in os.environ.get("DASHBOARD_PERSONEN", "").split(",")
        if x.strip()
    ])

    agent_name: str = field(default_factory=lambda: os.environ.get("AGENT_NAME", "Birdy"))
    # voor de prompts: wie zijn de ouders ("Jaap en Yvette") en wie is de vangnet-eigenaar
    ouders: str = field(default_factory=lambda: os.environ.get("AGENT_OUDERS", "de ouders"))
    vangnet: str = field(default_factory=lambda: os.environ.get("AGENT_VANGNET", "de ouder die het bericht stuurde"))

    # Vaste momenten (lokale tijd, HH:MM). Leeg = uit.
    digest_time: str = field(default_factory=lambda: os.environ.get("AGENT_DIGEST_TIME", "07:15"))
    weekly_time: str = field(default_factory=lambda: os.environ.get("AGENT_WEEKLY_TIME", "SUN 19:30"))
    # iCal-feeds (AGENDA_SYNC_ICS) elke N uur in de Google-agenda zetten; 0 = uit
    ics_sync_hours: float = field(default_factory=lambda: float(os.environ.get("AGENT_ICS_SYNC_HOURS", "6") or 0))
    # stil ochtendmoment: alleen AANDACHT.md (dashboard) bijwerken, geen chatbericht
    aandacht_time: str = field(default_factory=lambda: os.environ.get("AGENT_AANDACHT_TIME", "07:15"))
    # leeg = uit (standaard sinds 2 sep 2026: uitzoekwerk liever op verzoek dan dagelijks)
    proactive_time: str = field(default_factory=lambda: os.environ.get("AGENT_PROACTIVE_TIME", ""))

    # Model & guardrails
    model: str = field(default_factory=lambda: os.environ.get("AGENT_MODEL") or "sonnet")
    max_turns: int = field(default_factory=lambda: _env_int("AGENT_MAX_TURNS", 20))
    daily_budget_usd: float = field(default_factory=lambda: _env_float("AGENT_DAILY_BUDGET_USD", 3.0))
    cycle_budget_usd: float = field(default_factory=lambda: _env_float("AGENT_CYCLE_BUDGET_USD", 1.0))

    def validate(self) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY ontbreekt in .env")
        if not (self.slack_bot_token and self.slack_app_token):
            raise SystemExit("Geen kanaal geconfigureerd: zet SLACK_BOT_TOKEN + SLACK_APP_TOKEN in .env")
        if self.slack_bot_token and not self.slack_app_token:
            raise SystemExit("SLACK_APP_TOKEN ontbreekt (Socket Mode vereist een xapp-token)")
        if not self.workspace.exists():
            raise SystemExit(f"Workspace niet gevonden: {self.workspace}")
