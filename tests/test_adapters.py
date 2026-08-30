"""Offline-tests voor de Birdy 2.0-codebase: imports, adapters, scheduler-logica,
Slack-handlers met een gestubd brein, en de Todoist-paginering."""
import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# claude_agent_sdk is niet lokaal geïnstalleerd (alleen op de server nodig) — stub.
sdk = types.ModuleType("claude_agent_sdk")
sdk.ClaudeAgentOptions = MagicMock()
sdk.ResultMessage = type("ResultMessage", (), {})
sdk.query = MagicMock()
sys.modules["claude_agent_sdk"] = sdk

os.environ.update({
    "ANTHROPIC_API_KEY": "test",
    "TELEGRAM_BOT_TOKEN": "123456:TEST",
    "TELEGRAM_ALLOWED_CHAT_IDS": "-100",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_ALLOWED_MEMBER_IDS": "U_JAAP,U_YVETTE",
    "SLACK_CHANNEL_BIRDY": "C_BIRDY",
    "SLACK_CHANNEL_BRIEFING": "C_BRIEF",
    "AGENT_WORKSPACE": str(Path(__file__).parent / ".test-workspace"),
})
Path(os.environ["AGENT_WORKSPACE"]).mkdir(exist_ok=True)

from agent import main as agent_main            # noqa: E402
from agent.config import Config                 # noqa: E402
from agent.slack_adapter import SlackAdapter    # noqa: E402
from agent.telegram_adapter import TelegramAdapter  # noqa: E402
from datetime import datetime                   # noqa: E402


class FakeBrain:
    def __init__(self):
        self.calls = []

    async def run(self, prompt, label, **fmt):
        self.calls.append((prompt, label, fmt))
        return "OK-ANTWOORD"


def make_slack(cfg=None):
    cfg = cfg or Config()
    brain = FakeBrain()
    a = SlackAdapter(cfg, brain, asyncio.Lock())
    a._bot_user_id = "U_BIRDY"
    client = a.app.client  # methodes patchen; de property zelf is read-only
    client.reactions_add = AsyncMock()
    client.chat_postMessage = AsyncMock()
    client.users_info = AsyncMock(
        return_value={"user": {"profile": {"first_name": "Jaap"}, "real_name": "Jaap"}}
    )
    return a, brain


