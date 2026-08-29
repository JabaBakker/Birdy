"""Slack-adapter (Socket Mode): berichten, bestanden en 👍-bevestigingen naar het brein.

Actief wanneer SLACK_BOT_TOKEN en SLACK_APP_TOKEN zijn gezet. Socket Mode = alleen
uitgaand verkeer, geen open poort op de server.

Kanalen: #birdy = conversatie (bot moet lid zijn), #briefing = leeskanaal voor de
briefings (berichten daarin worden genegeerd), DM's werken ook.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import aiohttp
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler

from .brain import Brain
from .config import Config

log = logging.getLogger("fien.slack")

CONFIRM_REACTIONS = {"+1", "thumbsup", "white_check_mark", "heavy_check_mark", "ok_hand"}
IMAGE_MIMES = ("image/",)


class SlackAdapter:
    def __init__(self, cfg: Config, brain: Brain, work_lock: asyncio.Lock):
        self.cfg = cfg
        self.brain = brain
        self.work_lock = work_lock
        self.app = AsyncApp(token=cfg.slack_bot_token)
        # De Socket Mode-handler maakt een aiohttp-sessie aan en heeft daarvoor een
        # draaiende event-loop nodig — dus pas aanmaken in start().
        self.handler: AsyncSocketModeHandler | None = None
        self._task: asyncio.Task | None = None
        self._bot_user_id: str | None = None
        self._names: dict[str, str] = {}  # member-id -> voornaam

        self.app.event("message")(self.on_message)
        self.app.event("reaction_added")(self.on_reaction)
        self.app.event("app_mention")(self._noop)  # al afgedekt door "message"
        self.app.event("file_shared")(self._noop)  # bestanden zitten in het message-event

    # -- levenscyclus -------------------------------------------------------

    async def start(self) -> None:
        auth = await self.app.client.auth_test()
        self._bot_user_id = auth["user_id"]
        self.handler = AsyncSocketModeHandler(self.app, self.cfg.slack_app_token)
        self._task = asyncio.create_task(self.handler.start_async())
        log.info(
            "Slack-adapter draait als %s. leden=%s",
            auth.get("user", "?"),
            self.cfg.slack_allowed_member_ids or "SETUP-MODUS (stuur een DM voor je member-id)",
        )

    async def stop(self) -> None:
        if self.handler:
            await self.handler.close_async()
        if self._task:
            self._task.cancel()

    async def broadcast(self, text: str, kind: str = "briefing") -> None:
        channel = (
            self.cfg.slack_channel_briefing or self.cfg.slack_channel_birdy
            if kind == "briefing"
            else self.cfg.slack_channel_birdy or self.cfg.slack_channel_briefing
        )
        if not channel:
            return
        try:
            await self.app.client.chat_postMessage(channel=channel, text=text)
        except Exception:
            log.exception("versturen naar Slack-kanaal %s mislukt", channel)

    # -- helpers ------------------------------------------------------------

    def _allowed(self, user_id: str | None) -> bool:
        return bool(user_id) and user_id in self.cfg.slack_allowed_member_ids

    async def _name(self, user_id: str) -> str:
        if user_id not in self._names:
            try:
                info = await self.app.client.users_info(user=user_id)
                profile = info["user"].get("profile", {})
                self._names[user_id] = (
                    profile.get("first_name") or profile.get("display_name")
                    or info["user"].get("real_name") or "onbekend"
                )
            except Exception:
                self._names[user_id] = "onbekend"
        return self._names[user_id]

    @staticmethod
    def _shrink_image(path: Path) -> Path:
        """Telefoonfoto's verkleinen naar max 1600px JPEG: goedkoper voor vision en
        ruim binnen de SDK-buffer. HEIC (iPhone) wordt meteen omgezet. Bij twijfel of
        fouten blijft het origineel staan."""
        try:
            from PIL import Image
            try:
                from pillow_heif import register_heif_opener
                register_heif_opener()
            except ImportError:
                pass
            img = Image.open(path)
            img = img.convert("RGB")
            img.thumbnail((1600, 1600))
            out = path.with_suffix(".jpg")
            img.save(out, "JPEG", quality=85)
            if out != path:
                path.unlink(missing_ok=True)
            return out
        except Exception:
            log.exception("foto verkleinen mislukt — origineel behouden")
            return path

    async def _download_files(self, files: list[dict]) -> list[str]:
        """Slack-bijlagen naar de workspace-inbox; geeft relatieve paden terug."""
        notes: list[str] = []
        headers = {"Authorization": f"Bearer {self.cfg.slack_bot_token}"}
        async with aiohttp.ClientSession(headers=headers) as session:
            for f in files:
                url = f.get("url_private_download") or f.get("url_private")
                if not url:
                    continue
                name = f.get("name") or "bestand"
                sub = "photos" if str(f.get("mimetype", "")).startswith(IMAGE_MIMES) else "docs"
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                dest = self.cfg.workspace / "inbox" / sub / f"{stamp}-{name}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    async with session.get(url) as resp:
                        resp.raise_for_status()
                        dest.write_bytes(await resp.read())
                    if sub == "photos":
                        dest = await asyncio.to_thread(self._shrink_image, dest)
                    notes.append(f"inbox/{sub}/{dest.name}")
                except Exception:
                    log.exception("download van Slack-bestand %s mislukt", name)
        return notes

    async def _run_brain(self, label: str, sender: str, text: str, attachments: list[str]) -> str:
        async with self.work_lock:
            reply = await self.brain.run(
                "process_message.md", label,
                sender=sender, text=text or "(geen tekst)",
                photo=", ".join(attachments) or "(geen bijlage)",
            )
        return reply or "Hm, daar ging iets mis bij het verwerken — probeer het nog eens?"

    # -- handlers -----------------------------------------------------------

    async def _noop(self, event, **kwargs) -> None:
        return

    async def on_message(self, event: dict, say, **kwargs) -> None:
        if event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
            return
        user_id = event.get("user")
        channel = event.get("channel", "")
        channel_type = event.get("channel_type", "")

        if not self._allowed(user_id):
            if not self.cfg.slack_allowed_member_ids and channel_type == "im":
                await say(
                    f"Hoi! Ik ben {self.cfg.agent_name}. Deze workspace is nog niet geactiveerd.\n"
                    f"Jouw member-id: {user_id}\n"
                    f"Zet dit id in SLACK_ALLOWED_MEMBER_IDS in de .env op de server en "
                    f"herstart mij. Daarna doe ik mee."
                )
            return
        if channel == self.cfg.slack_channel_briefing:
            return  # leeskanaal

        text = (event.get("text") or "").strip()
        if self._bot_user_id:
            text = text.replace(f"<@{self._bot_user_id}>", "").strip()
        attachments = await self._download_files(event.get("files", []))
        if not text and not attachments:
            return

        sender = await self._name(user_id)
        thread_ts = None if channel_type == "im" else (event.get("thread_ts") or event.get("ts"))
        try:
            await self.app.client.reactions_add(channel=channel, timestamp=event["ts"], name="eyes")
        except Exception:
            pass  # puur een leesbevestiging

        reply = await self._run_brain(f"slack-bericht van {sender}", sender, text, attachments)
        await say(text=reply, thread_ts=thread_ts)

    async def on_reaction(self, event: dict, **kwargs) -> None:
        if event.get("reaction") not in CONFIRM_REACTIONS:
            return
        user_id = event.get("user")
        if not self._allowed(user_id):
            return
        item = event.get("item", {})
        if item.get("type") != "message":
            return
        channel, ts = item.get("channel"), item.get("ts")
        try:
            hist = await self.app.client.conversations_history(
                channel=channel, latest=ts, inclusive=True, limit=1
            )
            msgs = hist.get("messages", [])
        except Exception:
            log.exception("voorstel-bericht ophalen mislukt")
            return
        if not msgs or msgs[0].get("user") != self._bot_user_id:
            return  # alleen 👍 op berichten van Birdy zelf zijn een bevestiging

        proposal = (msgs[0].get("text") or "").strip()
        sender = await self._name(user_id)
        text = (
            f"{sender} bevestigde zojuist met 👍 dit voorstel van jou in de chat:\n"
            f"«{proposal}»\n"
            f"Voer het voorgestelde vervolg nu uit (archiveren, agenda-items, taken — wat je "
            f"daar aankondigde) en meld kort het resultaat."
        )
        reply = await self._run_brain(f"👍-bevestiging van {sender}", sender, text, [])
        await self.app.client.chat_postMessage(
            channel=channel, text=reply, thread_ts=msgs[0].get("thread_ts") or ts
        )
