"""Kiosk-dashboard voor de muurtablet (en telefoons).

Draait als aiohttp-server in de agent-container, alleen op localhost van de VPS;
ontsluiting gebeurt via Tailscale (tailscale serve → HTTPS, nodig voor de microfoon).
Aan/uit via DASHBOARD_TOKEN in .env: leeg = dashboard uit.

- GET  /                → kioskpagina (dark, auto-verversend, microfoonknop)
- GET  /api/overview    → agenda, overzicht, lijstjes, verjaardagen (JSON, cache 2 min)
- POST /api/message     → {"text": ...} → zelfde brein als de chat; antwoord terug + echo in Slack
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from aiohttp import web

STATIC_DIR = Path(__file__).parent / "static"

from .brain import Brain
from .config import Config

log = logging.getLogger("fien.dashboard")

CACHE_TTL = 120  # seconden


def _agenda_rijk(days: int = 7) -> list[dict]:
    """Afspraken mét eindtijd, voor de weekweergave. [{start, eind, titel}] (ISO)."""
    events: list[dict] = []
    try:
        from . import gcal
        if os.environ.get("GOOGLE_CALENDAR_ID"):
            svc = gcal._service()
            now = datetime.now()
            resp = svc.events().list(
                calendarId=os.environ["GOOGLE_CALENDAR_ID"],
                timeMin=now.astimezone().isoformat(),
                timeMax=(now + timedelta(days=days)).astimezone().isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=60,
            ).execute()
            for ev in resp.get("items", []):
                s, e = ev.get("start", {}), ev.get("end", {})
                events.append({
                    "start": (s.get("dateTime") or s.get("date", ""))[:16],
                    "eind": (e.get("dateTime") or e.get("date", ""))[:16],
                    "titel": ev.get("summary", "(zonder titel)"),
                })
    except BaseException:  # SystemExit van de CLI-helpers telt ook
        pass
    try:
        url = os.environ.get("FAMILYWALL_ICS_URL", "")
        if url:
            import icalendar
            import recurring_ical_events
            import requests

            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            cal = icalendar.Calendar.from_ical(resp.content)
            now = datetime.now()

            def iso(v) -> str:
                if isinstance(v, datetime):
                    return v.strftime("%Y-%m-%dT%H:%M")
                return v.strftime("%Y-%m-%d") if v else ""

            for ev in recurring_ical_events.of(cal).between(now, now + timedelta(days=days)):
                start = ev.get("DTSTART").dt
                eind = ev.get("DTEND")
                events.append({
                    "start": iso(start),
                    "eind": iso(eind.dt if eind else None) or iso(start),
                    "titel": str(ev.get("SUMMARY", "(zonder titel)")),
                })
    except BaseException:
        pass
    # zelfde moment + titel uit beide bronnen → één keer
    seen, uniek = set(), []
    for ev in sorted(events, key=lambda e: e["start"]):
        key = (ev["start"], ev["titel"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        uniek.append(ev)
    return uniek[:60]


def _agenda_compact(rijk: list[dict]) -> list[dict]:
    return [{
        "wanneer": ev["start"].replace("T", " "),
        "titel": ev["titel"],
    } for ev in rijk][:22]


def _overzicht_kort(text: str) -> dict:
    """OVERZICHT.md (chat-format) samenvatten: urgente en deze-week-items + rest-telling."""
    secties: dict[str, list[str]] = {"🔴": [], "🟠": [], "🟡": [], "⏳": []}
    huidig = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        kop = next((k for k in secties if s.startswith(k)), None)
        if kop:
            huidig = kop
            continue
        if s[0] in "🔴🟠🟡⏳✅📋":  # andere kop (bijv. ✅) → sectie sluiten
            huidig = None
            continue
        if huidig and s.startswith(("•", "-", "*")):
            item = s.lstrip("•-* ").strip()
            if item.startswith("(") or item.lower().startswith("niets"):
                continue  # plaatshouders als "(niets — goed nieuws …)" niet tonen
            secties[huidig].append(item)
    rest = []
    if secties["🟡"]:
        rest.append(f"{len(secties['🟡'])} voor later")
    if secties["⏳"]:
        rest.append(f"{len(secties['⏳'])} wachten op")
    return {"urgent": secties["🔴"][:5], "week": secties["🟠"][:5], "rest": " · ".join(rest)}


def _todoist_lijst(naam: str) -> list[dict]:
    from . import todoist

    try:
        project = todoist._project(naam)
        tasks = todoist._list_all("/tasks", {"project_id": project["id"]})
        out = [{
            "id": str(t["id"]),
            "tekst": t["content"],
            "due": ((t.get("due") or {}).get("date") or "")[:10],
        } for t in tasks]
        out.sort(key=lambda t: (t["due"] == "", t["due"]))  # deadlines eerst, oplopend
        return out[:12]
    except BaseException:
        return []


def _todoist_afvinken(task_id: str) -> bool:
    from . import todoist

    try:
        todoist._request("POST", f"/tasks/{task_id}/close")
        return True
    except BaseException:
        return False


def _todoist_afgevinkt(naam: str) -> list[dict]:
    """Onlangs afgevinkte taken (7 dagen) van een project, voor de herstel-lijst."""
    from datetime import timezone

    from . import todoist

    try:
        project = todoist._project(naam)
        nu = datetime.now(timezone.utc)
        data = todoist._request("GET", "/tasks/completed/by_completion_date", params={
            "project_id": project["id"],
            "since": (nu - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "until": nu.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        items = data.get("items") or data.get("results") or []
        return [{"id": str(t["id"]), "tekst": t["content"]} for t in items][:6]
    except BaseException:
        return []


def _todoist_heropen(task_id: str) -> bool:
    from . import todoist

    try:
        todoist._request("POST", f"/tasks/{task_id}/reopen")
        return True
    except BaseException:
        return False


def _todoist_deadline(task_id: str, datum: str) -> bool:
    from . import todoist

    try:
        todoist._request("POST", f"/tasks/{task_id}", json={"due_date": datum})
        return True
    except BaseException:
        return False


def _todoist_toevoegen(lijst: str, tekst: str) -> bool:
    from . import todoist

    try:
        project = todoist._project(lijst)
        todoist._request("POST", "/tasks", json={"content": tekst, "project_id": project["id"]})
        return True
    except BaseException:
        return False


def _verjaardagen() -> list[dict]:
    from . import gdrive

    try:
        svc = gdrive._service()
        node = gdrive._resolve(svc, "20 Huishouden/Verjaardagen", must_exist=False)
        if not node:
            return []
        text = svc.files().export(fileId=node["id"], mimeType="text/plain").execute()
        text = text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text
    except BaseException:
        return []
    vandaag = date.today()
    out = []
    for line in text.splitlines():
        clean = line.strip().lstrip("•-* ").strip()
        if len(clean) < 6 or not clean[:5].replace("-", "").isdigit():
            continue
        try:
            dd, mm = int(clean[:2]), int(clean[3:5])
            volgende = date(vandaag.year, mm, dd)
            if volgende < vandaag:
                volgende = date(vandaag.year + 1, mm, dd)
        except ValueError:
            continue
        naam = clean[5:].split("·")[0].strip(" ·—-")
        out.append({"datum": clean[:5], "naam": naam, "dagen": (volgende - vandaag).days})
    return sorted(out, key=lambda x: x["dagen"])[:5]


class Dashboard:
    def __init__(self, cfg: Config, brain: Brain, work_lock: asyncio.Lock, adapters: list):
        self.cfg = cfg
        self.brain = brain
        self.work_lock = work_lock
        self.adapters = adapters
        self._cache: dict | None = None
        self._cache_ts = 0.0
        self._gen = 0  # verhoogd bij elke mutatie: een lopende (oude) opbouw mag dan niet meer cachen
        self._traag: dict | None = None  # langzame bronnen (agenda/FamilyWall/Drive), eigen cache
        self._traag_ts = 0.0
        self._bouw_lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None

    def _invalidate(self, ook_traag: bool = False) -> None:
        self._cache = None
        self._gen += 1
        if ook_traag:
            self._traag = None

    # -- levenscyclus -------------------------------------------------------

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self.page)
        app.router.add_get("/api/overview", self.overview)
        app.router.add_post("/api/message", self.message)
        app.router.add_post("/api/done", self.done)
        app.router.add_post("/api/add", self.add)
        app.router.add_post("/api/due", self.due)
        app.router.add_post("/api/reopen", self.reopen)
        app.router.add_get("/logo.png", self.logo)
        app.router.add_get("/logo-bird.png", self.logo)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.cfg.dashboard_port)
        await site.start()
        log.info("Dashboard draait op poort %d", self.cfg.dashboard_port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def broadcast(self, text: str, kind: str = "briefing") -> None:
        return  # het dashboard toont data, hij ontvangt geen broadcasts

    # -- helpers ------------------------------------------------------------

    def _authorized(self, request: web.Request) -> bool:
        key = request.headers.get("X-Dashboard-Key") or request.query.get("key", "")
        return bool(self.cfg.dashboard_token) and key == self.cfg.dashboard_token

    # -- routes -------------------------------------------------------------

    async def page(self, request: web.Request) -> web.Response:
        return web.Response(text=PAGE, content_type="text/html")

    async def logo(self, request: web.Request) -> web.Response:
        naam = "logo-bird.png" if request.path.endswith("logo-bird.png") else "logo.png"
        pad = STATIC_DIR / naam
        if not pad.exists():
            raise web.HTTPNotFound()  # de pagina valt dan terug op het vogel-emoji
        return web.Response(body=pad.read_bytes(), content_type="image/png",
                            headers={"Cache-Control": "max-age=86400"})

    async def overview(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        if self._cache and time.time() - self._cache_ts <= CACHE_TTL:
            data = dict(self._cache)
        else:
            async with self._bouw_lock:
                if self._cache and time.time() - self._cache_ts <= CACHE_TTL:
                    data = dict(self._cache)
                else:
                    data = await self._bouw()
        data["nu"] = datetime.now().strftime("%A %d %B · %H:%M")
        return web.json_response(data)

    async def _bouw(self) -> dict:
        gen = self._gen
        if not self._traag or time.time() - self._traag_ts > CACHE_TTL:
            week, jarigen = await asyncio.gather(
                asyncio.to_thread(_agenda_rijk),
                asyncio.to_thread(_verjaardagen),
            )
            self._traag = {"week": week, "verjaardagen": jarigen}
            self._traag_ts = time.time()
        boodschappen, acties, boodschappen_af, acties_af = await asyncio.gather(
            asyncio.to_thread(_todoist_lijst, "boodschappen"),
            asyncio.to_thread(_todoist_lijst, "acties"),
            asyncio.to_thread(_todoist_afgevinkt, "boodschappen"),
            asyncio.to_thread(_todoist_afgevinkt, "acties"),
        )
        overzicht_pad = self.cfg.workspace / "OVERZICHT.md"
        overzicht = overzicht_pad.read_text() if overzicht_pad.exists() else ""
        vers = {
            "agenda": _agenda_compact(self._traag["week"]),
            "week": self._traag["week"],
            "personen": self.cfg.dashboard_personen,
            "taken": _overzicht_kort(overzicht),
            "boodschappen": boodschappen,
            "acties": acties,
            "boodschappen_af": boodschappen_af,
            "acties_af": acties_af,
            "verjaardagen": self._traag["verjaardagen"],
        }
        if gen == self._gen:  # geen mutatie tijdens het bouwen → cachen mag
            self._cache = vers
            self._cache_ts = time.time()
        return dict(vers)

    async def done(self, request: web.Request) -> web.Response:
        return await self._taak_actie(request, _todoist_afvinken)

    async def reopen(self, request: web.Request) -> web.Response:
        return await self._taak_actie(request, _todoist_heropen)

    async def _taak_actie(self, request: web.Request, actie) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        try:
            body = await request.json()
            task_id = str(body.get("id", "")).strip()
        except Exception:
            task_id = ""
        if not task_id or len(task_id) > 40:
            return web.json_response({"error": "geen taak-id"}, status=400)
        ok = await asyncio.to_thread(actie, task_id)
        if ok:
            self._invalidate()
        return web.json_response({"ok": ok}, status=200 if ok else 502)

    async def due(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        try:
            body = await request.json()
            task_id = str(body.get("id", "")).strip()
            datum = str(body.get("datum", "")).strip()
        except Exception:
            task_id, datum = "", ""
        geldig = (len(datum) == 10 and datum[4] == "-" and datum[7] == "-"
                  and datum.replace("-", "").isdigit())
        if not task_id or len(task_id) > 40 or not geldig:
            return web.json_response({"error": "id of datum ongeldig"}, status=400)
        ok = await asyncio.to_thread(_todoist_deadline, task_id, datum)
        if ok:
            self._invalidate()
        return web.json_response({"ok": ok}, status=200 if ok else 502)

    async def add(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        try:
            body = await request.json()
            lijst = str(body.get("lijst", "")).strip().lower()
            tekst = str(body.get("tekst", "")).strip()[:200]
        except Exception:
            lijst, tekst = "", ""
        if lijst not in ("boodschappen", "acties") or not tekst:
            return web.json_response({"error": "lijst of tekst ontbreekt"}, status=400)
        ok = await asyncio.to_thread(_todoist_toevoegen, lijst, tekst)
        if ok:
            self._invalidate()
        return web.json_response({"ok": ok}, status=200 if ok else 502)

    async def message(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "geen json"}, status=400)
        text = str(body.get("text", "")).strip()[:1000]
        if not text:
            return web.json_response({"error": "leeg bericht"}, status=400)

        async with self.work_lock:
            reply = await self.brain.run(
                "process_message.md", "dashboard-bericht",
                sender="het dashboard (muurtablet)", text=text, photo="(geen bijlage)",
            )
        reply = reply or "Hm, daar ging iets mis — probeer het nog eens?"
        self._invalidate(ook_traag=True)  # het brein kan ook agenda/documenten hebben aangepast
        for adapter in self.adapters:
            if adapter is not self:
                try:
                    await adapter.broadcast(f"🎤 Dashboard: “{text}”\n{reply}", kind="chat")
                except Exception:
                    pass
        return web.json_response({"reply": reply})


PAGE = """<!doctype html>
<html lang="nl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/logo-bird.png">
<title>Birdy</title>
<style>
  :root { --bg:#14171a; --panel:#1d2126; --ink:#e8e6df; --dim:#8b948f; --accent:#7fbfa6;
          --amber:#d9a44e; --rood:#e07a6a; --lijn:#2a2f35; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--ink); font-family:system-ui,-apple-system,sans-serif;
         padding:1.1rem; min-height:100vh; font-size:15px; }
  header { display:flex; align-items:center; gap:1.2rem; margin-bottom:.85rem; }
  header h1 { font-size:1.3rem; display:flex; align-items:center; gap:.5rem; }
  header h1 span { color:var(--accent); }
  header img { height:2.1rem; }
  #tabs { display:flex; gap:.35rem; background:var(--panel); border-radius:99px; padding:.25rem; }
  #tabs button { border:none; background:none; color:var(--dim); font-size:.92rem; cursor:pointer;
                 padding:.4rem 1rem; border-radius:99px; }
  #tabs button.actief { background:var(--accent); color:#14171a; font-weight:600; }
  #klok { color:var(--dim); font-size:1rem; text-transform:capitalize; margin-left:auto; }
  #tekstinvoer { display:flex; gap:.5rem; margin-bottom:1rem; }
  #tekstinvoer input { flex:1; background:var(--panel); border:1px solid #333a41; border-radius:12px;
                       color:var(--ink); padding:.75rem 1rem; font-size:1rem; }
  #tekstinvoer button { border:none; border-radius:12px; padding:0 1.1rem; font-size:1.2rem;
                        cursor:pointer; }
  #stuurknop { background:var(--accent); color:#14171a; }
  .micknop { background:var(--panel); border:1px solid #333a41 !important; }
  .micknop.luistert { background:var(--amber); animation:pulse 1.2s infinite; }
  @keyframes pulse { 50% { transform:scale(1.06); } }
  .grid { display:grid; gap:.9rem; grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
          align-items:start; }
  .kolom { display:flex; flex-direction:column; gap:.9rem; }
  .panel { background:var(--panel); border-radius:14px; padding:.9rem 1rem; }
  .panel h2 { font-size:.76rem; letter-spacing:.1em; text-transform:uppercase; color:var(--accent);
              margin-bottom:.55rem; }
  .panel h2.klik { cursor:pointer; }
  .panel ul { list-style:none; padding:0; }
  .panel li { padding:.24rem 0; font-size:.95rem; line-height:1.35; display:flex; gap:.5rem;
              align-items:baseline; }
  .panel li small { color:var(--dim); font-variant-numeric:tabular-nums; flex:0 0 3.1rem; }
  .panel li span { flex:1; min-width:0; }
  li.dag { font-size:.72rem; letter-spacing:.08em; text-transform:uppercase; color:var(--amber);
           border-top:1px solid var(--lijn); margin-top:.45rem; padding-top:.5rem; }
  li.dag:first-child { border-top:none; margin-top:0; padding-top:0; }
  li.urgent::before { content:"● "; color:var(--rood); }
  li.week::before { content:"● "; color:var(--amber); }
  .rest { color:var(--dim); font-size:.85rem; margin-top:.45rem; }
  li.vink { cursor:pointer; border-radius:8px; margin:0 -.4rem; padding:.3rem .4rem; }
  li.vink:active { background:rgba(127,191,166,.12); }
  li.vink::before { content:"◯"; color:var(--pc, var(--accent)); font-size:.9rem; }
  li.vink.gedaan { opacity:.4; text-decoration:line-through; pointer-events:none; }
  li.vink.gedaan::before { content:"✓"; }
  .toevoeg { margin-top:.5rem; }
  .toevoeg input { width:100%; background:transparent; border:none; border-top:1px solid var(--lijn);
                   color:var(--ink); padding:.5rem .2rem 0; font-size:.92rem; outline:none; }
  .toevoeg input::placeholder { color:var(--dim); }
  .due { display:inline-block; font-size:.66rem; padding:.05rem .42rem; margin-left:.4rem;
         border-radius:99px; white-space:nowrap; font-variant-numeric:tabular-nums; }
  .due.laat { background:rgba(224,122,106,.16); color:var(--rood); font-weight:600; }
  .due.nu { background:rgba(217,164,78,.16); color:var(--amber); font-weight:600; }
  .due.straks { border:1px solid var(--lijn); color:var(--dim); }
  .duebtn { flex:0 0 auto; align-self:center; width:1.35rem; height:1.35rem; border-radius:50%;
            border:1px dashed #3a4148; background:none; color:var(--dim); cursor:pointer;
            font-size:.9rem; line-height:1; padding:0; opacity:.7; }
  .duebtn:hover, .duebtn:active { color:var(--accent); border-color:var(--accent); opacity:1; }
  .leeg { color:var(--dim); font-style:italic; }
  #jarig li b { color:var(--amber); }
  /* ── weekweergave ── */
  #paneelWeek { display:none; }
  .wkwrap { overflow-x:auto; }
  .wk { display:grid; grid-template-columns:2.6rem repeat(7, minmax(120px,1fr)); gap:.35rem;
        min-width:920px; }
  .wk .kop { text-align:center; font-size:.74rem; letter-spacing:.06em; text-transform:uppercase;
             color:var(--dim); padding:.3rem 0; }
  .wk .kop.vandaag { color:var(--accent); font-weight:700; }
  .wk .heledag { min-height:1.6rem; display:flex; flex-direction:column; gap:.25rem; }
  .chip { font-size:.72rem; padding:.18rem .45rem; border-radius:7px; line-height:1.25;
          background:rgba(127,191,166,.14); border-left:3px solid var(--accent); cursor:pointer;
          overflow:hidden; }
  .tijdvak { position:relative; background:var(--panel); border-radius:10px; height:510px; }
  .uurlijn { position:absolute; left:0; right:0; border-top:1px solid rgba(255,255,255,.045); }
  .uuras { position:relative; height:510px; }
  .uuras div { position:absolute; right:.35rem; font-size:.66rem; color:var(--dim);
               transform:translateY(-50%); font-variant-numeric:tabular-nums; }
  .blok { position:absolute; left:3px; right:3px; border-radius:7px; padding:.15rem .4rem;
          font-size:.72rem; line-height:1.2; overflow:hidden; cursor:pointer;
          border-left:3px solid; }
  .blok b { display:block; font-weight:600; }
  .blok small { color:inherit; opacity:.75; font-size:.64rem; }
  .blok.conflict { outline:1.5px solid var(--rood); }
  .legenda { display:flex; gap:1rem; flex-wrap:wrap; margin:.6rem .2rem 0; font-size:.78rem;
             color:var(--dim); }
  .legenda i { display:inline-block; width:.7rem; height:.7rem; border-radius:3px;
               margin-right:.35rem; vertical-align:-1px; }
  #melding { position:fixed; left:1.2rem; bottom:1.2rem; max-width:min(380px,80vw);
             background:var(--panel); border:1px solid var(--amber); border-radius:12px;
             padding:.7rem .9rem; font-size:.92rem; display:none; z-index:60; }
  #melding button { margin-left:.6rem; border:none; border-radius:8px; padding:.3rem .7rem;
                    background:var(--accent); color:#14171a; font-size:.85rem; cursor:pointer;
                    font-weight:600; }
  .pers { display:inline-block; font-size:.64rem; padding:.04rem .4rem; margin-left:.4rem;
          border-radius:99px; vertical-align:baseline; }
  details.af { margin-top:.5rem; }
  details.af summary { font-size:.78rem; color:var(--dim); cursor:pointer; list-style:none;
                       padding-top:.4rem; border-top:1px solid var(--lijn); }
  details.af summary::before { content:"↩ "; }
  details.af li { color:var(--dim); text-decoration:line-through; font-size:.88rem; }
  .herstelknop { flex:0 0 auto; align-self:center; border:1px solid var(--lijn); background:none;
                 color:var(--accent); border-radius:8px; padding:.1rem .5rem; cursor:pointer;
                 font-size:.85rem; }
  #chatfab { position:fixed; right:1.2rem; bottom:1.2rem; width:64px; height:64px; border-radius:50%;
             border:none; background:var(--accent); cursor:pointer; z-index:40; font-size:1.7rem;
             box-shadow:0 4px 20px rgba(0,0,0,.45); display:flex; align-items:center;
             justify-content:center; }
  #chatfab img { height:2.3rem; }
  #chat { position:fixed; right:1.2rem; bottom:1.2rem; width:min(400px,92vw);
          height:min(580px,80vh); background:var(--panel); border:1px solid var(--lijn);
          border-radius:16px; display:none; flex-direction:column; z-index:50;
          box-shadow:0 10px 40px rgba(0,0,0,.55); }
  #chat.open { display:flex; }
  #chatkop { display:flex; align-items:center; gap:.6rem; padding:.7rem 1rem;
             border-bottom:1px solid var(--lijn); font-weight:600; }
  #chatkop img { height:1.6rem; }
  #chatkop button { margin-left:auto; background:none; border:none; color:var(--dim);
                    font-size:1.2rem; cursor:pointer; }
  #chatlog { flex:1; overflow-y:auto; padding:.85rem; display:flex; flex-direction:column; gap:.5rem; }
  .bub { max-width:85%; padding:.5rem .8rem; border-radius:14px; font-size:.95rem;
         line-height:1.45; white-space:pre-wrap; overflow-wrap:break-word; }
  .bub.ik { align-self:flex-end; background:var(--accent); color:#14171a;
            border-bottom-right-radius:4px; }
  .bub.birdy { align-self:flex-start; background:#262b31; border-bottom-left-radius:4px; }
  .bub.wacht { color:var(--dim); font-style:italic; }
  #chatinvoer { display:flex; gap:.45rem; padding:.65rem; border-top:1px solid var(--lijn); }
  #chatinvoer input { flex:1; background:var(--bg); border:1px solid #333a41; border-radius:10px;
                      color:var(--ink); padding:.55rem .8rem; font-size:.95rem; }
  #chatinvoer button { border:none; border-radius:10px; padding:0 .85rem; font-size:1.1rem;
                       cursor:pointer; background:var(--accent); color:#14171a; }
  #sleutel { display:none; padding:2rem; text-align:center; }
  #sleutel input { font-size:1.05rem; padding:.6rem; border-radius:8px; border:1px solid #444; }
