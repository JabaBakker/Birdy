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
        SlackAdapter(cfg, brain, asyncio.Lock())      # bouwt Bolt AsyncApp
        cfg.validate()  # Slack-tokens aanwezig → geen SystemExit
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
                    for veld in ("agenda", "onderwerpen", "aandacht", "boodschappen", "acties",
                                 "verjaardagen"):
                        self.assertIn(veld, data)
                    self.assertEqual(data["aandacht"]["birdy"]["items"], [])
                async with s.get("http://127.0.0.1:18811/api/agenda?van=2026-09-10",
                                 headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 400)  # tot ontbreekt
                async with s.get("http://127.0.0.1:18811/api/agenda?van=2026-09-10&tot=2026-09-17",
                                 headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 200)
                    self.assertEqual((await r.json())["events"], [])  # geen agenda gekoppeld
                async with s.get("http://127.0.0.1:18811/api/agenda?zoek=tandarts",
                                 headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 200)
                async with s.post("http://127.0.0.1:18811/api/event", json={"id": "abc", "titel": ""},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 400)  # lege titel
                async with s.post("http://127.0.0.1:18811/api/event",
                                  json={"id": "abc", "start": "2026-09-10T10:00", "eind": "2026-09-10"},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 400)  # tijd en hele dag gemengd
                async with s.post("http://127.0.0.1:18811/api/event",
                                  json={"id": "abc", "titel": "Tandarts", "start": "2026-09-10T10:00", "eind": "2026-09-10T10:30"},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 502)  # geen Google in de test
                async with s.post("http://127.0.0.1:18811/api/event", json={"actie": "nieuw", "titel": "X"},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 400)  # nieuw zonder datum
                async with s.post("http://127.0.0.1:18811/api/event", json={"actie": "verwijder"},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 400)  # verwijderen zonder id
                (cfg.workspace / "memory" / "planning.json").unlink(missing_ok=True)  # schone lei, ook na een eerdere run
                async with s.get("http://127.0.0.1:18811/api/plan", headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 200)
                    self.assertEqual((await r.json())["bijgewerkt"], 0)  # nog niets gedeeld
                async with s.post("http://127.0.0.1:18811/api/plan", json={"plan": "tekst"},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 400)
                async with s.post("http://127.0.0.1:18811/api/plan",
                                  json={"plan": {"datum": "2026-09-02", "gekozen": [0], "start": 1}, "taken": {"ochtend": []}},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 200)
                    ts = (await r.json())["bijgewerkt"]
                async with s.get("http://127.0.0.1:18811/api/plan", headers={"X-Dashboard-Key": "geheim"}) as r:
                    pj = await r.json()
                    self.assertEqual(pj["plan"]["gekozen"], [0]); self.assertEqual(pj["bijgewerkt"], ts)
                async with s.get("http://127.0.0.1:18811/api/geld", headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 403)  # geen pincode ingesteld in de test
                async with s.get("http://127.0.0.1:18811/api/verreken", headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 403)  # zonder pincode
                async with s.get("http://127.0.0.1:18811/api/transacties", headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 403)  # zonder pincode
                async with s.get("http://127.0.0.1:18811/manifest.webmanifest") as r:
                    self.assertEqual(r.status, 200)
                async with s.get("http://127.0.0.1:18811/sw.js") as r:
                    self.assertEqual(r.status, 200)
                async with s.get("http://127.0.0.1:18811/geheim.txt") as r:
                    self.assertEqual(r.status, 404)
                async with s.post("http://127.0.0.1:18811/api/message",
                                  json={"text": "voeg kwark toe"},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual((await r.json())["reply"], "OK-ANTWOORD")
                async with s.post("http://127.0.0.1:18811/api/done", json={"id": "123"}) as r:
                    self.assertEqual(r.status, 401)  # zonder sleutel
                async with s.post("http://127.0.0.1:18811/api/done", json={"id": ""},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 400)  # leeg id
                async with s.post("http://127.0.0.1:18811/api/done", json={"id": "123"},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 502)  # geen Todoist-token in de test
                async with s.post("http://127.0.0.1:18811/api/add",
                                  json={"lijst": "werk", "tekst": "x"},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 400)  # alleen boodschappen/acties
                async with s.post("http://127.0.0.1:18811/api/add",
                                  json={"lijst": "acties", "tekst": "band plakken"},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 502)  # geen Todoist-token in de test
                async with s.post("http://127.0.0.1:18811/api/due",
                                  json={"id": "123", "datum": "morgen"},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 400)  # alleen JJJJ-MM-DD
                async with s.post("http://127.0.0.1:18811/api/due",
                                  json={"id": "123", "datum": "2026-09-05"},
                                  headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 502)  # geen Todoist-token in de test
            self.assertEqual(brain.calls[0][2]["text"], "voeg kwark toe")
        finally:
            await d.stop()

    def test_cache_patches(self):
        from agent.dashboard import Dashboard
        c = {"boodschappen": [{"id": "1", "tekst": "kwark", "due": ""}],
             "boodschappen_af": [], "acties": [], "acties_af": []}
        Dashboard._patch_afgevinkt(c, "1")
        self.assertEqual(c["boodschappen"], [])
        self.assertEqual(c["boodschappen_af"][0]["tekst"], "kwark")
        Dashboard._patch_heropend(c, "1")
        self.assertEqual(c["boodschappen"][0]["tekst"], "kwark")
        self.assertEqual(c["boodschappen_af"], [])

    def test_onderwerpen_parse(self):
        from datetime import date
        from agent.bronnen import onderwerpen_parse
        vandaag = date(2026, 9, 2)
        items = onderwerpen_parse(
            "WAT LOOPT ER\n\nGrotere onderwerpen met een eigenaar.\n\n"
            "• Kinderfeest Evi — wie: Jaap · wanneer: 06-09 · stap: gastenlijst invullen\n"
            "• KPN moeder — wie: Jaap · wanneer: n.t.b. · notitie: Youfone vanaf €42\n"
            "• Cadeau Evi — wie: Jaap · wanneer: 31-08-2026 · stap: checken\n\n"
            "Afgerond\n• Oppas geregeld — wie: Jaap\n", vandaag)
        self.assertEqual([o["naam"] for o in items], ["Cadeau Evi", "Kinderfeest Evi", "KPN moeder"])
        self.assertEqual(items[0]["dagen"], -2)
        self.assertEqual(items[1]["dagen"], 4)
        self.assertEqual(items[1]["stap"], "gastenlijst invullen")
        self.assertIsNone(items[2]["dagen"])
        self.assertEqual(items[2]["notitie"], "Youfone vanaf €42")

    def test_signalen(self):
        from datetime import date
        from agent.signalen import bereken
        vandaag = date(2026, 9, 2)
        sig = bereken(
            acties=[{"tekst": "band plakken", "due": "2026-08-30"}, {"tekst": "cadeau Avie", "due": "2026-09-02"}],
            regelzaken=[{"naam": "Kapper Evi", "wie": "Jaap", "dagen": -3}],
            verjaardagen=[{"naam": "Avie", "dagen": 1, "notitie": ""}, {"naam": "Oma", "dagen": 3, "notitie": "boek"}],
            week=[{"start": "2026-09-02T11:00", "eind": "2026-09-02T12:00", "titel": "Feestje"},
                  {"start": "2026-09-02T11:30", "eind": "2026-09-02T13:00", "titel": "Bezoek oma"},
                  {"start": "2026-09-02", "eind": "2026-09-03", "titel": "Hele dag"}],
            onderwerpen=[{"naam": "Kinderfeest", "dagen": 0, "stap": "gastenlijst"}],
            vandaag=vandaag)
        teksten = [s["tekst"] for s in sig]
        self.assertTrue(any(t.startswith("1 actie over de datum") for t in teksten))
        self.assertFalse(any(t.startswith("Vandaag:") for t in teksten))  # staat al in de actiekolom
        self.assertIn("📂 Kinderfeest: vandaag — gastenlijst", teksten)
        self.assertIn("🔁 Kapper Evi is 3 dagen over tijd (Jaap)", teksten)
        self.assertIn("🎂 Avie morgen, nog geen cadeau-idee", teksten)
        self.assertTrue(any(t.startswith("⚠️ Overlap vandaag 11:00") for t in teksten))
        self.assertFalse(any("Oma" in t for t in teksten))

    def test_tijden_lokaal(self):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        from agent.gcal import _lokaal, _tijdvak
        # UTC uit een API → NL-zomertijd (+2)
        self.assertEqual(_lokaal(datetime(2026, 9, 6, 11, 0, tzinfo=timezone.utc)).hour, 13)
        # Europe/Paris uit een iCal → zelfde wandklok als Amsterdam
        self.assertEqual(_lokaal(datetime(2026, 9, 6, 13, 0, tzinfo=ZoneInfo("Europe/Paris"))).hour, 13)
        self.assertEqual(_tijdvak("2026-09-06T13:00:00+02:00", "2026-09-06T16:00:00+02:00"),
                         "2026-09-06 13:00–16:00")
        self.assertEqual(_tijdvak("2026-09-06T11:00:00Z", "2026-09-06T14:00:00Z"), "2026-09-06 13:00–16:00")
        self.assertEqual(_tijdvak("2026-09-07", None), "2026-09-07")

    def test_ics_feeds(self):
        import os
        from agent.gcal import ics_feeds
        oud = {k: os.environ.get(k) for k in ("FAMILYWALL_ICS_URL", "AGENDA_ICS_FEEDS")}
        try:
            os.environ["FAMILYWALL_ICS_URL"] = "https://fw.example/x.ics"
            os.environ["AGENDA_ICS_FEEDS"] = "Volleybal DS3|http://api.nevobo.nl/a.ics; https://school.example/k.ics ;"
            self.assertEqual(ics_feeds(), [("FamilyWall", "https://fw.example/x.ics"),
                                           ("Volleybal DS3", "http://api.nevobo.nl/a.ics"),
                                           ("school.example", "https://school.example/k.ics")])
            os.environ["FAMILYWALL_ICS_URL"] = ""
            os.environ["AGENDA_ICS_FEEDS"] = ""
            self.assertEqual(ics_feeds(), [])
        finally:
            for k, v in oud.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_sync_plan(self):
        from agent.gcal import _sync_plan
        ics = {
            "u1@2026-09-25T20:00": {"summary": "Spirit DS 3 - Nuvoc DS 4", "location": "Rodenborch", "description": "",
                                    "start": {"dateTime": "2026-09-25T20:00:00", "timeZone": "Europe/Amsterdam"},
                                    "end": {"dateTime": "2026-09-25T22:00:00", "timeZone": "Europe/Amsterdam"}},
            "u2@2026-10-01T20:30": {"summary": "VC Blox DS 1 - Spirit DS 3", "location": "Boxtel", "description": "",
                                    "start": {"dateTime": "2026-10-01T20:30:00", "timeZone": "Europe/Amsterdam"},
                                    "end": {"dateTime": "2026-10-01T22:30:00", "timeZone": "Europe/Amsterdam"}},
        }
        google = [
            {"id": "g1", "summary": "Spirit DS 3 - Nuvoc DS 4", "location": "Rodenborch",
             "start": {"dateTime": "2026-09-25T20:00:00+02:00"}, "end": {"dateTime": "2026-09-25T22:00:00+02:00"},
             "extendedProperties": {"private": {"birdy_sync": "V", "birdy_sync_key": "u1@2026-09-25T20:00"}}},
            {"id": "g3", "summary": "Afgelaste wedstrijd",
             "start": {"dateTime": "2026-10-08T20:00:00+02:00"}, "end": {"dateTime": "2026-10-08T22:00:00+02:00"},
             "extendedProperties": {"private": {"birdy_sync": "V", "birdy_sync_key": "u3@2026-10-08T20:00"}}},
        ]
        aanmaken, bijwerken, verwijderen = _sync_plan("V", ics, google)
        self.assertEqual([b["summary"] for b in aanmaken], ["VC Blox DS 1 - Spirit DS 3"])
        self.assertEqual(bijwerken, [])
        self.assertEqual(verwijderen, ["g3"])
        self.assertEqual(aanmaken[0]["extendedProperties"]["private"]["birdy_sync"], "V")
        # tijd verschoven in de feed → bijwerken
        ics["u1@2026-09-25T20:00"]["start"]["dateTime"] = "2026-09-25T19:30:00"
        _, bijwerken, _ = _sync_plan("V", ics, google)
        self.assertEqual([i for i, _ in bijwerken], ["g1"])

    def test_financien_register(self):
        from datetime import date
        from openpyxl import Workbook
        from agent import financien
        wb = Workbook(); wb.remove(wb.active)
        for tab, kolommen in financien.TABS.items():
            wb.create_sheet(tab).append(kolommen)
        wb["Vaste lasten"].append(["Energie", "Wonen", 180, "maand", "gezamenlijk", "gezamenlijk", 1, "", "", "", ""])
        wb["Vaste lasten"].append(["Netflix", "Abonnementen", "€ 15,99", "maand", "Jaap", "gezamenlijk", 12, "1 maand", "", "20-09-2026", "", "", "", "", ""])
        wb["Vaste lasten"].append(["Sportschool Yvette", "Sport", 600, "jaar", "gezamenlijk", "Yvette", "", "", "", "", ""])
        wb["Polissen"].append(["Autoverzekering", "ANWB", "WA+", 62, "maand", 150, "Jaap", "gezamenlijk", "", "31-12-2026", "", "1 maand", "", "", "", ""])
        wb["Hypotheek"].append(["Deel 1", "ING", 300000, 250000, 3.0, "01-12-2026", "annuïteit", 1450, "", "", "", "", ""])
        wb["Geldstromen"].append(["Lening vader", "uit", 600, "maand", "gezamenlijk", "vader", "gezamenlijk", "Familie", "betaling"])
        wb["Geldstromen"].append(["Lening vader", "in", 1800, "kwartaal", "vader", "gezamenlijk", "gezamenlijk", "Familie", "komt terug"])
        wb["Variabele kosten"].append(["Boodschappen", 600, "maand", "gezamenlijk", "gezamenlijk", "", ""])
        wb["Incidenteel"].append(["Vakantiegeld", "inkomst", 3000, "mei", "ja", "Jaap", "Jaap", ""])
        wb["Incidenteel"].append(["Vakantie", "uitgave", 3600, "juli", "ja", "Jaap", "gezamenlijk", ""])
        wb["Incidenteel"].append(["Aanslag", "uitgave", 500, "feb", "nee", "Jaap", "Jaap", ""])
        wb["Vaste lasten"].append(["Efteling", "Uitjes", 33.50, "2 maanden", "gezamenlijk", "gezamenlijk", "", "", "", "", "", "", "", "", ""])
        wb["Vaste lasten"].append(["Energie", "Wonen", 254, "maand", "Jaap", "gezamenlijk", "", "", "", "", "01-10-2026", "", "inleg", "", ""])
        wb["Inkomsten"].append(["Salaris Jaap", 5000, "maand", "Jaap", "Jaap", "", "", ""])
        r = financien.register(wb, vandaag=date(2026, 9, 2))
        self.assertTrue(r["beschikbaar"])
        self.assertEqual(r["totalen"]["variabel_pm"], 600.0)
        self.assertEqual(r["totalen"]["reservering_pm"], 50.0)  # (3600 − 3000) / 12; onvoorspelbaar telt niet
        self.assertEqual({l["naam"]: l["per_maand"] for l in r["vaste_lasten"]}["Efteling"], 16.75)
        self.assertEqual([x["wat"] for x in r["via_inleg"]], ["Energie"])  # zit in de inleg → niet in verrekening
        self.assertTrue(any("Energie" in t and "overstap" in t for t in [s["tekst"] for s in r["signalen"]]))
        self.assertEqual(r["totalen"]["over_pm"], round(5000 - (r["totalen"]["vast_pm"] - r["totalen"]["in_pm"]) - 600, 2))
        pm = {l["naam"]: l["per_maand"] for l in r["vaste_lasten"]}
        self.assertEqual(pm["Netflix"], 15.99); self.assertEqual(pm["Sportschool Yvette"], 50.0)
        self.assertEqual(r["per_categorie"]["Wonen"], 434.0)  # energie 180 + 254
        c = r["constructies"][0]
        self.assertEqual((c["uit_pm"], c["in_pm"], c["netto_pm"]), (600.0, 600.0, 0.0))
        h = r["hypotheek"]; self.assertEqual(h["rente"], 625.0); self.assertEqual(h["aflossing"], 825.0)
        # verrekening (pot-model): Jaap betaalde 15.99 + 62 voor de pot → pot is Jaap 77.99 schuldig;
        # de pot betaalde Yvettes sportschool (50) → Yvette is de pot 50 schuldig
        v = r["verrekening"]; self.assertEqual(v["personen"], ["Jaap", "Yvette"])
        self.assertAlmostEqual(v["saldo"]["Jaap"], 77.99, places=2); self.assertAlmostEqual(v["saldo"]["Yvette"], -50.0, places=2)
        self.assertIn("de pot is Jaap € 78", v["tekst"]); self.assertIn("Yvette is de pot € 50", v["tekst"])
        teksten = [s["tekst"] for s in r["signalen"]]
        self.assertTrue(any("Netflix" in t and "op te zeggen" in t for t in teksten))  # 20-09 − 1 maand ≈ nu
        self.assertTrue(any("Rentevaste periode" in t for t in teksten))          # 01-12 binnen 180 dagen
        self.assertFalse(any("Autoverzekering" in t for t in teksten))           # 31-12 − 1 maand > 21 dagen
        self.assertEqual(r["totalen"]["in_pm"], 600.0)

    async def test_verrekenen(self):
        from agent.dashboard import Dashboard
        cfg = Config(dashboard_token="geheim", dashboard_port=18813, dashboard_geld_pin="1234")
        d = Dashboard(cfg, FakeBrain(), asyncio.Lock(), [])
        (cfg.workspace / "memory" / "verrekenen.json").unlink(missing_ok=True)
        await d.start()
        try:
            import aiohttp
            H = {"X-Dashboard-Key": "geheim", "X-Geld-Pin": "1234"}
            async with aiohttp.ClientSession() as s:
                async with s.post("http://127.0.0.1:18813/api/verreken", json={"actie": "toevoegen", "wie": "Jaap", "bedrag": "12,50", "omschrijving": "luiers", "richting": "voor_pot"}, headers=H) as r:
                    self.assertEqual(r.status, 200)
                async with s.post("http://127.0.0.1:18813/api/verreken", json={"actie": "toevoegen", "wie": "Yvette", "bedrag": 30, "omschrijving": "kapper", "richting": "uit_pot"}, headers=H) as r:
                    self.assertEqual(r.status, 200)
                async with s.post("http://127.0.0.1:18813/api/verreken", json={"actie": "toevoegen", "wie": "Jaap", "bedrag": 0}, headers=H) as r:
                    self.assertEqual(r.status, 400)
                async with s.post("http://127.0.0.1:18813/api/verreken", json={"actie": "afrekenen"}, headers=H) as r:
                    a = (await r.json())["afrekening"]
                    self.assertEqual(a["saldo"], {"Jaap": 12.5, "Yvette": -30.0}); self.assertIn("pot → Jaap: € 12.50", a["tekst"])
                async with s.get("http://127.0.0.1:18813/api/verreken", headers=H) as r:
                    vr = await r.json()
                    self.assertTrue(all(p["verrekend"] for p in vr["posten"])); self.assertEqual(len(vr["afrekeningen"]), 1)
                async with s.post("http://127.0.0.1:18813/api/verreken", json={"actie": "afrekenen"}, headers=H) as r:
                    self.assertEqual(r.status, 400)  # niets open
                async with s.post("http://127.0.0.1:18813/api/besparingen", json={"actie": "toevoegen", "voorstel": "Wellis stoppen", "per_maand": "249"}, headers=H) as r:
                    bid = (await r.json())["items"][-1]["id"]
                async with s.post("http://127.0.0.1:18813/api/besparingen", json={"actie": "status", "id": bid, "status": "gedaan"}, headers=H) as r:
                    self.assertEqual((await r.json())["items"][-1]["status"], "gedaan")
                async with s.post("http://127.0.0.1:18813/api/besparingen", json={"actie": "verwijderen", "id": bid}, headers=H) as r:
                    self.assertEqual((await r.json())["items"], [])
                fd = aiohttp.FormData(); fd.add_field("bestand", b"%PDF-1.4 test", filename="polis.pdf"); fd.add_field("hint", "autoverzekering")
                async with s.post("http://127.0.0.1:18813/api/upload", data=fd, headers={"X-Dashboard-Key": "geheim"}) as r:
                    self.assertEqual(r.status, 200); up = await r.json()
                    self.assertEqual(up["reply"], "OK-ANTWOORD"); self.assertTrue(up["pad"].startswith("inbox/docs/"))
                    self.assertTrue((cfg.workspace / up["pad"]).exists()); (cfg.workspace / up["pad"]).unlink()
        finally:
            await d.stop()
            (cfg.workspace / "memory" / "verrekenen.json").unlink(missing_ok=True)
            (cfg.workspace / "memory" / "besparingen.json").unlink(missing_ok=True)

    def test_bank_parsers_en_categorien(self):
        from pathlib import Path
        from agent import bank
        csv_data = ('"Date","Name / Description","Account","Counterparty","Code","Debit/credit","Amount (EUR)","Transaction type","Notifications"\n'
                    '"20260901","Albert Heijn 1219","NL20INGB0688967574","","BA","Debit","3,57","Payment terminal","Card no: x"\n'
                    '"20260826","J BAKKER","NL20INGB0688967574","NL52ABNA0541363123","OV","Credit","4300,00","Transfer","Name: J BAKKER Description: Maandelijkse inleg Jaap"\n'
                    '"20260826","Smallsteps B.V.","NL20INGB0688967574","NL51RABO0307495248","IC","Debit","2651,70","SEPA direct debit","Smallsteps 09-2026"\n').encode()
        rek, tx = bank.parse_bestand("export.csv", csv_data, "ING gezamenlijk")
        self.assertEqual(rek, "ING gezamenlijk"); self.assertEqual(len(tx), 3)
        self.assertEqual([t["bedrag"] for t in tx], [-3.57, 4300.0, -2651.7])
        register = {"beschikbaar": True, "rekeningen": [{"iban": "NL52ABNA0541363123"}],
                    "vaste_lasten": [{"naam": "Kinderopvang Smallsteps", "categorie": "Kinderen", "herkenning": "NL51RABO0307495248"}],
                    "polissen": [], "variabel": [{"naam": "Boodschappen (Picnic, AH, Jumbo)", "herkenning": "Picnic|Albert Heijn"}], "inkomsten": []}
        bank.categoriseer(tx, register)
        self.assertEqual([t["categorie"] for t in tx], ["Boodschappen (Picnic, AH, Jumbo)", "Overboeking eigen rekeningen", "Kinderen"])
        self.assertEqual(tx[2]["post"], "Kinderopvang Smallsteps")
        ws = Path(os.environ["AGENT_WORKSPACE"]); (ws / "memory" / "transacties.json").unlink(missing_ok=True)
        stand = bank.voeg_toe(ws, tx, register); self.assertEqual((stand["toegevoegd"], stand["totaal"]), (3, 3))
        stand = bank.voeg_toe(ws, tx, register); self.assertEqual((stand["toegevoegd"], stand["dubbel"]), (0, 3))
        sam = bank.samenvatting(bank.laad(ws), 6, categorie="Kinderen")
        self.assertEqual(sam["aantal"], 1); self.assertEqual(sam["partijen"][0]["naam"], "Smallsteps B.V.")
        cats = bank.categorieen(bank.laad(ws), 6)
        self.assertTrue(any(c["categorie"] == "Kinderen" for c in cats)); self.assertFalse(any("Overboeking" in c["categorie"] for c in cats))
        (ws / "memory" / "transacties.json").unlink(missing_ok=True)

    def test_dubbelingen(self):
        from agent.signalen import dubbelingen
        acties = [{"tekst": "Cadeau kopen voor Evi's verjaardag"},
                  {"tekst": "Kinderfeest Evi: gastenlijst invullen"},
                  {"tekst": "Dierenarts bellen voor de kat"}]
        onderwerpen = [{"naam": "Cadeau voor Evi's verjaardag"}, {"naam": "Kinderfeest Evi organiseren"},
                       {"naam": "Kattenrollen voor op de schutting"}]
        self.assertEqual(dubbelingen(acties, onderwerpen),
                         [("Cadeau kopen voor Evi's verjaardag", "Cadeau voor Evi's verjaardag")])

    def test_gdrive_query_escaping(self):
        from agent import gdrive
        self.assertEqual(gdrive._q("Huis & tuin's"), "Huis & tuin\\'s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
