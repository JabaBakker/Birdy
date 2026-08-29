"""Gezins-agent: Telegram-bot + brein + vaste momenten (ochtendbriefing, weekplanning).

Draaien:  python -m agent.main
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters,
)

from .brain import Brain, ensure_git
from .config import Config
from .ledger import Ledger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("fien")

cfg = Config()
ledger: Ledger
brain: Brain
work_lock = asyncio.Lock()  # één denkcyclus tegelijk


def allowed(update: Update) -> bool:
    return bool(update.effective_chat) and update.effective_chat.id in cfg.allowed_chat_ids


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not allowed(update):
        await chat.send_message(
            f"Hoi! Ik ben {cfg.agent_name}. Dit gesprek is nog niet geactiveerd.\n"
            f"Chat-id: {chat.id}\n"
            f"Zet dit id in TELEGRAM_ALLOWED_CHAT_IDS in de .env op de server en "
            f"herstart mij. Daarna doe ik mee."
        )
        return
    await chat.send_message(
        f"Hoi, ik ben {cfg.agent_name} 👋 Stuur me losse gedachten, to-do's of foto's van "
        f"lijstjes en brieven — ik maak er taken van met een eigenaar en een datum.\n"
        f"• /overzicht — alles wat loopt\n"
        f"Elke ochtend om {cfg.digest_time} zet ik hier de dagbriefing neer."
    )


async def cmd_overzicht(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    path = cfg.workspace / "OVERZICHT.md"
    text = path.read_text().strip() if path.exists() else ""
    await update.effective_chat.send_message(text or "Nog geen overzicht — stuur me eerst wat taken!")


async def cmd_hulp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    path = cfg.workspace / "HULP.md"
    text = path.read_text().strip() if path.exists() else ""
    await update.effective_chat.send_message(text or "Er is nog geen hulptekst — vraag me gewoon wat ik kan!")


async def cmd_intenties(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    path = cfg.workspace / "INTENTIES.md"
    text = path.read_text().strip() if path.exists() else ""
    await update.effective_chat.send_message(
        text or "Nog geen intenties — zeg 'nieuwe intentie: …' en ik zet hem erbij."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update) or not update.message:
        return
    msg = update.message
    sender = (msg.from_user.first_name if msg.from_user else "onbekend")
    text = msg.text or msg.caption or ""

    photo_note = ""
    if msg.photo:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = cfg.workspace / "inbox" / "photos" / f"{stamp}.jpg"
        file = await msg.photo[-1].get_file()
        await file.download_to_drive(str(dest))
        photo_note = f"inbox/photos/{dest.name}"

    await update.effective_chat.send_action(ChatAction.TYPING)
    async with work_lock:
        reply = await brain.run(
            "process_message.md", f"bericht van {sender}",
            sender=sender, text=text or "(geen tekst)",
            photo=photo_note or "(geen foto)",
        )
    await msg.reply_text(reply or "Hm, daar ging iets mis bij het verwerken — probeer het nog eens?")


async def broadcast(app: Application, text: str | None) -> None:
    if not text or text.strip().upper() == "STIL":
        return
    for chat_id in cfg.allowed_chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            log.exception("versturen naar %s mislukt", chat_id)


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


async def scheduler(app: Application) -> None:
    fired: dict[str, str] = {}
    jobs = [
        ("digest", cfg.digest_time, "digest.md", "ochtendbriefing"),
        ("weekly", cfg.weekly_time, "weekly.md", "weekplanning"),
        ("proactive", cfg.proactive_time, "proactive.md", "eigen initiatief"),
    ]
    while True:
        now = datetime.now()
        for name, spec, prompt, label in jobs:
            key = _due(spec, now, fired.get(name))
            if key:
                fired[name] = key
                log.info("vast moment: %s", label)
                async with work_lock:
                    reply = await brain.run(prompt, label)
                await broadcast(app, reply)
        await asyncio.sleep(20)


async def post_init(app: Application) -> None:
    app.create_task(scheduler(app))
    log.info(
        "%s draait. chats=%s digest=%s budget=$%.2f/dag",
        cfg.agent_name, cfg.allowed_chat_ids or "SETUP-MODUS (stuur /start voor chat-id)",
        cfg.digest_time, cfg.daily_budget_usd,
    )


def main() -> None:
    global ledger, brain
    cfg.validate()
    ensure_git(cfg.workspace)
    ledger = Ledger(cfg.workspace / "memory" / "ledger.json")
    brain = Brain(cfg, ledger)

    app = Application.builder().token(cfg.bot_token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("overzicht", cmd_overzicht))
    app.add_handler(CommandHandler("hulp", cmd_hulp))
    app.add_handler(CommandHandler("help", cmd_hulp))
    app.add_handler(CommandHandler("intenties", cmd_intenties))
    app.add_handler(CommandHandler("afspraken", cmd_intenties))  # alias
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