</style></head><body>
<div id="sleutel">
  <img src="/logo.png" alt="Birdy" style="max-height:200px" onerror="this.style.display='none'"><br><br>
  <p>Vul de dashboard-sleutel in (staat in de .env op de server):</p><br>
  <input id="sleutelveld" placeholder="sleutel"> <button onclick="zetSleutel()">Opslaan</button></div>
<div id="app" style="display:none">
  <header>
    <h1><img src="/logo-bird.png" alt="" onerror="this.replaceWith('🐦')"><span>Birdy</span></h1>
    <div id="tabs">
      <button id="tabVandaag" class="actief" onclick="kiesTab('vandaag')">Vandaag</button>
      <button id="tabWeek" onclick="kiesTab('week')">Week</button>
    </div>
    <div id="klok"></div>
  </header>
  <div id="tekstinvoer">
    <input id="invoer" placeholder="Zeg of typ iets tegen Birdy — “voeg kwark toe aan de boodschappen”">
    <button class="micknop" onclick="spraak(this)" title="Praat tegen Birdy">🎤</button>
    <button id="stuurknop" onclick="stuur(document.getElementById('invoer').value)">→</button>
  </div>
  <div class="grid" id="paneelVandaag">
    <div class="panel"><h2 class="klik" onclick="kiesTab('week')" title="Naar weekoverzicht">📅 Agenda ↗</h2><ul id="agenda"></ul></div>
    <div class="kolom">
      <div class="panel"><h2>📋 Wat loopt er</h2><ul id="taken"></ul><div class="rest" id="takenrest"></div></div>
      <div class="panel" id="jarig"><h2>🎂 Verjaardagen</h2><ul id="verjaardagen"></ul></div>
    </div>
    <div class="panel"><h2>🛒 Boodschappen</h2><ul id="boodschappen"></ul>
      <div class="toevoeg"><input placeholder="+ toevoegen…" enterkeyhint="done"
        onkeydown="voegToe(event,'boodschappen',this)"></div>
      <details class="af" id="boodschappenAfWrap"><summary>onlangs afgevinkt</summary>
        <ul id="boodschappenAf"></ul></details></div>
    <div class="panel"><h2>⚡ Acties</h2><ul id="acties"></ul>
      <div class="toevoeg"><input placeholder="+ toevoegen…" enterkeyhint="done"
        onkeydown="voegToe(event,'acties',this)"></div>
      <details class="af" id="actiesAfWrap"><summary>onlangs afgevinkt</summary>
        <ul id="actiesAf"></ul></details></div>
  </div>
  <div id="paneelWeek">
    <div class="wkwrap"><div class="wk" id="wkgrid"></div></div>
    <div class="legenda" id="legenda"></div>
  </div>
