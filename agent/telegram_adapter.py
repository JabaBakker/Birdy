"""Telegram-adapter: ontvangt berichten/foto's en levert ze aan het brein.

Actief wanneer TELEGRAM_BOT_TOKEN is gezet. Wordt in M4 verwijderd (overgang naar Slack).
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

from .brain import Brain
from .config import Config

log = logging.getLogger("fien.telegram")


class TelegramAdapter:
    def __init__(self, cfg: Config, brain: Brain, work_lock: asyncio.Lock):
        self.cfg = cfg
        self.brain = brain
        self.work_lock = work_lock
        self.app = Application.builder().token(cfg.bot_token).build()
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("overzicht", self.cmd_overzicht))
        self.app.add_handler(CommandHandler("hulp", self.cmd_hulp))
        self.app.add_handler(CommandHandler("help", self.cmd_hulp))
        self.app.add_handler(CommandHandler("intenties", self.cmd_intenties))
        self.app.add_handler(CommandHandler("afspraken", self.cmd_intenties))  # alias
        self.app.add_handler(
            MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, self.handle_message)
        )

    # -- levenscyclus -------------------------------------------------------

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(allowed_updates=["message"])
        log.info("Telegram-adapter draait. chats=%s",
                 self.cfg.allowed_chat_ids or "SETUP-MODUS (stuur /start voor chat-id)")

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    async def broadcast(self, text: str, kind: str = "briefing") -> None:
        for chat_id in self.cfg.allowed_chat_ids:
            try:
                await self.app.bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                log.exception("versturen naar %s mislukt", chat_id)

    # -- handlers -----------------------------------------------------------

    def allowed(self, update: Update) -> bool:
        return bool(update.effective_chat) and update.effective_chat.id in self.cfg.allowed_chat_ids

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not self.allowed(update):
            await chat.send_message(
                f"Hoi! Ik ben {self.cfg.agent_name}. Dit gesprek is nog niet geactiveerd.\n"
                f"Chat-id: {chat.id}\n"
                f"Zet dit id in TELEGRAM_ALLOWED_CHAT_IDS in de .env op de server en "
                f"herstart mij. Daarna doe ik mee."
            )
            return
        await chat.send_message(
            f"Hoi, ik ben {self.cfg.agent_name} 👋 Stuur me losse gedachten, to-do's of foto's van "
            f"lijstjes en brieven — ik maak er taken van met een eigenaar en een datum.\n"
            f"• /overzicht — alles wat loopt\n"
            f"Elke ochtend om {self.cfg.digest_time} zet ik hier de dagbriefing neer."
        )

    async def _send_workspace_file(self, update: Update, name: str, fallback: str) -> None:
        if not self.allowed(update):
            return
        path = self.cfg.workspace / name
        text = path.read_text().strip() if path.exists() else ""
        await update.effective_chat.send_message(text or fallback)

    async def cmd_overzicht(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send_workspace_file(
            update, "OVERZICHT.md", "Nog geen overzicht — stuur me eerst wat taken!"
        )

    async def cmd_hulp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send_workspace_file(
            update, "HULP.md", "Er is nog geen hulptekst — vraag me gewoon wat ik kan!"
        )

    async def cmd_intenties(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send_workspace_file(
            update, "INTENTIES.md", "Nog geen intenties — zeg 'nieuwe intentie: …' en ik zet hem erbij."
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.allowed(update) or not update.message:
            return
        msg = update.message
        sender = (msg.from_user.first_name if msg.from_user else "onbekend")
        text = msg.text or msg.caption or ""

        photo_note = ""
        if msg.photo:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = self.cfg.workspace / "inbox" / "photos" / f"{stamp}.jpg"
            file = await msg.photo[-1].get_file()
            await file.download_to_drive(str(dest))
            photo_note = f"inbox/photos/{dest.name}"

        await update.effective_chat.send_action(ChatAction.TYPING)
        async with self.work_lock:
            reply = await self.brain.run(
                "process_message.md", f"bericht van {sender}",
                sender=sender, text=text or "(geen tekst)",
                photo=photo_note or "(geen bijlage)",
            )
        await msg.reply_text(reply or "Hm, daar ging iets mis bij het verwerken — probeer het nog eens?")