class Tests(unittest.IsolatedAsyncioTestCase):
    def test_imports_en_constructie(self):
        cfg = Config()
        brain = FakeBrain()
        TelegramAdapter(cfg, brain, asyncio.Lock())   # bouwt PTB Application
        SlackAdapter(cfg, brain, asyncio.Lock())      # bouwt Bolt AsyncApp
        self.assertEqual(cfg.slack_allowed_member_ids, ["U_JAAP", "U_YVETTE"])

    def test_due(self):
        now = datetime(2026, 8, 30, 19, 30)  # zondag
        self.assertIsNotNone(agent_main._due("SUN 19:30", now, None))
        self.assertIsNone(agent_main._due("SUN 19:31", now, None))
        self.assertIsNone(agent_main._due("MON 19:30", now, None))
        key = agent_main._due("07:15", datetime(2026, 8, 31, 7, 15), None)
        self.assertIsNotNone(key)
        self.assertIsNone(agent_main._due("07:15", datetime(2026, 8, 31, 7, 15), key))

    async def test_slack_bericht_van_gezinslid(self):
        a, brain = make_slack()
        say = AsyncMock()
        await a.on_message(
            {"user": "U_JAAP", "channel": "C_BIRDY", "channel_type": "channel",
             "ts": "1.0", "text": "voeg kwark toe aan de boodschappen"},
            say=say,
        )
        self.assertEqual(len(brain.calls), 1)
        self.assertEqual(brain.calls[0][2]["sender"], "Jaap")
        say.assert_awaited_once()
        self.assertEqual(say.await_args.kwargs["thread_ts"], "1.0")

    async def test_slack_vreemden_en_briefingkanaal_genegeerd(self):
        a, brain = make_slack()
        say = AsyncMock()
        await a.on_message({"user": "U_HACKER", "channel": "C_BIRDY",
                            "channel_type": "channel", "ts": "1.0", "text": "hoi"}, say=say)
        await a.on_message({"user": "U_JAAP", "channel": "C_BRIEF",
                            "channel_type": "channel", "ts": "1.0", "text": "hoi"}, say=say)
        await a.on_message({"user": "U_JAAP", "channel": "C_BIRDY", "channel_type": "channel",
                            "ts": "1.0", "text": "bot-echo", "bot_id": "B1"}, say=say)
        self.assertEqual(brain.calls, [])
        say.assert_not_awaited()

    async def test_slack_setup_modus(self):
        cfg = Config(slack_allowed_member_ids=[])
        a, brain = make_slack(cfg)
        say = AsyncMock()
        await a.on_message({"user": "U_NIEUW", "channel": "D1", "channel_type": "im",
                            "ts": "1.0", "text": "hoi"}, say=say)
        self.assertEqual(brain.calls, [])
        self.assertIn("U_NIEUW", say.await_args.args[0])

    async def test_slack_duim_bevestiging(self):
        a, brain = make_slack()
        a.app.client.conversations_history = AsyncMock(return_value={
            "messages": [{"user": "U_BIRDY", "ts": "1.0",
                          "text": "Zal ik dit archiveren onder Evi/School?"}]
        })
        await a.on_reaction({"reaction": "+1", "user": "U_JAAP",
                             "item": {"type": "message", "channel": "C_BIRDY", "ts": "1.0"}})
        self.assertEqual(len(brain.calls), 1)
        self.assertIn("bevestigde", brain.calls[0][2]["text"])
        a.app.client.chat_postMessage.assert_awaited_once()

    async def test_slack_duim_op_menselijk_bericht_genegeerd(self):
        a, brain = make_slack()
        a.app.client.conversations_history = AsyncMock(return_value={
            "messages": [{"user": "U_YVETTE", "ts": "1.0", "text": "gewoon een bericht"}]
        })
        await a.on_reaction({"reaction": "+1", "user": "U_JAAP",
                             "item": {"type": "message", "channel": "C_BIRDY", "ts": "1.0"}})
        self.assertEqual(brain.calls, [])

    async def test_broadcast_kanaalkeuze(self):
        a, _ = make_slack()
        await a.broadcast("briefing-tekst", kind="briefing")
        await a.broadcast("chat-tekst", kind="chat")
        kanalen = [c.kwargs["channel"] for c in a.app.client.chat_postMessage.await_args_list]
        self.assertEqual(kanalen, ["C_BRIEF", "C_BIRDY"])

    async def test_broadcast_stil(self):
        a, _ = make_slack()
        await agent_main.broadcast([a], "STIL")
        await agent_main.broadcast([a], None)
        a.app.client.chat_postMessage.assert_not_awaited()

    def test_todoist_paginering(self):
        from agent import todoist
        pages = [
            {"results": [{"id": 1, "name": "Boodschappen"}], "next_cursor": "abc"},
            {"results": [{"id": 2, "name": "Acties"}], "next_cursor": None},
        ]
        calls = []

        def fake_request(method, path, **kw):
            calls.append(kw.get("params", {}).get("cursor"))
            return pages[len(calls) - 1]

        orig = todoist._request
        todoist._request = fake_request
        try:
            out = todoist._list_all("/projects")
        finally:
            todoist._request = orig
        self.assertEqual([p["name"] for p in out], ["Boodschappen", "Acties"])
        self.assertEqual(calls, [None, "abc"])

    async def test_dashboard(self):
        from agent.dashboard import Dashboard
        cfg = Config(dashboard_token="geheim", dashboard_port=18811)
        brain = FakeBrain()
        d = Dashboard(cfg, brain, asyncio.Lock(), [])
        await d.start()
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get("http://127.0.0.1:18811/") as r:
                    self.assertEqual(r.status, 200)
                    self.assertIn("Birdy", await r.text())
                async with s.get("http://127.0.0.1:18811/api/overview") as r:
                    self.assertEqual(r.status, 401)  # zonder sleutel
                async with s.get("http://127.0.0.1:18811/api/overview",
                                 headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 200)
                    data = await r.json()
                    for veld in ("agenda", "taken", "boodschappen", "acties", "verjaardagen"):
                        self.assertIn(veld, data)
                async with s.post("http://127.0.0.1:18811/api/message",
                                  json={"text": "voeg kwark toe"},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual((await r.json())["reply"], "OK-ANTWOORD")
            self.assertEqual(brain.calls[0][2]["text"], "voeg kwark toe")
        finally:
            await d.stop()

    def test_overzicht_kort(self):
        from agent.dashboard import _overzicht_kort
        kort = _overzicht_kort(
            "📋 OVERZICHT (bijgewerkt)\n\n🔴 NU / TE LAAT\n• Cadeau regelen — Jaap\n\n"
            "🟠 DEZE WEEK\n• Zwemles opzeggen — Yvette\n\n🟡 LATER\n• Schuur opruimen\n"
            "• Banden wisselen\n\n⏳ WACHTEN OP\n• Reactie school\n\n✅ Net klaar: iets"
        )
        self.assertEqual(kort["urgent"], ["Cadeau regelen — Jaap"])
        self.assertEqual(kort["week"], ["Zwemles opzeggen — Yvette"])
        self.assertEqual(kort["rest"], "2 voor later · 1 wachten op")

    def test_gdrive_query_escaping(self):
        from agent import gdrive
        self.assertEqual(gdrive._q("Huis & tuin's"), "Huis & tuin\\'s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