</div>
<div id="melding"></div>
<button id="chatfab" title="Chat met Birdy" onclick="chatOpen(true)">
  <img src="/logo-bird.png" alt="" onerror="this.replaceWith('💬')"></button>
<div id="chat">
  <div id="chatkop"><img src="/logo-bird.png" alt="" onerror="this.replaceWith('🐦')">Birdy
    <button onclick="chatOpen(false)" title="Sluiten">✕</button></div>
  <div id="chatlog"></div>
  <div id="chatinvoer">
    <input id="chatveld" placeholder="Typ je bericht…" enterkeyhint="send">
    <button class="micknop" onclick="spraak(this)" title="Praat tegen Birdy">🎤</button>
    <button onclick="stuur(document.getElementById('chatveld').value)">→</button>
  </div>
</div>
<script>
let KEY = null;
try { KEY = localStorage.getItem('birdy-key'); } catch (e) {}
const q = new URLSearchParams(location.search).get('key');
if (q) { KEY = q; try { localStorage.setItem('birdy-key', q); } catch (e) {} }
function zetSleutel(){ KEY = document.getElementById('sleutelveld').value.trim();
  try { localStorage.setItem('birdy-key', KEY); } catch(e){} ververs(); }

function kiesTab(t){
  document.getElementById('paneelVandaag').style.display = t === 'vandaag' ? 'grid' : 'none';
  document.getElementById('paneelWeek').style.display = t === 'week' ? 'block' : 'none';
  document.getElementById('tabVandaag').classList.toggle('actief', t === 'vandaag');
  document.getElementById('tabWeek').classList.toggle('actief', t === 'week');
  try { localStorage.setItem('birdy-tab', t); } catch(e){}
}

