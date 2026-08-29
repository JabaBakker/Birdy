"""Gezins-agent: adapters (Slack/Telegram) + brein + vaste momenten.

Draaien:  python -m agent.main

Adapters gaan aan op basis van de .env: TELEGRAM_BOT_TOKEN → Telegram,
SLACK_BOT_TOKEN + SLACK_APP_TOKEN → Slack. Beide tegelijk kan (overgangsfase).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from .brain import Brain, ensure_git
from .config import Config
from .ledger import Ledger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("fien")

cfg = Config()
work_lock = asyncio.Lock()  # één denkcyclus tegelijk


def _due(spec: str, now: datetime, last: str | None) -> str | None:
    """Geeft een fire-key terug als 'spec' (HH:MM of 'SUN 19:30') nu moet vuren."""
    if not spec:
        return None
    parts = spec.strip().upper().split()
    days = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
    if len(parts) == 2:
        if days.get(parts[0]) != now.weekday():
            return None
        hhmm = parts[1]
    else:
        hhmm = parts[0]
    if now.strftime("%H:%M") != hhmm:
        return None
    key = f"{spec}@{now:%Y-%m-%d %H:%M}"
    return None if key == last else key


async def broadcast(adapters: list, text: str | None, kind: str = "briefing") -> None:
    if not text or text.strip().upper() == "STIL":
        return
    for adapter in adapters:
        try:
            await adapter.broadcast(text, kind)
        except Exception:
            log.exception("broadcast via %s mislukt", type(adapter).__name__)


async def check_drive_inbox(brain: Brain, adapters: list) -> None:
    """Nieuwe bestanden in de Drive-inbox ophalen en door het brein laten verwerken."""
    from . import gdrive  # lazy: google-libs alleen laden als Drive geconfigureerd is

    try:
        new_files = await asyncio.to_thread(gdrive.poll_inbox, cfg.workspace)
    except Exception:
        log.exception("Drive-inbox poll mislukt")
        return
    if not new_files:
        return
    log.info("Drive-inbox: %d nieuw(e) bestand(en)", len(new_files))
    listing = "\n".join(f"- {name} → {local}" for name, local in new_files)
    async with work_lock:
        reply = await brain.run(
            "process_message.md", "drive-inbox",
            sender="de Drive-inbox (map 00 Inbox)",
            text=(
                "Er zijn nieuwe bestanden in de Drive-inbox gezet. Ze zijn al lokaal "
                f"gedownload:\n{listing}\n"
                "Lees ze en doe per bestand een archiveer-voorstel in de chat."
            ),
            photo=", ".join(local for _, local in new_files),
        )
    await broadcast(adapters, reply, kind="chat")


async def scheduler(brain: Brain, adapters: list) -> None:
    fired: dict[str, str] = {}
    jobs = [
        ("digest", cfg.digest_time, "digest.md", "ochtendbriefing"),
        ("weekly", cfg.weekly_time, "weekly.md", "weekplanning"),
        ("proactive", cfg.proactive_time, "proactive.md", "eigen initiatief"),
    ]
    last_inbox_check = datetime.min
    while True:
        now = datetime.now()
        for name, spec, prompt, label in jobs:
            key = _due(spec, now, fired.get(name))
            if key:
                fired[name] = key
                log.info("vast moment: %s", label)
                async with work_lock:
                    reply = await brain.run(prompt, label)
                await broadcast(adapters, reply, kind="briefing")
        if (
            cfg.drive_root_folder_id
            and cfg.drive_inbox_poll_min > 0
            and (now - last_inbox_check).total_seconds() >= cfg.drive_inbox_poll_min * 60
        ):
            last_inbox_check = now
            await check_drive_inbox(brain, adapters)
        await asyncio.sleep(20)


async def amain() -> None:
    cfg.validate()
    ensure_git(cfg.workspace)
    ledger = Ledger(cfg.workspace / "memory" / "ledger.json")
    brain = Brain(cfg, ledger)

    adapters = []
    if cfg.bot_token:
        from .telegram_adapter import TelegramAdapter
        adapters.append(TelegramAdapter(cfg, brain, work_lock))
    if cfg.slack_bot_token and cfg.slack_app_token:
        from .slack_adapter import SlackAdapter
        adapters.append(SlackAdapter(cfg, brain, work_lock))

    started = []
    for adapter in adapters:
        try:
            await adapter.start()
            started.append(adapter)
        except Exception:
            log.exception(
                "adapter %s start niet (checkt tokens in .env) — ik draai door met de rest",
                type(adapter).__name__,
            )
    adapters = started
    if not adapters:
        raise SystemExit("Geen enkele adapter kon starten — check de tokens in .env")
    log.info(
        "%s draait. adapters=%s digest=%s budget=$%.2f/dag",
        cfg.agent_name, [type(a).__name__ for a in adapters],
        cfg.digest_time, cfg.daily_budget_usd,
    )
    try:
        await scheduler(brain, adapters)
    finally:
        for adapter in adapters:
            try:
                await adapter.stop()
            except Exception:
                log.exception("adapter %s stoppen mislukt", type(adapter).__name__)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