function vul(id, items, maak){ const el = document.getElementById(id);
  el.innerHTML = items.length ? items.map(maak).join('') : '<li class="leeg">niets 🎉</li>'; }
function esc(s){ const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function dagLabel(d){
  const dt = new Date(d + 'T00:00'); const nu = new Date(); nu.setHours(0,0,0,0);
  const diff = Math.round((dt - nu) / 86400000);
  if (diff === 0) return 'Vandaag'; if (diff === 1) return 'Morgen';
  return dt.toLocaleDateString('nl-NL', { weekday:'long', day:'numeric', month:'short' });
}
function agendaHtml(items){
  if (!items.length) return '<li class="leeg">niets gepland 🎉</li>';
  const groepen = {};
  items.forEach(e => { const d = e.wanneer.slice(0,10); (groepen[d] = groepen[d]||[]).push(e); });
  return Object.keys(groepen).sort().map(d =>
    `<li class="dag">${dagLabel(d)}</li>` +
    groepen[d].map(e => `<li><small>${e.wanneer.length>10 ? e.wanneer.slice(11) : 'hele dag'}</small><span>${esc(e.titel)}</span></li>`).join('')
  ).join('');
}
function dueBadge(d){
  if (!d) return '';
  const dt = new Date(d + 'T00:00'); const nu = new Date(); nu.setHours(0,0,0,0);
  const diff = Math.round((dt - nu) / 86400000);
  if (diff < 0)  return `<span class="due laat">te laat</span>`;
  if (diff === 0) return `<span class="due nu">vandaag</span>`;
  if (diff === 1) return `<span class="due straks">morgen</span>`;
  const label = diff < 7
    ? dt.toLocaleDateString('nl-NL', { weekday:'short' })
    : dt.toLocaleDateString('nl-NL', { day:'numeric', month:'short' });
  return `<span class="due straks">${label}</span>`;
}

// ── weekweergave ──────────────────────────────────────────────────────────
const KLEUREN = ['#7fbfa6', '#d9a44e', '#e07a6a', '#8ab4d8', '#b39ddb', '#f2a1c2'];
let PERSONEN = [];
function persoonMatch(tekst){
  const t = tekst.toLowerCase();
  for (let i = 0; i < PERSONEN.length; i++)
    if (t.includes(PERSONEN[i].toLowerCase()))
      return { naam: PERSONEN[i], kleur: KLEUREN[i % KLEUREN.length] };
  return null;
}
function kleurVoor(titel){
  const p = persoonMatch(titel);
  return p ? p.kleur : '#5b6570';
}
function persChip(tekst){
  const p = persoonMatch(tekst);
  return p ? ` <span class="pers" style="background:${p.kleur}22;color:${p.kleur}">${esc(p.naam)}</span>` : '';
}
const U0 = 7, U1 = 22, HOOG = 510, PPU = HOOG / (U1 - U0);
function minuten(iso){ return parseInt(iso.slice(11,13),10)*60 + parseInt(iso.slice(14,16),10); }
function renderWeek(events){
  const grid = document.getElementById('wkgrid');
  const delen = bouwWeek(events);
  grid.innerHTML = delen;
  const leg = document.getElementById('legenda');
  leg.innerHTML = PERSONEN.map((p,i) =>
    `<span><i style="background:${KLEUREN[i%KLEUREN.length]}"></i>${esc(p)}</span>`).join('') +
    `<span><i style="background:#5b6570"></i>overig</span><span>⚠ rode rand = overlap</span>`;
}
function bouwWeek(events){
  const dagen = {}; const start = new Date(); start.setHours(0,0,0,0);
  const volgorde = [];
  for (let i = 0; i < 7; i++){
    const d = new Date(start); d.setDate(d.getDate() + i);
    const key = d.toLocaleDateString('sv-SE');
    volgorde.push(key); dagen[key] = { datum:d, heledag:[], tijd:[] };
  }
  events.forEach(e => {
    const d = e.start.slice(0,10); if (!(d in dagen)) return;
    (e.start.length <= 10 ? dagen[d].heledag : dagen[d].tijd).push(e);
  });
  let html = '<div></div>';
  volgorde.forEach((key, i) => {
    const g = dagen[key];
    const label = i === 0 ? 'Vandaag'
      : g.datum.toLocaleDateString('nl-NL', { weekday:'short', day:'numeric' });
    html += `<div class="kop${i===0?' vandaag':''}">${label}</div>`;
  });
  html += '<div></div>';
  volgorde.forEach(key => {
    const g = dagen[key];
    html += `<div class="heledag">` + g.heledag.map(e => {
      const k = kleurVoor(e.titel);
      return `<div class="chip" style="border-color:${k};background:${k}22"` +
        ` onclick="detail('${esc(e.titel).replace(/'/g,'&#39;')}','hele dag')">${esc(e.titel)}</div>`;
    }).join('') + `</div>`;
  });
  let uuras = '<div class="uuras">';
  for (let u = U0; u <= U1; u += 2)
    uuras += `<div style="top:${(u-U0)*PPU}px">${String(u).padStart(2,'0')}</div>`;
  html += uuras + '</div>';
  volgorde.forEach(key => {
    const g = dagen[key];
    const t = g.tijd.map(e => ({ ...e, s: minuten(e.start),
      e: (e.eind && e.eind.length > 10) ? Math.max(minuten(e.eind), minuten(e.start) + 30)
                                        : minuten(e.start) + 60 }));
    t.sort((a,b) => a.s - b.s);
    for (let a = 0; a < t.length; a++) for (let b = a+1; b < t.length; b++)
      if (t[b].s < t[a].e) { t[a].conflict = t[b].conflict = true; t[b].schuif = true; }
    let vak = '<div class="tijdvak">';
    for (let u = U0; u <= U1; u += 2) vak += `<div class="uurlijn" style="top:${(u-U0)*PPU}px"></div>`;
    t.forEach(e => {
      const top = Math.max(0, (e.s/60 - U0) * PPU);
      const hoogte = Math.max(24, Math.min(HOOG - top - 2, (e.e - e.s) / 60 * PPU - 2));
      const k = kleurVoor(e.titel);
      const tijd = e.start.slice(11,16) +
        ((e.eind && e.eind.length > 10) ? '–' + e.eind.slice(11,16) : '');
      vak += `<div class="blok${e.conflict ? ' conflict' : ''}"` +
        ` style="top:${top}px;height:${hoogte}px;border-color:${k};background:${k}26;` +
        `${e.schuif ? 'left:34%;' : (e.conflict ? 'right:34%;' : '')}color:var(--ink)"` +
        ` onclick="detail('${esc(e.titel).replace(/'/g,'&#39;')}','${tijd}${e.conflict ? ' · ⚠ overlapt' : ''}')">` +
        `<b>${esc(e.titel)}</b><small>${tijd}</small></div>`;
    });
    html += vak + '</div>';
  });
  return html;
}
function detail(titel, sub){ toon(`📅 ${titel} — ${sub}`); }

async function ververs(){
  try {
    const r = await fetch('/api/overview', { headers: { 'X-Dashboard-Key': KEY } });
    if (r.status === 401) { toonSleutel(); return; }
    const d = await r.json();
    document.getElementById('sleutel').style.display = 'none';
    document.getElementById('app').style.display = 'block';
    document.getElementById('klok').textContent = d.nu;
    PERSONEN = d.personen || [];
    document.getElementById('agenda').innerHTML = agendaHtml(d.agenda);
    renderWeek(d.week || []);
    const t = d.taken || {urgent:[],week:[],rest:''};
    const rows = t.urgent.map(x => `<li class="urgent"><span>${esc(x)}${persChip(x)}</span></li>`)
      .concat(t.week.map(x => `<li class="week"><span>${esc(x)}${persChip(x)}</span></li>`));
    document.getElementById('taken').innerHTML =
      rows.length ? rows.join('') : '<li class="leeg">niets dringends 🎉</li>';
    document.getElementById('takenrest').textContent = t.rest ? 'verder: ' + t.rest : '';
    const taakRij = x => {
      const p = persoonMatch(x.tekst);
      return `<li class="vink"${p ? ` style="--pc:${p.kleur}"` : ''} onclick="vink(this,'${x.id}')">` +
        `<span>${esc(x.tekst)}${dueBadge(x.due)}</span>` +
        (x.due ? '' : `<button class="duebtn" title="deadline prikken"` +
          ` onclick="event.stopPropagation();kiesDatum('${x.id}')">+</button>`) + `</li>`;
    };
    vul('boodschappen', d.boodschappen, taakRij);
    vul('acties', d.acties, taakRij);
    const afRij = x => `<li><span>${esc(x.tekst)}</span>` +
      `<button class="herstelknop" title="terugzetten" onclick="herstel('${x.id}')">↩</button></li>`;
    [['boodschappen', d.boodschappen_af], ['acties', d.acties_af]].forEach(([naam, items]) => {
      const wrap = document.getElementById(naam + 'AfWrap');
      wrap.style.display = (items && items.length) ? 'block' : 'none';
      document.getElementById(naam + 'Af').innerHTML = (items || []).map(afRij).join('');
    });
    vul('verjaardagen', d.verjaardagen,
        j => `<li><small>${j.datum}</small><span>${esc(j.naam)} <b>${j.dagen===0?'vandaag! 🎉':'over '+j.dagen+' dgn'}</b></span></li>`);
  } catch (e) { /* volgende poging over 60s */ }
}
function toonSleutel(){ document.getElementById('app').style.display='none';
  document.getElementById('sleutel').style.display='block'; }
try { kiesTab(localStorage.getItem('birdy-tab') || 'vandaag'); } catch(e){ kiesTab('vandaag'); }
ververs(); setInterval(ververs, 60000);

// ── chat ──────────────────────────────────────────────────────────────────
let chatGesch = [];
try { chatGesch = JSON.parse(localStorage.getItem('birdy-chat')) || []; } catch (e) {}
function chatBewaar(){ try { localStorage.setItem('birdy-chat',
  JSON.stringify(chatGesch.slice(-60))); } catch (e) {} }
function chatRender(){ const log = document.getElementById('chatlog');
  log.innerHTML = chatGesch.map(m => `<div class="bub ${m.wie}">${esc(m.tekst)}</div>`).join('');
  log.scrollTop = log.scrollHeight; }
function chatOpen(open){
  document.getElementById('chat').classList.toggle('open', open);
  document.getElementById('chatfab').style.display = open ? 'none' : 'flex';
  if (open) { chatRender(); document.getElementById('chatveld').focus(); }
}
function chatVoeg(wie, tekst){ chatGesch.push({ wie, tekst }); chatBewaar(); chatRender(); }

async function stuur(tekst){
  tekst = (tekst || '').trim(); if (!tekst) return;
  document.getElementById('invoer').value = '';
  document.getElementById('chatveld').value = '';
  chatOpen(true); chatVoeg('ik', tekst);
  const log = document.getElementById('chatlog');
  const wacht = document.createElement('div');
  wacht.className = 'bub birdy wacht'; wacht.textContent = '…denkt na…';
  log.appendChild(wacht); log.scrollTop = log.scrollHeight;
  try {
    const r = await fetch('/api/message', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ text: tekst }) });
    const d = await r.json();
    wacht.remove(); chatVoeg('birdy', d.reply || d.error || 'er ging iets mis');
    ververs();
  } catch(e){ wacht.remove(); chatVoeg('birdy', 'Ik ben even niet bereikbaar — probeer het zo nog eens.'); }
}

async function voegToe(ev, lijst, input){
  if (ev.key !== 'Enter') return;
  const tekst = input.value.trim(); if (!tekst) return;
  input.value = ''; input.placeholder = '… toevoegen';
  try {
    const r = await fetch('/api/add', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ lijst, tekst }) });
    if (!r.ok) throw new Error();
    await ververs();
  } catch(e){ input.value = tekst; toon('Toevoegen lukte even niet — probeer nog eens.'); }
  input.placeholder = '+ toevoegen…';
}
async function zetDatum(id, datum){
  try {
    const r = await fetch('/api/due', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ id, datum }) });
    if (!r.ok) throw new Error();
    await ververs();
  } catch(e){ toon('Deadline zetten lukte even niet — probeer nog eens.'); }
}
function kiesDatum(id){
  const inp = document.createElement('input');
  inp.type = 'date'; inp.min = new Date().toISOString().slice(0,10);
  inp.style.cssText = 'position:fixed;bottom:0;left:0;opacity:0;pointer-events:none';
  document.body.appendChild(inp);
  inp.onchange = () => { if (inp.value) zetDatum(id, inp.value); inp.remove(); };
  try { inp.showPicker(); }
  catch(e){ inp.remove();
    const d = prompt('Deadline (JJJJ-MM-DD):');
    if (d && /^\\d{4}-\\d{2}-\\d{2}$/.test(d.trim())) zetDatum(id, d.trim()); }
}
async function vink(el, id){
  el.classList.add('gedaan');
  const tekst = (el.querySelector('span')?.childNodes[0]?.textContent || 'taak').trim();
  try {
    const r = await fetch('/api/done', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ id }) });
    if (!r.ok) throw new Error();
    toonMetKnop(`✓ Afgevinkt: ${tekst}`, 'Ongedaan maken', () => herstel(id));
    setTimeout(ververs, 800);
  } catch(e){
    el.classList.remove('gedaan');
    toon('Afvinken lukte even niet — probeer nog eens.');
  }
}
async function herstel(id){
  try {
    const r = await fetch('/api/reopen', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ id }) });
    if (!r.ok) throw new Error();
    toon('Hersteld 👍'); ververs();
  } catch(e){ toon('Herstellen lukte even niet — check de Todoist-app.'); }
}
function toon(t){ const a = document.getElementById('melding');
  a.textContent = t; a.style.display = 'block';
  clearTimeout(a._t); a._t = setTimeout(() => a.style.display='none', 8000); }
function toonMetKnop(t, knoptekst, actie){
  const a = document.getElementById('melding');
  a.textContent = t;
  const b = document.createElement('button');
  b.textContent = knoptekst;
  b.onclick = () => { a.style.display = 'none'; actie(); };
  a.appendChild(b);
  a.style.display = 'block';
  clearTimeout(a._t); a._t = setTimeout(() => a.style.display='none', 10000); }

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null, luistert = false;
function spraak(knop){
  if (!SR) {
    chatOpen(true); document.getElementById('chatveld').focus();
    toon('🎤 Spraak werkt in Chrome of Safari — typen kan altijd.');
    return;
  }
  if (luistert) { rec.stop(); return; }
  rec = new SR(); rec.lang = 'nl-NL'; rec.interimResults = false;
  rec.onstart = () => { luistert = true; knop.classList.add('luistert'); };
  rec.onend = () => { luistert = false; knop.classList.remove('luistert'); };
  rec.onerror = () => toon('🎤 Ik kon je niet verstaan — probeer nog eens.');
  rec.onresult = ev => stuur(ev.results[0][0].transcript);
  rec.start();
}
document.getElementById('invoer').addEventListener('keydown',
  e => { if (e.key === 'Enter') stuur(e.target.value); });
document.getElementById('chatveld').addEventListener('keydown',
  e => { if (e.key === 'Enter') stuur(e.target.value); });
</script></body></html>"""
