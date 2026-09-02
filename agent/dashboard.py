"""Kiosk-dashboard voor de muurtablet (en telefoons).

Draait als aiohttp-server in de agent-container, alleen op localhost van de VPS;
ontsluiting gebeurt via Tailscale (tailscale serve → HTTPS, nodig voor de microfoon).
Aan/uit via DASHBOARD_TOKEN in .env: leeg = dashboard uit.

- GET  /                → kioskpagina (dark, auto-verversend, microfoonknop)
- GET  /api/overview    → agenda, onderwerpen (Doc "Wat loopt er"), aandacht (regels +
                          Birdy's briefingpunten uit AANDACHT.md), lijstjes, verjaardagen,
                          regelzaken, thuis (JSON, cache 2 min)
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


def _agenda_rijk(days: int = 7) -> tuple[list[dict], bool]:
    """Afspraken mét eindtijd, voor de weekweergave: ([{start, eind, titel, …}], compleet).
    compleet=False als een bron faalde — dan is het resultaat mogelijk (deels) leeg en
    houdt de cache liever de vorige complete versie vast."""
    events: list[dict] = []
    google_ok = not os.environ.get("GOOGLE_CALENDAR_ID")
    fw_ok = not os.environ.get("FAMILYWALL_ICS_URL")
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
                timeZone="Europe/Amsterdam",  # anders komt alles in UTC (2 uur te vroeg)
            ).execute()
            for ev in resp.get("items", []):
                s, e = ev.get("start", {}), ev.get("end", {})
                wie = ev.get("creator", {}) or {}
                org = ev.get("organizer", {}) or {}
                naam = (org.get("displayName") or wie.get("displayName")
                        or wie.get("email") or org.get("email") or "")
                events.append({
                    "id": ev.get("id", ""),
                    "start": (s.get("dateTime") or s.get("date", ""))[:16],
                    "eind": (e.get("dateTime") or e.get("date", ""))[:16],
                    "titel": ev.get("summary", "(zonder titel)"),
                    "omschrijving": (ev.get("description") or "")[:600],
                    "locatie": ev.get("location", ""),
                    "wie": naam.replace("bakkerbirdy@gmail.com", "Birdy"),
                    "bron": "Gezinsagenda (Google)",
                })
            google_ok = True
    except BaseException:  # SystemExit van de CLI-helpers telt ook
        log.warning("Google-agenda ophalen mislukt", exc_info=True)
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
                    "id": "",  # alleen-lezen: FamilyWall-afspraken kunnen niet verzet worden
                    "start": iso(start),
                    "eind": iso(eind.dt if eind else None) or iso(start),
                    "titel": str(ev.get("SUMMARY", "(zonder titel)")),
                    "omschrijving": str(ev.get("DESCRIPTION", ""))[:600],
                    "locatie": str(ev.get("LOCATION", "")),
                    "wie": "",
                    "bron": "FamilyWall",
                })
            fw_ok = True
    except BaseException:
        log.warning("FamilyWall ophalen mislukt", exc_info=True)
    # zelfde moment + titel uit beide bronnen → één keer
    seen, uniek = set(), []
    for ev in sorted(events, key=lambda e: e["start"]):
        key = (ev["start"], ev["titel"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        uniek.append(ev)
    return uniek[:60], google_ok and fw_ok


def _agenda_compact(rijk: list[dict]) -> list[dict]:
    return [{
        "wanneer": ev["start"].replace("T", " "),
        "titel": ev["titel"],
    } for ev in rijk][:22]


def _datum_dagen(tekst: str, vandaag: date | None = None) -> int | None:
    """DD-MM(-JJJJ) ergens in de tekst → aantal dagen vanaf vandaag (negatief = voorbij).
    Zonder jaar: de eerstvolgende keer dat die datum valt (of net voorbij, tot 60 dagen)."""
    import re

    vandaag = vandaag or date.today()
    m = re.search(r"(\d{1,2})-(\d{1,2})(?:-(\d{4}))?", tekst)
    if not m:
        return None
    dag, maand, jaar = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if jaar:
            return (date(int(jaar), maand, dag) - vandaag).days
        d = date(vandaag.year, maand, dag)
        if (d - vandaag).days < -60:
            d = date(vandaag.year + 1, maand, dag)
        return (d - vandaag).days
    except ValueError:
        return None


def _onderwerpen_parse(text: str, vandaag: date | None = None) -> list[dict]:
    """Lopende onderwerpen uit het Google Doc 'Wat loopt er'. Regelformat:
    • Kinderfeest Evi — wie: Jaap · wanneer: 06-09 · stap: gastenlijst invullen · notitie: …
    Regels onder een kop 'Afgerond' of beginnend met ✅ tellen niet mee."""
    import re

    def veld(s: str, naam: str) -> str:
        m = re.search(r"\b" + naam + r"\s*:\s*([^·]+)", s, re.I)
        return m.group(1).strip() if m else ""

    out, klaar = [], False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if re.match(r"^(#+\s*)?(afgerond|klaar|gedaan)\b", s, re.I):
            klaar = True
            continue
        if not s.startswith(("•", "-", "*", "✅")):
            continue
        if klaar or s.startswith("✅") or "✅" in s[:3]:
            continue
        s = s.lstrip("•-* ").strip()
        naam = re.split(r"\s+[—–-]\s+|\s+·\s+", s)[0].strip()
        if not naam:
            continue
        wanneer = veld(s, "wanneer")
        out.append({
            "naam": naam[:80], "wie": veld(s, "wie")[:30], "wanneer": wanneer[:30],
            "dagen": _datum_dagen(wanneer, vandaag) if wanneer else None,
            "stap": veld(s, "(?:volgende )?stap")[:160], "notitie": veld(s, "notitie")[:300],
        })
    out.sort(key=lambda o: (o["dagen"] is None, o["dagen"] if o["dagen"] is not None else 0))
    return out[:30]


def _doc_tekst(pad: str) -> str:
    """Platte tekst van een Google Doc in de Drive-hub; '' als hij er niet is of Drive uit staat."""
    from . import gdrive

    try:
        svc = gdrive._service()
        node = gdrive._resolve(svc, pad, must_exist=False)
        if not node:
            return ""
        text = svc.files().export(fileId=node["id"], mimeType="text/plain").execute()
        return text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text
    except BaseException:
        return ""


ONDERWERPEN_DOC = "Wat loopt er"


def _onderwerpen() -> list[dict]:
    return _onderwerpen_parse(_doc_tekst(ONDERWERPEN_DOC))


def _aandacht_birdy(workspace: Path) -> dict:
    """Birdy's aandachtspunten uit AANDACHT.md (geschreven door de ochtendbriefing).
    Format: eerste regel '💡 AANDACHT (bijgewerkt DD-MM HH:MM)', daarna • regels (max 3)."""
    import re

    pad = workspace / "AANDACHT.md"
    if not pad.exists():
        return {"tijd": "", "items": []}
    items, tijd = [], ""
    for line in pad.read_text().splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.search(r"bijgewerkt\s+([\d-]+\s+[\d:]+)", s, re.I)
        if m and not tijd:
            tijd = m.group(1)
            continue
        if s.startswith(("•", "-", "*")):
            items.append(s.lstrip("•-* ").strip()[:200])
    return {"tijd": tijd, "items": items[:3]}


def _signalen(acties: list[dict], regelzaken: list[dict], verjaardagen: list[dict],
              week: list[dict], onderwerpen: list[dict], vandaag: date | None = None) -> list[dict]:
    """Regel-gebaseerde aandachtspunten, zonder LLM. Elk item: {tekst, l2, ernst}
    (ernst 0 = te laat/vandaag, 1 = binnenkort). l2 = welk blad opent bij klikken."""
    vandaag = vandaag or date.today()
    out: list[dict] = []
    vandaag_s = vandaag.isoformat()

    te_laat = [a for a in acties if a.get("due") and a["due"] < vandaag_s]
    if te_laat:
        n = len(te_laat)
        out.append({"tekst": f"{n} actie{'s' if n > 1 else ''} over de datum: "
                             + ", ".join(a["tekst"][:28] for a in te_laat[:2])
                             + (" …" if n > 2 else ""), "l2": "acties", "ernst": 0})
    vandaag_acties = [a for a in acties if a.get("due") == vandaag_s]
    if vandaag_acties:
        n = len(vandaag_acties)
        out.append({"tekst": f"Vandaag: {', '.join(a['tekst'][:28] for a in vandaag_acties[:3])}"
                             + (f" (+{n - 3})" if n > 3 else ""), "l2": "acties", "ernst": 0})

    for o in onderwerpen:
        if o["dagen"] is not None and o["dagen"] <= 1:
            wanneer = "vandaag" if o["dagen"] == 0 else "morgen" if o["dagen"] == 1 \
                else f"{-o['dagen']} dag{'en' if o['dagen'] < -1 else ''} over tijd"
            out.append({"tekst": f"📂 {o['naam']}: {wanneer}"
                                 + (f" — {o['stap']}" if o["stap"] else ""),
                        "l2": "onderwerpen", "ernst": 0 if o["dagen"] <= 0 else 1})

    for z in regelzaken:
        if z.get("dagen") is not None and z["dagen"] < 0:
            out.append({"tekst": f"🔁 {z['naam']} is {-z['dagen']} dag{'en' if z['dagen'] < -1 else ''} over tijd"
                                 + (f" ({z['wie']})" if z.get("wie") else ""),
                        "l2": "regelzaken", "ernst": 0})

    for j in verjaardagen:
        if j.get("dagen") is not None and 0 <= j["dagen"] <= 7 and not (j.get("notitie") or "").strip():
            wanneer = "vandaag" if j["dagen"] == 0 else "morgen" if j["dagen"] == 1 else f"over {j['dagen']} dagen"
            out.append({"tekst": f"🎂 {j['naam']} {wanneer}, nog geen cadeau-idee",
                        "l2": "verjaardagen", "ernst": 1 if j["dagen"] > 1 else 0})

    # overlappende afspraken met tijd, vandaag en morgen
    morgen_s = (vandaag + timedelta(days=1)).isoformat()
    getimed = [e for e in week if "T" in e.get("start", "") and e["start"][:10] in (vandaag_s, morgen_s)]
    gemeld: set[tuple[str, str]] = set()
    for i, a in enumerate(getimed):
        for b in getimed[i + 1:]:
            if a["start"][:10] != b["start"][:10]:
                continue
            a_eind, b_eind = a.get("eind") or a["start"], b.get("eind") or b["start"]
            if a["start"] < b_eind and b["start"] < a_eind:
                sleutel = tuple(sorted((a["titel"], b["titel"])))
                if sleutel in gemeld:
                    continue
                gemeld.add(sleutel)
                dag = "vandaag" if a["start"][:10] == vandaag_s else "morgen"
                out.append({"tekst": f"⚠️ Overlap {dag} {a['start'][11:16]}: {a['titel'][:24]} en {b['titel'][:24]}",
                            "l2": "week", "ernst": 0})

    out.sort(key=lambda s: s["ernst"])
    return out[:8]


def _todoist_lijst(naam: str) -> list[dict]:
    from . import todoist

    try:
        project = todoist._project(naam)
        tasks = todoist._list_all("/tasks", {"project_id": project["id"]})
        out = [{
            "id": str(t["id"]),
            "tekst": t["content"],
            "due": ((t.get("due") or {}).get("date") or "")[:10],
            "notitie": (t.get("description") or "")[:400],
        } for t in tasks]
        out.sort(key=lambda t: (t["due"] == "", t["due"]))  # deadlines eerst, oplopend
        return out[:50]  # de Vandaag-tab toont de top; de verdiepende pagina alles
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


def _todoist_toevoegen(lijst: str, tekst: str) -> dict | None:
    from . import todoist

    try:
        project = todoist._project(lijst)
        t = todoist._request("POST", "/tasks", json={"content": tekst, "project_id": project["id"]})
        return {"id": str(t["id"]), "tekst": t["content"],
                "due": ((t.get("due") or {}).get("date") or "")[:10]}
    except BaseException:
        return None


def _agenda_verzet(event_id: str, start: str, eind: str) -> bool:
    """Google-afspraak verplaatsen (start/eind als 'YYYY-MM-DDTHH:MM', lokale tijd)."""
    from . import gcal

    try:
        svc = gcal._service()
        svc.events().patch(
            calendarId=os.environ["GOOGLE_CALENDAR_ID"], eventId=event_id,
            body={
                "start": {"dateTime": f"{start}:00", "timeZone": "Europe/Amsterdam"},
                "end": {"dateTime": f"{eind}:00", "timeZone": "Europe/Amsterdam"},
            },
        ).execute()
        return True
    except BaseException:
        log.warning("afspraak verzetten mislukt", exc_info=True)
        return False


def _regelzaken() -> list[dict]:
    """Terugkerende regelzaken uit het huishoudhandboek (Google Doc), gesorteerd op
    'volgende'-datum. Regelformat: • Kapper Evi — wie: Yvette · elke: ~8 weken ·
    laatst: 15-07-2026 · volgende: ±09-09-2026"""
    import re

    from . import gdrive

    try:
        svc = gdrive._service()
        node = gdrive._resolve(svc, "20 Huishouden/Huishoudhandboek", must_exist=False)
        if not node:
            return []
        text = svc.files().export(fileId=node["id"], mimeType="text/plain").execute()
        text = text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text
    except BaseException:
        return []

    def veld(s: str, naam: str) -> str:
        m = re.search(naam + r"\s*:\s*±?\s*([^·]+)", s, re.I)
        return m.group(1).strip() if m else ""

    vandaag = date.today()
    out = []
    for line in text.splitlines():
        s = line.strip().lstrip("•-* ").strip()
        if not s or not re.search(r"\b(volgende|elke)\b", s, re.I):
            continue
        naam = re.split(r"\s+[—–-]\s+|\s+·\s+", s)[0].strip()
        volgende = veld(s, "volgende")
        dagen = None
        m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})", volgende)
        if m:
            try:
                dagen = (date(int(m.group(3)), int(m.group(2)), int(m.group(1))) - vandaag).days
            except ValueError:
                pass
        out.append({"naam": naam[:60], "wie": veld(s, "wie"), "elke": veld(s, "elke"),
                    "laatst": veld(s, "laatst"), "volgende": volgende, "dagen": dagen})
    out.sort(key=lambda z: (z["dagen"] is None, z["dagen"] if z["dagen"] is not None else 0))
    return out[:30]


def _thuis() -> dict | None:
    """Stand van het huis via Homey; None als niet gekoppeld of even onbereikbaar."""
    from . import homey

    if not homey.geconfigureerd():
        return None
    try:
        return homey.samenvatting()
    except BaseException:
        log.warning("Homey ophalen mislukt", exc_info=True)
        return None


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
        delen = [d.strip(" ·—-") for d in clean[5:].split("·")]
        out.append({"datum": clean[:5], "naam": delen[0],
                    "notitie": " · ".join(d for d in delen[1:] if d and d != "—"),
                    "dagen": (volgende - vandaag).days})
    return sorted(out, key=lambda x: x["dagen"])[:30]


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
        app.router.add_post("/api/verzet", self.verzet)
        app.router.add_post("/api/homey/lamp", self.homey_lamp)
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
            (week, compleet), jarigen, regelzaken, thuis, onderwerpen = await asyncio.gather(
                asyncio.to_thread(_agenda_rijk),
                asyncio.to_thread(_verjaardagen),
                asyncio.to_thread(_regelzaken),
                asyncio.to_thread(_thuis),
                asyncio.to_thread(_onderwerpen),
            )
            if compleet or not self._traag:
                self._traag = {"week": week, "verjaardagen": jarigen, "regelzaken": regelzaken,
                               "thuis": thuis, "onderwerpen": onderwerpen}
                self._traag_ts = time.time()
            else:
                # een bron faalde: houd de vorige complete week vast en probeer bij de
                # volgende aanvraag meteen opnieuw (ts bewust niet bijgewerkt)
                self._traag["verjaardagen"] = jarigen
                self._traag["regelzaken"] = regelzaken
                self._traag["thuis"] = thuis or self._traag.get("thuis")
                self._traag["onderwerpen"] = onderwerpen
        boodschappen, acties, boodschappen_af, acties_af = await asyncio.gather(
            asyncio.to_thread(_todoist_lijst, "boodschappen"),
            asyncio.to_thread(_todoist_lijst, "acties"),
            asyncio.to_thread(_todoist_afgevinkt, "boodschappen"),
            asyncio.to_thread(_todoist_afgevinkt, "acties"),
        )
        onderwerpen = self._traag.get("onderwerpen", [])
        vers = {
            "agenda": _agenda_compact(self._traag["week"]),
            "week": self._traag["week"],
            "personen": self.cfg.dashboard_personen,
            "onderwerpen": onderwerpen,
            "aandacht": {
                "birdy": _aandacht_birdy(self.cfg.workspace),
                "signalen": _signalen(acties, self._traag.get("regelzaken", []),
                                      self._traag["verjaardagen"], self._traag["week"], onderwerpen),
            },
            "boodschappen": boodschappen,
            "acties": acties,
            "boodschappen_af": boodschappen_af,
            "acties_af": acties_af,
            "verjaardagen": self._traag["verjaardagen"],
            "regelzaken": self._traag.get("regelzaken", []),
            "thuis": self._traag.get("thuis"),
        }
        if gen == self._gen:  # geen mutatie tijdens het bouwen → cachen mag
            self._cache = vers
            self._cache_ts = time.time()
        return dict(vers)

    async def done(self, request: web.Request) -> web.Response:
        return await self._taak_actie(request, _todoist_afvinken, self._patch_afgevinkt)

    async def reopen(self, request: web.Request) -> web.Response:
        return await self._taak_actie(request, _todoist_heropen, self._patch_heropend)

    async def _taak_actie(self, request: web.Request, actie, patch) -> web.Response:
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
            # Todoist's lijst-API loopt na een mutatie soms seconden achter; opnieuw
            # ophalen zou verouderde data cachen. Daarom: cache zelf bijwerken en pas
            # bij de volgende TTL-verversing weer met Todoist verzoenen.
            self._gen += 1  # lopende (oude) opbouw mag hier niet meer overheen cachen
            if self._cache:
                patch(self._cache, task_id)
        return web.json_response({"ok": ok}, status=200 if ok else 502)

    @staticmethod
    def _patch_afgevinkt(c: dict, task_id: str) -> None:
        for lijst, af in (("boodschappen", "boodschappen_af"), ("acties", "acties_af")):
            for t in c.get(lijst, []):
                if t["id"] == task_id:
                    c[lijst] = [x for x in c[lijst] if x["id"] != task_id]
                    c[af] = ([{"id": task_id, "tekst": t["tekst"]}] + list(c.get(af, [])))[:6]
                    return

    @staticmethod
    def _patch_heropend(c: dict, task_id: str) -> None:
        for lijst, af in (("boodschappen", "boodschappen_af"), ("acties", "acties_af")):
            for t in c.get(af, []):
                if t["id"] == task_id:
                    c[af] = [x for x in c[af] if x["id"] != task_id]
                    c[lijst] = list(c.get(lijst, [])) + [{"id": task_id, "tekst": t["tekst"], "due": ""}]
                    return

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
            self._gen += 1
            if self._cache:
                for lijst in ("boodschappen", "acties"):
                    for t in self._cache.get(lijst, []):
                        if t["id"] == task_id:
                            t["due"] = datum
        return web.json_response({"ok": ok}, status=200 if ok else 502)

    async def verzet(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        try:
            body = await request.json()
            event_id = str(body.get("id", "")).strip()
            start = str(body.get("start", "")).strip()
            eind = str(body.get("eind", "")).strip()
        except Exception:
            event_id = start = eind = ""

        def geldig(t: str) -> bool:
            return len(t) == 16 and t[10] == "T" and t[:10].replace("-", "").isdigit() \
                and t[11:].replace(":", "").isdigit()

        if not event_id or len(event_id) > 200 or not geldig(start) or not geldig(eind):
            return web.json_response({"error": "id of tijd ongeldig"}, status=400)
        ok = await asyncio.to_thread(_agenda_verzet, event_id, start, eind)
        if ok:
            self._gen += 1
            self._cache = None  # agenda-compact opnieuw opbouwen (uit gepatchte week)
            if self._traag:
                for e in self._traag["week"]:
                    if e.get("id") == event_id:
                        e["start"], e["eind"] = start, eind
                self._traag["week"].sort(key=lambda e: e["start"])
        return web.json_response({"ok": ok}, status=200 if ok else 502)

    async def homey_lamp(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        try:
            body = await request.json()
            device_id = str(body.get("id", "")).strip()
            aan = bool(body.get("aan", False))
        except Exception:
            device_id = ""
        if not device_id or len(device_id) > 80:
            return web.json_response({"error": "geen apparaat"}, status=400)
        from . import homey
        try:
            await asyncio.to_thread(homey.zet_aan_uit, device_id, aan)
        except Exception as e:
            fout = "geen rechten (scope 'Apparaten: bedienen' ontbreekt)" if "403" in str(e) else str(e)[:120]
            return web.json_response({"error": fout}, status=502)
        thuis = await asyncio.to_thread(_thuis)
        if self._traag and thuis:
            self._traag["thuis"] = thuis
        self._cache = None
        self._gen += 1
        return web.json_response({"ok": True})

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
        taak = await asyncio.to_thread(_todoist_toevoegen, lijst, tekst)
        if taak:
            self._gen += 1
            if self._cache:
                self._cache[lijst] = list(self._cache.get(lijst, [])) + [taak]
        return web.json_response({"ok": bool(taak)}, status=200 if taak else 502)

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
  /* aandacht: Birdy's eigen punten herkenbaar (vogeltje + warme tint), regels gewoon */
  li.birdy { background:rgba(217,164,78,.11); border-left:3px solid var(--amber); border-radius:8px;
             padding:.38rem .55rem; margin:.12rem 0; align-items:flex-start; }
  img.bird { width:1.15rem; height:1.15rem; flex:0 0 auto; vertical-align:-.2rem; }
  li.birdy img.bird { margin-top:.1rem; }
  li.signaal { cursor:pointer; border-radius:8px; margin:0 -.4rem; padding:.28rem .4rem; }
  li.signaal:active { background:rgba(127,191,166,.12); }
  li.signaal.ernst0 span::before { content:"● "; color:var(--rood); }
  li.signaal.ernst1 span::before { content:"● "; color:var(--amber); }
  .rest .bird { width:.9rem; height:.9rem; vertical-align:-.15rem; margin-right:.2rem; }
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
  .blok.sleepbaar { touch-action:none; cursor:grab; }
  .blok.sleept { z-index:30; opacity:.85; cursor:grabbing; box-shadow:0 6px 18px rgba(0,0,0,.5); }
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
  li.klik { cursor:pointer; }
  /* ── vandaag: compacte zijbalk + hoofdvlak ── */
  #paneelVandaag.lay { display:grid; grid-template-columns:238px 1fr; gap:.9rem;
                       align-items:start; }
  .zijbalk { display:flex; flex-direction:column; gap:.65rem; }
  .mini { background:var(--panel); border-radius:12px; padding:.7rem .85rem; cursor:pointer;
          border:1px solid transparent; }
  .mini:hover, .mini:active { border-color:var(--accent); }
  .mini h3 { font-size:.72rem; letter-spacing:.09em; text-transform:uppercase;
             color:var(--accent); display:flex; align-items:center; gap:.4rem;
             margin-bottom:.35rem; }
  .mini h3 b { margin-left:auto; background:rgba(127,191,166,.18); color:var(--accent);
               border-radius:99px; padding:0 .5rem; font-size:.7rem; }
  .mini ul { list-style:none; padding:0; }
  .mini li { font-size:.86rem; padding:.13rem 0; display:flex; gap:.4rem; align-items:baseline; }
  .mini li span { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .mini li small { color:var(--dim); flex:0 0 auto; font-size:.74rem; }
  .mini li small.laat { color:var(--rood); font-weight:700; }
  .mini li small.nu { color:var(--amber); font-weight:700; }
  .mini li.meer { color:var(--dim); font-size:.78rem; }
  .hoofd { display:grid; gap:.9rem; grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
           align-items:start; }
  @media (max-width:760px){
    #paneelVandaag.lay { grid-template-columns:1fr; }
    .zijbalk { flex-direction:row; overflow-x:auto; }
    .mini { min-width:190px; flex:0 0 auto; }
  }
  /* ── planning (kinderroutine) ── */
  #paneelPlan { display:none; max-width:600px; margin:0 auto; }
  .pl-kop { display:flex; align-items:center; gap:.7rem; margin-bottom:1rem; }
  .pl-kop h2 { font-size:1.35rem; }
  .pl-dagdeel { margin-left:auto; display:flex; gap:.3rem; background:var(--panel);
                border-radius:99px; padding:.22rem; }
  .pl-dagdeel button { border:none; background:none; color:var(--dim); font-size:.95rem;
                       padding:.4rem .9rem; border-radius:99px; cursor:pointer; }
  .pl-dagdeel button.actief { background:var(--amber); color:#14171a; font-weight:700; }
  .pl-hint { color:var(--dim); margin-bottom:.8rem; font-size:1rem; }
  .pl-lijst { list-style:none; padding:0; display:flex; flex-direction:column; gap:.55rem; }
  .pl-kaart { background:var(--panel); border:2px solid var(--lijn); border-radius:16px;
              padding:1rem 1.1rem; display:flex; align-items:center; gap:.9rem;
              font-size:1.2rem; user-select:none; -webkit-user-select:none;
              position:relative; overflow:hidden; }
  .pl-kaart.sleepbaar { touch-action:none; cursor:grab; }
  .pl-kaart .em { font-size:2rem; }
  .pl-kaart .naam { font-weight:600; }
  .pl-kaart .tijd { margin-left:auto; color:var(--dim); font-size:.95rem; white-space:nowrap; }
  .pl-kaart.tilt { z-index:5; box-shadow:0 10px 28px rgba(0,0,0,.55); border-color:var(--accent); }
  .pl-kaart .balk { position:absolute; left:0; bottom:0; height:6px; background:var(--amber);
                    width:0%; border-radius:0 3px 3px 0; transition:width 1s linear; }
  .pl-kaart.nu { border-color:var(--amber); }
  .pl-kaart.nu .naam::after { content:" ⏰"; }
  .pl-kaart.af { opacity:.5; border-color:var(--accent); }
  .pl-kaart.af .naam { text-decoration:line-through; }
  .pl-kaart.af .balk { background:var(--accent); width:100% !important; }
  .pl-vink { width:2.4rem; height:2.4rem; border-radius:50%; border:2px solid var(--accent);
             background:none; color:var(--accent); font-size:1.3rem; cursor:pointer;
             flex:0 0 auto; padding:0; }
  .pl-kaart.af .pl-vink { background:var(--accent); color:#14171a; }
  .pl-start { width:100%; margin-top:1.1rem; padding:1.1rem; font-size:1.35rem; border:none;
              border-radius:16px; background:var(--accent); color:#14171a; font-weight:800;
              cursor:pointer; }
  .pl-reset { margin-top:1rem; background:none; border:none; color:var(--dim);
              font-size:.85rem; cursor:pointer; text-decoration:underline; }
  #paneelPlan.breed { max-width:none; }
  .pl-sterren { font-size:1.05rem; letter-spacing:.12em; line-height:1.5; word-break:break-all; }
  .pl-bonuskaart { transition:min-height .6s; }
  .pl-versie { display:flex; gap:.3rem; background:var(--panel); border-radius:99px; padding:.22rem; }
  .pl-versie button { border:none; background:none; color:var(--dim); font-size:.9rem;
                      padding:.35rem .8rem; border-radius:99px; cursor:pointer; }
  .pl-versie button.actief { background:var(--accent); color:#14171a; font-weight:700; }
  .pl-opzet { display:grid; grid-template-columns:1fr 1fr; gap:1rem; align-items:start; }
  .pl-kolomkop { font-size:.78rem; letter-spacing:.08em; text-transform:uppercase;
                 color:var(--dim); margin-bottom:.5rem; }
  .pl-slots, .pl-bak { list-style:none; padding:0; display:flex; flex-direction:column; gap:.5rem; }
  .pl-slot { border:2px dashed #3a4148; border-radius:16px; min-height:3.9rem; display:flex;
             align-items:center; justify-content:center; color:var(--dim); font-size:.9rem; }
  .pl-slot.vol { border:none; min-height:0; display:block; }
  .pl-slot .pl-kaart { border-color:var(--accent); }
  .pl-kaart.mini { padding:.75rem .85rem; font-size:1.05rem; gap:.7rem; }
  .pl-kaart.mini .em { font-size:1.6rem; }
  .pl-tijd-rij { display:flex; gap:.7rem; align-items:center; margin-top:1.1rem;
                 color:var(--dim); font-size:1rem; }
  .pl-tijd-rij input { background:var(--panel); border:1px solid #333a41; color:var(--ink);
                       border-radius:10px; padding:.5rem .7rem; font-size:1.15rem; }
  .pl-balkwrap { margin:1.4rem 0 .4rem; position:relative; }
  .pl-verleden { position:absolute; left:0; top:0; height:96px; width:0;
                 background:rgba(20,23,26,.6); border-radius:14px 0 0 14px; z-index:3;
                 pointer-events:none; transition:width 1s linear; }
  .pl-balk2 { display:flex; gap:.45rem; height:96px; }
  .pl-seg { display:flex; flex-direction:column; align-items:center; justify-content:center;
            gap:.1rem; font-size:.74rem; cursor:pointer; min-width:2.6rem; position:relative;
            overflow:hidden; white-space:nowrap; border-radius:14px; background:var(--panel);
            border:2px solid var(--lijn); transition:flex-grow 1s linear; }
  .pl-seg .em2 { font-size:1.6rem; }
  .pl-plank { display:flex; gap:.5rem; align-items:center; margin:.1rem 0 .9rem;
              min-height:2.9rem; font-size:1.4rem; flex-wrap:wrap; }
  .pl-plank .badge { width:2.6rem; height:2.6rem; border-radius:50%; display:flex;
                     align-items:center; justify-content:center; background:var(--panel);
                     border:2.5px solid var(--accent); font-size:1.35rem; }
  .pl-plank .badge.nieuw { animation:popin .55s cubic-bezier(.2,1.6,.4,1); }
  .pl-plank .leeg-plank { color:var(--dim); font-size:.9rem; font-style:italic; }
  @keyframes popin { from { transform:scale(0); } }
  .stap { width:1.5rem; height:1.5rem; border-radius:6px; border:1px solid var(--lijn);
          background:none; color:var(--ink); cursor:pointer; font-size:.95rem; padding:0;
          vertical-align:middle; }
  .pl-kaart .weg { flex:0 0 auto; background:none; border:none; color:var(--dim);
                   cursor:pointer; font-size:.85rem; opacity:.55; padding:.2rem; }
  .pl-kaart .weg:hover { color:var(--rood); opacity:1; }
  .pl-kaart.nieuw { border-style:dashed; color:var(--dim); cursor:pointer; }
  #plNieuw { position:fixed; inset:0; background:rgba(0,0,0,.55); display:none;
             align-items:center; justify-content:center; z-index:80; }
  #pnKaart { background:var(--panel); border:1px solid var(--lijn); border-radius:16px;
             padding:1.2rem 1.4rem; width:min(430px,92vw); display:flex;
             flex-direction:column; gap:.8rem; }
  #pnEmojis { display:flex; flex-wrap:wrap; gap:.35rem; }
  .pn-em { font-size:1.4rem; width:2.6rem; height:2.6rem; border-radius:10px;
           border:2px solid var(--lijn); background:none; cursor:pointer; padding:0; }
  .pn-em.actief { border-color:var(--accent); background:rgba(127,191,166,.15); }
  #pnNaam { background:var(--bg); border:1px solid #333a41; border-radius:10px;
            color:var(--ink); padding:.65rem .8rem; font-size:1.05rem; }
  .pn-rij { display:flex; align-items:center; gap:.7rem; }
  .pn-rij button.groot { border:none; border-radius:10px; padding:.6rem 1.2rem;
                         background:var(--accent); color:#14171a; font-weight:700;
                         cursor:pointer; font-size:1rem; margin-left:auto; }
  .pl-seg.nu2 { border-color:var(--amber); box-shadow:0 5px 16px rgba(0,0,0,.45);
                transform:translateY(-3px); }
  .pl-seg.bonus { background:rgba(127,191,166,.16) !important; cursor:default;
                  border-style:dashed; border-color:var(--accent); }
  .pl-seg.pad { background:none; border-color:transparent; cursor:default; min-width:0;
                color:var(--dim); opacity:.5; font-size:1.1rem; letter-spacing:.3em; }
  .pl-seg.op { border-color:var(--rood) !important; box-shadow:0 0 0 3px rgba(224,122,106,.25); }
  .pl-rail { position:relative; height:9px; background:var(--panel); border-radius:99px;
             margin:2.1rem .1rem .35rem; border:1px solid var(--lijn); }
  .pl-rail .vul { position:absolute; left:0; top:0; bottom:0; border-radius:99px;
                  background:linear-gradient(90deg, var(--accent), var(--amber));
                  transition:width 1s linear; }
  .pl-rail .stip { position:absolute; top:50%; transform:translate(-50%,-62%);
                   font-size:1.55rem; line-height:1; transition:left 1s linear;
                   filter:drop-shadow(0 2px 3px rgba(0,0,0,.55)); }
  .pl-timer { position:relative; width:180px; margin:1.5rem auto .3rem; }
  .pl-timer svg { display:block; width:180px; height:180px; }
  .ringbg { fill:none; stroke:var(--lijn); stroke-width:9; }
  .ring { fill:none; stroke-width:9; stroke-linecap:round;
          transition:stroke-dashoffset 1s linear; }
  .ring.buiten { stroke:var(--accent); }
  .ring.binnen { stroke:var(--amber); }
  .ring.op { stroke:var(--rood); }
  .pl-timer-tekst { position:absolute; inset:0; display:flex; flex-direction:column;
                    align-items:center; justify-content:center; gap:.15rem; }
  .pl-timer-tekst b { font-size:1.75rem; font-variant-numeric:tabular-nums; }
  .pl-timer-tekst span { color:var(--dim); font-size:.82rem; text-align:center; }
  .pl-rail .nulabel { position:absolute; top:-1.85rem; transform:translateX(-50%);
                      font-size:.9rem; color:var(--rood); font-weight:800;
                      font-variant-numeric:tabular-nums; transition:left 1s linear;
                      white-space:nowrap; }
  .pl-tijden { display:flex; justify-content:space-between; color:var(--dim);
               font-size:.9rem; margin:0 .1rem; font-variant-numeric:tabular-nums; }
  .pl-bonuskaart { margin-top:.9rem; background:rgba(127,191,166,.1); border:2px solid var(--accent);
                   border-radius:16px; padding:1rem 1.1rem; display:flex; align-items:center;
                   gap:.9rem; font-size:1.15rem; position:relative; overflow:hidden; }
  .pl-bonuskaart .em { font-size:2rem; }
  .pl-bonuskaart b { margin-left:auto; color:var(--accent); font-size:1.35rem; white-space:nowrap; }
  .pl-bonuskaart .balk { position:absolute; left:0; bottom:0; height:6px; background:var(--accent);
                         transition:width 1s linear; }
  @media (max-width:560px){ .pl-opzet { gap:.5rem; } .pl-kaart.mini { font-size:.95rem; } }
  #plCanvas { position:fixed; inset:0; pointer-events:none; z-index:90; }
  #plKlaar { position:fixed; inset:0; background:rgba(20,23,26,.88); display:none;
             align-items:center; justify-content:center; flex-direction:column; gap:1rem;
             z-index:85; text-align:center; }
  #plKlaar h1 { font-size:clamp(2.2rem,8vw,4rem); }
  #plKlaar p { color:var(--dim); font-size:1.2rem; }
  #l2 { position:fixed; inset:0; background:rgba(0,0,0,.55); display:none;
        align-items:center; justify-content:center; z-index:65; }
  #l2kaart { background:var(--panel); border:1px solid var(--lijn); border-radius:16px;
             padding:1.1rem 1.4rem; width:min(920px,96vw); max-height:88vh; overflow-y:auto; }
  .fchips { display:flex; gap:.4rem; flex-wrap:wrap; margin:.7rem 0 .5rem; }
  .fchip { border:1px solid var(--lijn); background:none; color:var(--dim); border-radius:99px;
           padding:.28rem .85rem; font-size:.85rem; cursor:pointer; text-transform:capitalize; }
  .fchip.actief { background:var(--accent); color:#14171a; border-color:var(--accent);
                  font-weight:600; }
  .sectiegrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
                gap:0 1.6rem; }
  .vraagknop { flex:0 0 auto; align-self:center; border:1px solid #3a4148; background:none;
               border-radius:8px; padding:.15rem .5rem; cursor:pointer; font-size:.85rem;
               opacity:.75; }
  .vraagknop:hover, .vraagknop:active { border-color:var(--accent); opacity:1; }
  #l2kop { display:flex; align-items:baseline; }
  #l2kop h3 { font-size:1.1rem; }
  #l2kop button { margin-left:auto; background:none; border:none; color:var(--dim);
                  font-size:1.2rem; cursor:pointer; }
  #l2Inhoud h4 { font-size:.78rem; letter-spacing:.08em; text-transform:uppercase;
                 color:var(--amber); margin:.9rem 0 .35rem; }
  #l2Inhoud ul { list-style:none; padding:0; }
  #l2Inhoud li { padding:.28rem 0; font-size:.97rem; line-height:1.4; display:flex;
                 gap:.5rem; align-items:baseline; }
  #l2Inhoud li small { color:var(--dim); font-variant-numeric:tabular-nums; flex:0 0 3.1rem; }
  #l2Inhoud li small.waarde { flex:0 0 auto; white-space:nowrap; color:var(--ink);
                              margin-left:auto; font-size:.92rem; }
  #l2Inhoud li span { flex:1; min-width:0; }
  #l2Inhoud .notitie { color:var(--dim); font-style:italic; font-size:.88rem; }
  #l2Inhoud .afitem span { text-decoration:line-through; color:var(--dim); }
  #taakvraag { position:fixed; inset:0; background:rgba(0,0,0,.55); display:none;
               align-items:center; justify-content:center; z-index:75; }
  #tvKaart { background:var(--panel); border:1px solid var(--accent); border-radius:16px;
             padding:1.1rem 1.3rem; width:min(500px,94vw); max-height:80vh; overflow-y:auto;
             display:flex; flex-direction:column; gap:.6rem; }
  #tvKaart h3 { font-size:1.05rem; }
  #tvAntwoord { background:#262b31; border-radius:12px; padding:.7rem .9rem; font-size:.95rem;
                line-height:1.5; white-space:pre-wrap; display:none; }
  #tvInvoer { display:flex; gap:.45rem; }
  #tvInvoer input { flex:1; background:var(--bg); border:1px solid #333a41; border-radius:10px;
                    color:var(--ink); padding:.6rem .8rem; font-size:.95rem; }
  #tvInvoer button { border:none; border-radius:10px; padding:0 .85rem; font-size:1.1rem;
                     cursor:pointer; background:var(--accent); color:#14171a; }
  #detail { position:fixed; inset:0; background:rgba(0,0,0,.55); display:none;
            align-items:center; justify-content:center; z-index:70; }
  #detailkaart { background:var(--panel); border:1px solid var(--lijn); border-radius:16px;
                 padding:1.2rem 1.4rem; max-width:min(460px,90vw); max-height:80vh;
                 overflow-y:auto; }
  #detailkaart h3 { margin-bottom:.6rem; font-size:1.15rem; }
  #dRegels div { padding:.18rem 0; font-size:.95rem; color:var(--dim); }
  #dRegels .omschr { color:var(--ink); white-space:pre-wrap; margin-top:.5rem;
                     border-top:1px solid var(--lijn); padding-top:.6rem; line-height:1.5; }
  #detailkaart button { margin-top:.9rem; border:none; border-radius:10px; padding:.45rem 1.1rem;
                        background:var(--accent); color:#14171a; cursor:pointer; font-size:.95rem; }
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
      <button id="tabPlan" onclick="kiesTab('plan')">Planning</button>
    </div>
    <div id="klok"></div>
  </header>
  <div id="tekstinvoer">
    <input id="invoer" placeholder="Zeg of typ iets tegen Birdy — “voeg kwark toe aan de boodschappen”">
    <button class="micknop" onclick="spraak(this)" title="Praat tegen Birdy">🎤</button>
    <button id="stuurknop" onclick="stuur(document.getElementById('invoer').value)">→</button>
  </div>
  <div class="lay" id="paneelVandaag">
    <aside class="zijbalk">
      <div class="mini" onclick="openL2('onderwerpen')">
        <h3>📂 Onderwerpen <b id="moCount"></b></h3><ul id="moList"></ul></div>
      <div class="mini" onclick="openL2('boodschappen')">
        <h3>🛒 Boodschappen <b id="mbCount"></b></h3><ul id="mbList"></ul></div>
      <div class="mini" onclick="openL2('verjaardagen')">
        <h3>🎂 Verjaardagen</h3><ul id="mvList"></ul></div>
      <div class="mini" onclick="openL2('regelzaken')">
        <h3>🔁 Regelzaken</h3><ul id="mrList"></ul></div>
      <div class="mini" id="miniThuis" style="display:none" onclick="openL2('thuis')">
        <h3>🏠 Thuis</h3><ul id="mtList"></ul></div>
    </aside>
    <div class="hoofd">
      <div class="panel"><h2 class="klik" onclick="kiesTab('week')" title="Naar weekoverzicht">📅 Agenda ↗</h2><ul id="agenda"></ul></div>
      <div class="panel aandacht"><h2 class="klik" onclick="openL2('aandacht')">💡 Aandacht ↗</h2><ul id="aandacht"></ul><div class="rest" id="aandachtrest"></div></div>
      <div class="panel"><h2 class="klik" onclick="openL2('acties')">⚡ Acties ↗</h2><ul id="acties"></ul>
        <div class="toevoeg"><input placeholder="+ toevoegen…" enterkeyhint="done"
          onkeydown="voegToe(event,'acties',this)"></div>
        <details class="af" id="actiesAfWrap"><summary>onlangs afgevinkt</summary>
          <ul id="actiesAf"></ul></details></div>
    </div>
  </div>
  <div id="paneelWeek">
    <div class="wkwrap"><div class="wk" id="wkgrid"></div></div>
    <div class="legenda" id="legenda"></div>
  </div>
  <div id="paneelPlan"></div>
</div>
<div id="melding"></div>
<canvas id="plCanvas"></canvas>
<div id="plKlaar" onclick="this.style.display='none'">
  <h1>🎉 Goed gedaan! 🎉</h1><p>Alles is af — wat ging dat snel!</p></div>
<div id="plNieuw" onclick="this.style.display='none'">
  <div id="pnKaart" onclick="event.stopPropagation()">
    <h3>➕ Nieuw taakje</h3>
    <div id="pnEmojis"></div>
    <input id="pnNaam" placeholder="Hoe heet het taakje?" maxlength="30">
    <div class="pn-rij"><span>Duur:</span>
      <button class="stap" onclick="plNieuwMinStap(-1)">−</button>
      <b id="pnMin"></b>
      <button class="stap" onclick="plNieuwMinStap(1)">+</button>
      <button class="groot" onclick="plNieuwToevoegen()">Toevoegen</button></div>
  </div>
</div>
<div id="l2" onclick="sluitL2()">
  <div id="l2kaart" onclick="event.stopPropagation()">
    <div id="l2kop"><h3 id="l2Titel"></h3><button onclick="sluitL2()" title="Sluiten">✕</button></div>
    <div id="l2Inhoud"></div>
  </div>
</div>
<div id="taakvraag" onclick="sluitTaakVraag()">
  <div id="tvKaart" onclick="event.stopPropagation()">
    <h3 id="tvTitel"></h3>
    <div id="tvNotitie" class="notitie"></div>
    <div id="tvAntwoord"></div>
    <div id="tvInvoer">
      <input id="tvVeld" placeholder="Vraag Birdy iets over deze taak…" enterkeyhint="send">
      <button class="micknop" onclick="spraak(this, tvStuur)" title="Spreek je vraag in">🎤</button>
      <button onclick="tvStuur(document.getElementById('tvVeld').value)">→</button>
    </div>
  </div>
</div>
<div id="detail" onclick="this.style.display='none'">
  <div id="detailkaart" onclick="event.stopPropagation()">
    <h3 id="dTitel"></h3>
    <div id="dRegels"></div>
    <button onclick="document.getElementById('detail').style.display='none'">Sluiten</button>
  </div>
</div>
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
  document.getElementById('paneelPlan').style.display = t === 'plan' ? 'block' : 'none';
  document.getElementById('tabVandaag').classList.toggle('actief', t === 'vandaag');
  document.getElementById('tabWeek').classList.toggle('actief', t === 'week');
  document.getElementById('tabPlan').classList.toggle('actief', t === 'plan');
  try { localStorage.setItem('birdy-tab', t); } catch(e){}
  if (t === 'plan') renderPlan();
}

function vul(id, items, maak){ const el = document.getElementById(id);
  el.innerHTML = items.length ? items.map(maak).join('') : '<li class="leeg">niets 🎉</li>'; }
function vulMeer(id, items, maak, max, l2){
  const el = document.getElementById(id);
  let html = items.length ? items.slice(0, max).map(maak).join('')
                          : '<li class="leeg">niets 🎉</li>';
  if (items.length > max)
    html += `<li class="leeg klik" onclick="openL2('${l2}')">… nog ${items.length - max} — alles ↗</li>`;
  el.innerHTML = html;
}
function esc(s){ const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function taakRij(x){
  const p = persoonMatch(x.tekst);
  return `<li class="vink"${p ? ` style="--pc:${p.kleur}"` : ''} onclick="vink(this,'${x.id}')">` +
    `<span>${esc(x.tekst)}${dueBadge(x.due)}</span>` +
    (x.due ? '' : `<button class="duebtn" title="deadline prikken"` +
      ` onclick="event.stopPropagation();kiesDatum('${x.id}')">+</button>`) + `</li>`;
}
function afRij(x){
  return `<li class="afitem"><span>${esc(x.tekst)}</span>` +
    `<button class="herstelknop" title="terugzetten" onclick="herstel('${x.id}')">↩</button></li>`;
}
function jarigRij(j){
  return `<li><small>${j.datum}</small><span>${esc(j.naam)} ` +
    `<b>${j.dagen===0 ? 'vandaag! 🎉' : 'over ' + j.dagen + ' dgn'}</b></span></li>`;
}

// ── verdiepende pagina's (L2) ─────────────────────────────────────────────
let DATA = null, L2open = null, L2filter = 'alle';
function openL2(naam){ L2open = naam; L2filter = 'alle';
  document.getElementById('l2').style.display = 'flex'; renderL2(); }
function sluitL2(){ L2open = null; document.getElementById('l2').style.display = 'none'; }
function zetFilter(f){ L2filter = f; renderL2(); }
function filterChips(){
  const namen = ['alle', ...PERSONEN, 'overig'];
  return `<div class="fchips">` + namen.map(n =>
    `<button class="fchip${L2filter === n ? ' actief' : ''}"` +
    ` onclick="zetFilter('${n}')">${esc(n)}</button>`).join('') + `</div>`;
}
function filterItems(items, tekstVan){
  if (L2filter === 'alle') return items;
  return items.filter(x => {
    const p = persoonMatch(tekstVan(x));
    return L2filter === 'overig' ? !p : (p && p.naam === L2filter);
  });
}
function kw(w){ return (Math.round((w || 0) / 100) / 10).toFixed(1).replace('.', ',') + ' kW'; }
// P1-meter (net_w): + = afnemen van het net, − = terugleveren. Huisverbruik = zon + net.
function energie(th){
  const zon = th.zon_w, net = th.net_w;
  return {
    zon,
    huis: (zon !== null && net !== null) ? Math.max(0, zon + net) : (zon === null ? net : null),
    terug: net !== null && net < 0 ? -net : 0,
    vanNet: net !== null && net > 0 ? net : 0,
  };
}
function nettoLabel(th){
  if (th.net_w === null) return '';
  const e = energie(th);
  return e.terug > 0
    ? `<small class="nu">↑ ${kw(e.terug)}</small>`
    : `<small>↓ ${kw(e.vanNet)}</small>`;
}
async function lampUit(id){
  try {
    const r = await fetch('/api/homey/lamp', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ id, aan: false }) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'fout');
    toon('💡 Uit gezet'); ververs();
  } catch(e){ toon('Lamp schakelen lukte niet: ' + e.message); }
}
function signaalRij(s){
  const actie = s.l2 === 'week' ? "kiesTab('week')" : `openL2('${s.l2}')`;
  return `<li class="signaal ernst${s.ernst}" onclick="${actie}"><span>${esc(s.tekst)}</span></li>`;
}
function dagenLabel(d){
  if (d === null || d === undefined) return '';
  if (d < 0) return 'te laat';
  if (d === 0) return 'vandaag';
  if (d === 1) return 'morgen';
  return 'over ' + d + ' d';
}
async function regelzaakGedaan(i){
  const z = (DATA.regelzaken || [])[i]; if (!z) return;
  const tekst = `We hebben net "${z.naam}" gedaan. Werk het huishoudhandboek bij: laatst = vandaag,` +
    ` volgende = vandaag + het interval. Bevestig kort.`;
  chatVoeg('ik', `✓ ${z.naam} gedaan`);
  toon(`✓ Doorgegeven aan Birdy: "${z.naam}" is gedaan — handboek wordt bijgewerkt…`);
  try {
    const r = await fetch('/api/message', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ text: tekst }) });
    const d = await r.json();
    chatVoeg('birdy', d.reply || d.error || 'er ging iets mis');
    toon('🐦 ' + (d.reply || d.error || 'er ging iets mis'));
    ververs();
  } catch(e){ toon('Doorgeven lukte even niet — probeer nog eens.'); }
}
function taakRijL2(x){
  const p = persoonMatch(x.tekst);
  return `<li class="vink"${p ? ` style="--pc:${p.kleur}"` : ''} onclick="vink(this,'${x.id}')">` +
    `<span>${esc(x.tekst)}${dueBadge(x.due)}` +
    (x.notitie ? `<br><span class="notitie">${esc(x.notitie)}</span>` : '') + `</span>` +
    `<button class="vraagknop" title="vraag Birdy hierover"` +
    ` onclick="event.stopPropagation();vraagOver('${L2open}','${x.id}')">💬</button>` +
    (x.due ? '' : `<button class="duebtn" title="deadline prikken"` +
      ` onclick="event.stopPropagation();kiesDatum('${x.id}')">+</button>`) + `</li>`;
}
let TV = null;  // open taak-dialoog: {lijst, id}
function vraagOver(lijst, id){
  const x = (DATA[lijst] || []).find(t => t.id === id); if (!x) return;
  TV = { lijst, id };
  document.getElementById('tvTitel').textContent = '💬 ' + x.tekst;
  const n = document.getElementById('tvNotitie');
  n.textContent = x.notitie || ''; n.style.display = x.notitie ? 'block' : 'none';
  const a = document.getElementById('tvAntwoord');
  a.textContent = ''; a.style.display = 'none';
  document.getElementById('tvVeld').value = '';
  document.getElementById('taakvraag').style.display = 'flex';
  document.getElementById('tvVeld').focus();
}
function sluitTaakVraag(){ TV = null; document.getElementById('taakvraag').style.display = 'none'; }
async function tvStuur(vraag){
  vraag = (vraag || '').trim(); if (!vraag || !TV) return;
  const x = (DATA[TV.lijst] || []).find(t => t.id === TV.id); if (!x) return;
  document.getElementById('tvVeld').value = '';
  const a = document.getElementById('tvAntwoord');
  a.style.display = 'block'; a.textContent = '🐦 …denkt na…';
  const soort = TV.lijst === 'acties' ? 'actie' : 'boodschap';
  try {
    const r = await fetch('/api/message', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ text: `Over de ${soort} "${x.tekst}": ${vraag}` }) });
    const d = await r.json();
    a.textContent = '🐦 ' + (d.reply || d.error || 'er ging iets mis');
    await ververs();  // vernieuwde notitie ophalen (L2 eronder ververst mee)
    if (TV){
      const nx = (DATA[TV.lijst] || []).find(t => t.id === TV.id);
      const n = document.getElementById('tvNotitie');
      if (nx && nx.notitie){ n.textContent = nx.notitie; n.style.display = 'block'; }
    }
  } catch(e){ a.textContent = '🐦 Even niet bereikbaar — probeer het zo nog eens.'; }
}
function renderL2(){
  if (!L2open || !DATA) return;
  document.getElementById('l2Titel').textContent = {
    onderwerpen: '📂 Onderwerpen — wat loopt er', aandacht: '💡 Aandacht',
    boodschappen: '🛒 Boodschappen',
    acties: '⚡ Acties — alles', verjaardagen: '🎂 Verjaardagen & cadeau-ideeën',
    regelzaken: '🔁 Regelzaken — huishoudhandboek', thuis: '🏠 Thuis — via Homey',
  }[L2open];
  let html = '';
  if (L2open === 'thuis'){
    const th = DATA.thuis;
    if (!th){ document.getElementById('l2Inhoud').innerHTML = '<p class="leeg">Homey is even niet bereikbaar.</p>'; return; }
    html += '<h4>⚡ Energie</h4><ul>';
    const e = energie(th);
    if (e.zon !== null) html += `<li><span>☀️ Zonnepanelen leveren</span><small class="waarde">${kw(e.zon)}</small></li>`;
    if (e.huis !== null) html += `<li><span>🏠 Huis verbruikt</span><small class="waarde">${kw(e.huis)}</small></li>`;
    if (th.net_w !== null){
      html += e.terug > 0
        ? `<li><span>↑ Teruglevering aan het net</span><small class="waarde">${kw(e.terug)}</small></li>`
        : `<li><span>↓ Afname van het net</span><small class="waarde">${kw(e.vanNet)}</small></li>`;
    }
    html += '</ul>';
    if ((th.klimaat || []).length){
      html += '<h4>🌡️ Klimaat</h4><ul>' + th.klimaat.map(k =>
        `<li><span>${esc(k.kamer)}</span><small class="waarde">${k.temp}°${k.doel !== null && k.doel !== undefined ? ' · doel ' + k.doel + '°' : ''}</small></li>`).join('') + '</ul>';
    }
    const lampen = th.lampen_aan || [];
    html += `<h4>💡 Lampen aan (${lampen.length})</h4><ul>` + (lampen.length ? lampen.map(l =>
      `<li><span>${esc(l.naam)}${l.kamer ? ` <span class="notitie">· ${esc(l.kamer)}</span>` : ''}</span>` +
      `<button class="herstelknop" onclick="lampUit('${l.id}')">uit</button></li>`).join('')
      : '<li class="leeg">alles uit 🌙</li>') + '</ul>';
    const app = [];
    if (th.auto) app.push(`<li><span>🚗 ${esc(th.auto.naam)}</span><small class="waarde">${th.auto.batterij ?? '?'}%${th.auto.laadt ? ' · laadt ⚡' : ''}</small></li>`);
    if (th.deur) app.push(`<li><span>🚪 ${esc(th.deur.naam)}</span><small class="waarde">${th.deur.dicht === true ? '🔒 op slot' : th.deur.dicht === false ? '🔓 open' : 'onbekend'}</small></li>`);
    if (th.stofzuiger) app.push(`<li><span>🧹 ${esc(th.stofzuiger.naam)}</span><small class="waarde">${th.stofzuiger.batterij ?? '?'}%</small></li>`);
    if (th.tv_aan !== null && th.tv_aan !== undefined) app.push(`<li><span>📺 TV</span><small class="waarde">${th.tv_aan ? 'aan' : 'uit'}</small></li>`);
    if (app.length) html += '<h4>🔌 Apparaten</h4><ul>' + app.join('') + '</ul>';
    html += `<p class="notitie" style="margin-top:.8rem">${th.aantal} apparaten via Homey Pro</p>`;
    document.getElementById('l2Inhoud').innerHTML = html; return;
  }
  if (L2open === 'regelzaken'){
    const items = DATA.regelzaken || [];
    html = '<ul>' + (items.length ? items.map((z, i) =>
      `<li><span>${esc(z.naam)}${z.wie ? persChip(z.wie) : ''}` +
      `<span class="due ${z.dagen !== null && z.dagen < 0 ? 'laat' : z.dagen === 0 ? 'nu' : 'straks'}">` +
      `${dagenLabel(z.dagen) || 'geen datum'}</span><br>` +
      `<span class="notitie">${esc([z.elke ? 'elke ' + z.elke : '', z.laatst ? 'laatst ' + z.laatst : '',
        z.volgende ? 'volgende ' + z.volgende : ''].filter(Boolean).join(' · '))}</span></span>` +
      `<button class="herstelknop" title="net gedaan — Birdy werkt het handboek bij"` +
      ` onclick="regelzaakGedaan(${i})">✓ gedaan</button></li>`).join('')
      : '<li class="leeg">Nog geen regelzaken — zeg tegen Birdy: "de grijze bak gaat elke 2 weken aan straat".</li>') + '</ul>';
    document.getElementById('l2Inhoud').innerHTML = html; return;
  }
  if (L2open === 'onderwerpen'){
    const items = filterItems(DATA.onderwerpen || [], o => o.naam + ' ' + o.wie);
    html = filterChips() + '<ul>' + (items.length ? items.map(o =>
      `<li><span>${esc(o.naam)}${o.wie ? persChip(o.wie) : ''}` +
      (o.dagen !== null ? `<span class="due ${o.dagen < 0 ? 'laat' : o.dagen <= 1 ? 'nu' : 'straks'}">` +
        `${esc(o.wanneer)}${o.dagen === 0 ? ' · vandaag' : o.dagen === 1 ? ' · morgen' : o.dagen < 0 ? ' · te laat' : ''}</span>`
        : (o.wanneer ? `<span class="due straks">${esc(o.wanneer)}</span>` : '')) +
      (o.stap ? `<br><span class="notitie">→ ${esc(o.stap)}</span>` : '') +
      (o.notitie ? `<br><span class="notitie">${esc(o.notitie)}</span>` : '') +
      `</span></li>`).join('')
      : '<li class="leeg">niets voor dit filter</li>') + '</ul>';
    html += `<p class="notitie" style="margin-top:.8rem">Uit het Google Doc “Wat loopt er” in de Drive-map — Birdy houdt het bij; zelf aanpassen mag ook.</p>`;
  } else if (L2open === 'aandacht'){
    const a = DATA.aandacht || { birdy: { items: [] }, signalen: [] };
    const b = a.birdy || { items: [] };
    html = `<h4><img src="/logo-bird.png" class="bird" onerror="this.replaceWith('🐦')"> Wat Birdy opviel` +
      (b.tijd ? ` <span class="notitie">· briefing van ${esc(b.tijd)}</span>` : '') + '</h4><ul>' +
      (b.items.length ? b.items.map(x => `<li class="birdy"><img src="/logo-bird.png" class="bird" onerror="this.replaceWith('🐦')"><span>${esc(x)}</span></li>`).join('')
        : '<li class="leeg">nog niets — komt bij de volgende ochtendbriefing</li>') + '</ul>';
    html += '<h4>Signalen uit agenda, acties en handboek</h4><ul>' +
      ((a.signalen || []).length ? a.signalen.map(signaalRij).join('') : '<li class="leeg">niets dat aandacht vraagt 🙂</li>') + '</ul>';
  } else if (L2open === 'boodschappen' || L2open === 'acties'){
    const items = filterItems(DATA[L2open] || [], x => x.tekst);
    html = filterChips() + '<ul>' +
      (items.length ? items.map(taakRijL2).join('') : '<li class="leeg">niets voor dit filter</li>') +
      '</ul>';
    html += `<div class="toevoeg"><input placeholder="+ toevoegen…" enterkeyhint="done"
      onkeydown="voegToe(event,'${L2open}',this)"></div>`;
    const af = filterItems(DATA[L2open + '_af'] || [], x => x.tekst);
    if (af.length) html += '<h4>↩ Onlangs afgevinkt</h4><ul>' + af.map(afRij).join('') + '</ul>';
  } else if (L2open === 'verjaardagen'){
    const items = DATA.verjaardagen || [];
    html = '<ul>' + (items.length ? items.map(j =>
      `<li><small>${j.datum}</small><span>${esc(j.naam)} ` +
      `<b>${j.dagen===0 ? 'vandaag! 🎉' : 'over ' + j.dagen + ' dgn'}</b>` +
      (j.notitie ? `<br><span class="notitie">${esc(j.notitie)}</span>` : '') +
      `</span></li>`).join('') : '<li class="leeg">nog geen verjaardagen — zeg ze tegen Birdy!</li>') + '</ul>';
  }
  document.getElementById('l2Inhoud').innerHTML = html;
}

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
let PERSONEN = [], WEEK = [];
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
  events.forEach((e, i) => {
    e._i = i;
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
        ` onclick="detailEv(${e._i})">${esc(e.titel)}</div>`;
    }).join('') + `</div>`;
  });
  let uuras = '<div class="uuras">';
  for (let u = U0; u <= U1; u += 2)
    uuras += `<div style="top:${(u-U0)*PPU}px">${String(u).padStart(2,'0')}</div>`;
  html += uuras + '</div>';
  volgorde.forEach(key => {
    const g = dagen[key];
    const t = g.tijd.map(e => ({ ev: e, s: minuten(e.start),
      e: (e.eind && e.eind.length > 10) ? Math.max(minuten(e.eind), minuten(e.start) + 30)
                                        : minuten(e.start) + 60 }));
    t.sort((a,b) => a.s - b.s || b.e - a.e);
    // kolomtoewijzing zoals een echte kalender: overlappers netjes naast elkaar
    const actief = [];
    t.forEach(x => {
      for (let i = actief.length - 1; i >= 0; i--) if (actief[i].e <= x.s) actief.splice(i, 1);
      const bezet = new Set(actief.map(a => a.col));
      x.col = 0; while (bezet.has(x.col)) x.col++;
      actief.push(x);
    });
    let ci = 0;  // clusters van aaneengesloten overlap → gedeelde kolombreedte
    while (ci < t.length){
      let cj = ci, eind = t[ci].e;
      while (cj + 1 < t.length && t[cj + 1].s < eind){ cj++; eind = Math.max(eind, t[cj].e); }
      const groep = t.slice(ci, cj + 1);
      const cols = Math.max(...groep.map(y => y.col)) + 1;
      groep.forEach(y => { y.cols = cols; });
      ci = cj + 1;
    }
    let vak = '<div class="tijdvak">';
    for (let u = U0; u <= U1; u += 2) vak += `<div class="uurlijn" style="top:${(u-U0)*PPU}px"></div>`;
    t.forEach(x => {
      const top = Math.max(0, (x.s/60 - U0) * PPU);
      const hoogte = Math.max(24, Math.min(HOOG - top - 2, (x.e - x.s) / 60 * PPU - 2));
      const k = kleurVoor(x.ev.titel);
      const breedte = 100 / x.cols;
      const tijd = x.ev.start.slice(11,16) +
        ((x.ev.eind && x.ev.eind.length > 10) ? '–' + x.ev.eind.slice(11,16) : '');
      const sleepbaar = !!x.ev.id;
      vak += `<div class="blok${x.cols > 1 ? ' conflict' : ''}${sleepbaar ? ' sleepbaar' : ''}"` +
        ` style="top:${top}px;height:${hoogte}px;border-color:${k};background:${k}26;` +
        `left:calc(${x.col * breedte}% + 3px);width:calc(${breedte}% - 6px);color:var(--ink)"` +
        ` onclick="if(!onderdruktKlik)detailEv(${x.ev._i})"` +
        (sleepbaar ? ` onpointerdown="blokDown(event,${x.ev._i})"` +
          ` onpointermove="blokMove(event)" onpointerup="blokUp(event)"` +
          ` onpointercancel="blokUp(event)"` : '') + `>` +
        `<b>${esc(x.ev.titel)}</b><small>${tijd}</small></div>`;
    });
    html += vak + '</div>';
  });
  return html;
}
// ── afspraken verslepen (alleen Google; FamilyWall is alleen-lezen) ──
let sleepData = null, onderdruktKlik = false;
function blokDown(ev, i){
  sleepData = { i, el: ev.currentTarget, x0: ev.clientX, y0: ev.clientY, dx: 0, dy: 0, bezig: false };
  try { ev.currentTarget.setPointerCapture(ev.pointerId); } catch(e){}
}
function blokMove(ev){
  if (!sleepData) return;
  sleepData.dx = ev.clientX - sleepData.x0;
  sleepData.dy = ev.clientY - sleepData.y0;
  if (!sleepData.bezig && Math.abs(sleepData.dx) + Math.abs(sleepData.dy) > 8){
    sleepData.bezig = true;
    sleepData.el.classList.add('sleept');
  }
  if (sleepData.bezig)
    sleepData.el.style.transform = `translate(${sleepData.dx}px, ${sleepData.dy}px)`;
}
function blokUp(ev){
  if (!sleepData) return;
  const s = sleepData; sleepData = null;
  s.el.style.transform = ''; s.el.classList.remove('sleept');
  if (!s.bezig) return;  // gewone tik → onclick opent de detailkaart
  onderdruktKlik = true; setTimeout(() => { onderdruktKlik = false; }, 300);
  const kolomBreedte = s.el.parentElement.getBoundingClientRect().width + 6;
  const dagen = Math.round(s.dx / kolomBreedte);
  const minuten = Math.round((s.dy / PPU) * 60 / 15) * 15;  // per kwartier
  if (dagen === 0 && minuten === 0) return;
  const e = WEEK[s.i];
  const oudS = e.start, oudE = e.eind;
  verzetNaar(s.i, schuifIso(e.start, dagen, minuten),
             (e.eind && e.eind.length > 10) ? schuifIso(e.eind, dagen, minuten) : '',
             () => verzetNaar(s.i, oudS, oudE, null));
}
function schuifIso(iso, dagen, minuten){
  const d = new Date(iso);
  d.setDate(d.getDate() + dagen); d.setMinutes(d.getMinutes() + minuten);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}
async function verzetNaar(i, nieuwS, nieuwE, undo){
  const e = WEEK[i];
  nieuwE = nieuwE || nieuwS;
  const oudS = e.start, oudE = e.eind;
  e.start = nieuwS; e.eind = nieuwE; renderWeek(WEEK);  // optimistisch
  try {
    const r = await fetch('/api/verzet', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ id: e.id, start: nieuwS, eind: nieuwE }) });
    if (!r.ok) throw new Error();
    const label = `${dagLabel(nieuwS.slice(0,10))} ${nieuwS.slice(11,16)}`;
    if (undo) toonMetKnop(`📅 “${e.titel}” verzet naar ${label}`, 'Ongedaan maken', undo);
    else toon(`📅 “${e.titel}” staat weer op ${label}`);
  } catch(err){
    e.start = oudS; e.eind = oudE; renderWeek(WEEK);
    toon('Verzetten lukte even niet — probeer nog eens.');
  }
}
function detailEv(i){
  const e = WEEK[i]; if (!e) return;
  document.getElementById('dTitel').textContent = e.titel;
  const tijd = e.start.length > 10
    ? `${dagLabel(e.start.slice(0,10))} · ${e.start.slice(11,16)}` +
      ((e.eind && e.eind.length > 10) ? '–' + e.eind.slice(11,16) : '')
    : `${dagLabel(e.start.slice(0,10))} · hele dag`;
  let rows = `<div>🕐 ${esc(tijd)}</div>`;
  if (e.locatie) rows += `<div>📍 ${esc(e.locatie)}</div>`;
  if (e.wie) rows += `<div>👤 ${esc(e.wie)}</div>`;
  if (e.bron) rows += `<div>🔗 ${esc(e.bron)}</div>`;
  if (e.omschrijving) rows += `<div class="omschr">${esc(e.omschrijving)}</div>`;
  document.getElementById('dRegels').innerHTML = rows;
  document.getElementById('detail').style.display = 'flex';
}

async function ververs(){
  try {
    const r = await fetch('/api/overview', { headers: { 'X-Dashboard-Key': KEY } });
    if (r.status === 401) { toonSleutel(); return; }
    const d = await r.json();
    document.getElementById('sleutel').style.display = 'none';
    document.getElementById('app').style.display = 'block';
    document.getElementById('klok').textContent = d.nu;
    DATA = d;
    PERSONEN = d.personen || [];
    WEEK = d.week || [];
    document.getElementById('agenda').innerHTML = agendaHtml(d.agenda);
    renderWeek(WEEK);
    // aandacht: eerst Birdy's punten (herkenbaar), dan de regel-signalen; samen max 5
    const a = d.aandacht || { birdy: { items: [] }, signalen: [] };
    const birdyRows = ((a.birdy && a.birdy.items) || []).map(x =>
      `<li class="birdy"><img src="/logo-bird.png" class="bird" onerror="this.replaceWith('🐦')"><span>${esc(x)}</span></li>`);
    const sig = a.signalen || [];
    const sigRows = sig.slice(0, Math.max(2, 5 - birdyRows.length)).map(signaalRij);
    const rows = birdyRows.concat(sigRows);
    document.getElementById('aandacht').innerHTML =
      rows.length ? rows.join('') : '<li class="leeg">niets dat aandacht vraagt 🙂</li>';
    const restN = sig.length - sigRows.length;
    document.getElementById('aandachtrest').innerHTML =
      (a.birdy && a.birdy.tijd ? `<img src="/logo-bird.png" class="bird" onerror="this.remove()">Birdy · ${esc(a.birdy.tijd.slice(-5))}` : '') +
      (restN > 0 ? `${a.birdy && a.birdy.tijd ? ' · ' : ''}nog ${restN} signa${restN > 1 ? 'len' : 'al'} ↗` : '');
    // zijbalk: onderwerpen
    const mo = d.onderwerpen || [];
    document.getElementById('moCount').textContent = mo.length || '';
    document.getElementById('moList').innerHTML = mo.length
      ? mo.slice(0, 3).map(o => `<li><span>${esc(o.naam)}</span>` +
          `<small class="${o.dagen !== null && o.dagen < 0 ? 'laat' : o.dagen === 0 || o.dagen === 1 ? 'nu' : ''}">` +
          `${o.dagen !== null ? dagenLabel(o.dagen) : esc(o.wanneer || '')}</small></li>`).join('') +
        (mo.length > 3 ? `<li class="meer">… nog ${mo.length - 3}</li>` : '')
      : '<li class="leeg">niets lopends 🎉</li>';
    vulMeer('acties', d.acties || [], taakRij, 12, 'acties');
    const afWrap = document.getElementById('actiesAfWrap');
    afWrap.style.display = (d.acties_af && d.acties_af.length) ? 'block' : 'none';
    document.getElementById('actiesAf').innerHTML = (d.acties_af || []).map(afRij).join('');
    // zijbalk: compacte mini-kaarten
    const mb = d.boodschappen || [];
    document.getElementById('mbCount').textContent = mb.length || '';
    document.getElementById('mbList').innerHTML = mb.length
      ? mb.slice(0, 3).map(x => `<li><span>${esc(x.tekst)}</span></li>`).join('') +
        (mb.length > 3 ? `<li class="meer">… nog ${mb.length - 3}</li>` : '')
      : '<li class="leeg">lijst is leeg 🎉</li>';
    const mv = d.verjaardagen || [];
    document.getElementById('mvList').innerHTML = mv.length
      ? mv.slice(0, 2).map(j => `<li><span>${esc(j.naam)}</span>` +
          `<small class="${j.dagen === 0 ? 'nu' : ''}">${dagenLabel(j.dagen)}</small></li>`).join('')
      : '<li class="leeg">geen bekend</li>';
    const mr = d.regelzaken || [];
    document.getElementById('mrList').innerHTML = mr.length
      ? mr.slice(0, 3).map(z => `<li><span>${esc(z.naam)}</span>` +
          `<small class="${z.dagen !== null && z.dagen < 0 ? 'laat' : z.dagen === 0 ? 'nu' : ''}">` +
          `${dagenLabel(z.dagen)}</small></li>`).join('') +
        (mr.length > 3 ? `<li class="meer">… nog ${mr.length - 3}</li>` : '')
      : '<li class="leeg">handboek nog leeg</li>';
    const th = d.thuis;
    const miniThuis = document.getElementById('miniThuis');
    miniThuis.style.display = th ? 'block' : 'none';
    if (th){
      const rijen = [];
      if (th.zon_w !== null || th.net_w !== null){
        const e = energie(th);
        let r = [e.zon !== null ? `☀️ ${kw(e.zon)}` : '', e.huis !== null ? `🏠 ${kw(e.huis)}` : '']
                  .filter(Boolean).join(' · ');
        rijen.push(`<li><span>${r}</span>${nettoLabel(th)}</li>`);
      }
      const woon = (th.klimaat || []).find(k => /woon/i.test(k.kamer)) || (th.klimaat || [])[0];
      if (woon) rijen.push(`<li><span>🌡️ ${woon.temp}° ${esc(woon.kamer)}</span></li>`);
      if (th.auto) rijen.push(`<li><span>🚗 ${esc(th.auto.naam)}</span>` +
        `<small>${th.auto.batterij ?? '?'}%${th.auto.laadt ? ' ⚡' : ''}</small></li>`);
      const lampen = (th.lampen_aan || []).length;
      const deur = th.deur ? (th.deur.dicht === true ? '🔒 op slot' : th.deur.dicht === false ? '🔓 open' : '') : '';
      if (lampen || deur) rijen.push(`<li><span>${lampen ? `💡 ${lampen} aan` : ''}${lampen && deur ? ' · ' : ''}${deur}</span></li>`);
      document.getElementById('mtList').innerHTML = rijen.join('') || '<li class="leeg">geen gegevens</li>';
    }
    renderL2();
  } catch (e) { /* volgende poging over 60s */ }
}
function toonSleutel(){ document.getElementById('app').style.display='none';
  document.getElementById('sleutel').style.display='block'; }
try {  // 'plan' wordt pas hersteld nadat de planningsfuncties geladen zijn (regel onderaan)
  const t0 = localStorage.getItem('birdy-tab') || 'vandaag';
  if (t0 !== 'plan') kiesTab(t0);
} catch(e){ kiesTab('vandaag'); }
ververs(); setInterval(ververs, 60000);

// ── planning: kinderroutine met slots, drie weergaven en bonus ────────────
const ROUTINES = {
  ochtend: [
    { e:'🚽', n:'Naar de wc', m:3 },
    { e:'👕', n:'Aankleden', m:5 },
    { e:'🥣', n:'Ontbijten', m:15 },
    { e:'🪥', n:'Tandenpoetsen', m:3 },
    { e:'🧺', n:'Haren en wassen', m:4 },
    { e:'🎒', n:'Tas en schoenen', m:4 },
  ],
  avond: [
    { e:'🧸', n:'Speelgoed opruimen', m:5 },
    { e:'🛁', n:'Wassen', m:10 },
    { e:'🩳', n:'Pyjama aan', m:3 },
    { e:'🪥', n:'Tandenpoetsen', m:3 },
    { e:'🚽', n:'Nog even plassen', m:2 },
  ],
};
const BONUS = { e:'🎬', n:'Filmpje of boekje', basis: 5 };
function plVandaag(){ return new Date().toLocaleDateString('sv-SE'); }
function plLeeg(dagdeel){
  let versie = 'kaart';
  try { versie = localStorage.getItem('birdy-plan-versie') || 'kaart'; } catch(e){}
  return { datum: plVandaag(), dagdeel, versie, gekozen: [], start: 0, af: [],
           vertrek: dagdeel === 'ochtend' ? '08:00' : '19:30' };
}
function plLaad(){
  let s = null;
  try { s = JSON.parse(localStorage.getItem('birdy-plan')); } catch(e){}
  if (!s || s.datum !== plVandaag() || !Array.isArray(s.gekozen))
    s = plLeeg(new Date().getHours() < 14 ? 'ochtend' : 'avond');
  return s;
}
let PLAN = plLaad();
const EMOJIS = ['🚽','👕','🥣','🪥','🧺','🎒','🧸','🛁','🩳','📖','🧦','🍎','🐕','🎨','✏️','🚲'];
function takenVan(d){
  try {
    const alles = JSON.parse(localStorage.getItem('birdy-plan-taken')) || {};
    if (Array.isArray(alles[d]) && alles[d].length) return alles[d];
  } catch(e){}
  return ROUTINES[d];
}
function takenBewaar(d, lijst){
  let alles = {};
  try { alles = JSON.parse(localStorage.getItem('birdy-plan-taken')) || {}; } catch(e){}
  alles[d] = lijst;
  try { localStorage.setItem('birdy-plan-taken', JSON.stringify(alles)); } catch(e){}
}
function taakTijd(i, delta){
  const lijst = takenVan(PLAN.dagdeel).map(t => ({ ...t }));
  lijst[i].m = Math.max(1, Math.min(60, lijst[i].m + delta));
  takenBewaar(PLAN.dagdeel, lijst); renderPlan();
}
function taakWeg(i){
  const lijst = takenVan(PLAN.dagdeel).map(t => ({ ...t }));
  lijst.splice(i, 1);
  PLAN.gekozen = PLAN.gekozen.filter(g => g !== i).map(g => g > i ? g - 1 : g);
  takenBewaar(PLAN.dagdeel, lijst); plBewaar(); renderPlan();
}
function takenHerstel(){
  let alles = {};
  try { alles = JSON.parse(localStorage.getItem('birdy-plan-taken')) || {}; } catch(e){}
  delete alles[PLAN.dagdeel];
  try { localStorage.setItem('birdy-plan-taken', JSON.stringify(alles)); } catch(e){}
  PLAN.gekozen = []; plBewaar(); renderPlan();
}
let plNieuwEmoji = EMOJIS[0], plNieuwMin = 5;
function plNieuwOpen(){
  plNieuwEmoji = EMOJIS[0]; plNieuwMin = 5;
  document.getElementById('pnNaam').value = '';
  plNieuwTeken();
  document.getElementById('plNieuw').style.display = 'flex';
  document.getElementById('pnNaam').focus();
}
function plNieuwTeken(){
  document.getElementById('pnEmojis').innerHTML = EMOJIS.map(e =>
    `<button class="pn-em${e === plNieuwEmoji ? ' actief' : ''}"
       onclick="plNieuwEmoji='${e}';plNieuwTeken()">${e}</button>`).join('');
  document.getElementById('pnMin').textContent = plNieuwMin + ' min';
}
function plNieuwMinStap(d){ plNieuwMin = Math.max(1, Math.min(60, plNieuwMin + d)); plNieuwTeken(); }
function plNieuwToevoegen(){
  const naam = document.getElementById('pnNaam').value.trim();
  if (!naam){ toon('Geef het taakje een naam!'); return; }
  const lijst = takenVan(PLAN.dagdeel).map(t => ({ ...t }));
  lijst.push({ e: plNieuwEmoji, n: naam.slice(0, 30), m: plNieuwMin });
  takenBewaar(PLAN.dagdeel, lijst);
  document.getElementById('plNieuw').style.display = 'none';
  renderPlan();
}
let plWaarsch = null, plWaarschVertrek = false;
function plBewaar(){ try { localStorage.setItem('birdy-plan', JSON.stringify(PLAN)); } catch(e){} }
function plDagdeel(d){ PLAN = plLeeg(d); PLAN.dagdeel = d;
  PLAN.vertrek = d === 'ochtend' ? '08:00' : '19:30'; plBewaar(); renderPlan(); }
function plVersie(v){ PLAN.versie = v;
  try { localStorage.setItem('birdy-plan-versie', v); } catch(e){}
  plBewaar(); renderPlan(); }
function plReset(){ PLAN = plLeeg(PLAN.dagdeel); plWaarsch = null; plWaarschVertrek = false;
  plBewaar(); renderPlan(); }
function plStart(){
  if (!PLAN.gekozen.length) { toon('Sleep eerst wat kaartjes naar links!'); return; }
  const t = document.getElementById('plVertrek');
  if (t && t.value) PLAN.vertrek = t.value;
  const nuK = new Date();
  if (hmNaarMin(PLAN.vertrek) <= nuK.getHours() * 60 + nuK.getMinutes() + 1){
    toon('⏰ De eindtijd is al (bijna) geweest — kies een latere tijd!'); return;
  }
  PLAN.start = Date.now(); PLAN.af = []; plWaarsch = null; plWaarschVertrek = false;
  plBewaar(); renderPlan(); deuntje();
}
function plKaartHtml(t, extra){
  return `<span class="em">${t.e}</span><span class="naam">${t.n}</span>` +
         `<span class="tijd">${t.m} min</span>` + (extra || '');
}
// actieve taak = eerste onafgevinkte in de gekozen volgorde; die start zodra de
// vorige is afgevinkt (dus vroeg klaar = volgende kaart begint direct te lopen)
function plActief(){ return PLAN.gekozen[PLAN.af.length]; }
function plActiefStart(){
  return PLAN.af.length ? PLAN.af[PLAN.af.length - 1].t : PLAN.start;
}
function bonusMinuten(){
  const r = takenVan(PLAN.dagdeel);
  const klaarPlanned = PLAN.af.reduce((som, a) => som + r[a.i].m, 0);
  const alles = PLAN.af.length === PLAN.gekozen.length;
  const eind = alles && PLAN.af.length
    ? (PLAN.af[PLAN.af.length - 1].t - PLAN.start) / 60000
    : (Date.now() - PLAN.start) / 60000;
  return Math.max(0, Math.round(BONUS.basis + klaarPlanned - eind));
}
function renderPlan(){
  const el = document.getElementById('paneelPlan');
  el.classList.toggle('breed', !!PLAN.start && PLAN.versie === 'balk');
  const r = takenVan(PLAN.dagdeel);
  const kop = `<div class="pl-kop"><h2>${PLAN.dagdeel === 'ochtend' ? '🌞 Goedemorgen!' : '🌙 Avondprogramma'}</h2>
    <div class="pl-versie">
      <button class="${PLAN.versie==='kaart'?'actief':''}" onclick="plVersie('kaart')">Kaartjes</button>
      <button class="${PLAN.versie==='balk'?'actief':''}" onclick="plVersie('balk')">Tijdlijn</button>
      <button class="${PLAN.versie==='ster'?'actief':''}" onclick="plVersie('ster')">Sterren</button>
    </div>
    <div class="pl-dagdeel">
      <button class="${PLAN.dagdeel==='ochtend'?'actief':''}" onclick="plDagdeel('ochtend')">Ochtend</button>
      <button class="${PLAN.dagdeel==='avond'?'actief':''}" onclick="plDagdeel('avond')">Avond</button>
    </div></div>`;
  if (!PLAN.start){ renderOpzet(el, r, kop); return; }
  if (PLAN.versie === 'balk') renderBalk(el, r, kop);
  else if (PLAN.versie === 'ster') renderSterren(el, r, kop);
  else renderKaarten(el, r, kop);
  plTick();
}
// ── opzet: placeholders links, voorraad rechts ──
function renderOpzet(el, r, kop){
  PLAN.gekozen = PLAN.gekozen.filter(g => g < r.length);
  const slots = PLAN.gekozen.map((i, s) =>
    `<li class="pl-slot vol" data-s="${s}"><div class="pl-kaart mini" onclick="plWeg(${s})"
      title="tik om terug te leggen">${plKaartHtml(r[i])}</div></li>`)
    .concat(PLAN.gekozen.length < r.length
      ? [`<li class="pl-slot" data-s="${PLAN.gekozen.length}">${PLAN.gekozen.length + 1}e taak hier</li>`] : []);
  const bak = r.map((t, i) => PLAN.gekozen.includes(i) ? '' :
    `<li class="pl-kaart mini sleepbaar" data-i="${i}"
       onpointerdown="plPak(event,${i})" onpointermove="plSleepMove(event)"
       onpointerup="plLos(event)" onpointercancel="plLos(event)">
       <span class="em">${t.e}</span><span class="naam">${t.n}</span>
       <span class="tijd"><button class="stap" onpointerdown="event.stopPropagation()"
         onclick="event.stopPropagation();taakTijd(${i},-1)">−</button> ${t.m}m
         <button class="stap" onpointerdown="event.stopPropagation()"
         onclick="event.stopPropagation();taakTijd(${i},1)">+</button></span>
       <button class="weg" title="taakje weghalen" onpointerdown="event.stopPropagation()"
         onclick="event.stopPropagation();taakWeg(${i})">✕</button></li>`).join('');
  el.innerHTML = kop +
    `<p class="pl-hint">Sleep de taken die je gaat doen naar links, in jouw volgorde.
      Wat je niet hoeft, laat je gewoon staan!</p>` +
    `<div class="pl-opzet">
      <div><div class="pl-kolomkop">📋 Mijn plan</div><ul class="pl-slots" id="plSlots">${slots.join('')}</ul></div>
      <div><div class="pl-kolomkop">🧺 Taken</div><ul class="pl-bak">${bak}
        <li class="pl-kaart mini nieuw" onclick="plNieuwOpen()">
          <span class="em">➕</span><span class="naam">Nieuw taakje…</span></li></ul></div>
    </div>` +
    `<div class="pl-tijd-rij"><span>${PLAN.dagdeel === 'ochtend' ? '🕗 We vertrekken om' : '🕢 Bedtijd om'}</span>
      <input type="time" id="plVertrek" value="${PLAN.vertrek}">
      <span>· daarna: ${BONUS.e} ${BONUS.n.toLowerCase()} (${BONUS.basis} min + bonus!)</span></div>` +
    `<button class="pl-start" onclick="plStart()">Start! ▶</button>` +
    `<button class="pl-reset" onclick="plReset()">opnieuw beginnen</button>
     <button class="pl-reset" onclick="takenHerstel()">standaardtaken herstellen</button>`;
}
function plWeg(s){ PLAN.gekozen.splice(s, 1); plBewaar(); renderPlan(); }
let plPakData = null;
function plPak(ev, i){
  const li = ev.currentTarget;
  plPakData = { li, i, x0: ev.clientX, y0: ev.clientY, bezig: false };
  try { li.setPointerCapture(ev.pointerId); } catch(e){}
  window.addEventListener('pointerup', plLos, { once: true });
  window.addEventListener('pointercancel', plLos, { once: true });
}
function plSleepMove(ev){
  if (!plPakData) return;
  const s = plPakData, dx = ev.clientX - s.x0, dy = ev.clientY - s.y0;
  if (!s.bezig && Math.abs(dx) + Math.abs(dy) > 8){ s.bezig = true; s.li.classList.add('tilt'); }
  if (s.bezig){ s.li.style.transform = `translate(${dx}px, ${dy}px)`;
    s.li.style.zIndex = 20; s.px = ev.clientX; s.py = ev.clientY; }
}
function plLos(ev){
  if (!plPakData) return;
  const s = plPakData; plPakData = null;
  s.li.style.transform = ''; s.li.style.zIndex = ''; s.li.classList.remove('tilt');
  if (!s.bezig){
    PLAN.gekozen.push(s.i); plBewaar(); renderPlan(); return;
  }
  s.li.style.visibility = 'hidden';
  const doel = document.elementFromPoint(s.px || 0, s.py || 0);
  s.li.style.visibility = '';
  const slot = doel && doel.closest ? doel.closest('.pl-slot') : null;
  if (slot){
    const plek = Math.min(parseInt(slot.dataset.s, 10), PLAN.gekozen.length);
    PLAN.gekozen.splice(plek, 0, s.i);
    plBewaar(); renderPlan();
  }
}
// ── weergave 1: kaartjes (opeenvolgend: balk van de actieve taak loopt) ──
function renderKaarten(el, r, kop){
  const actief = plActief();
  el.innerHTML = kop +
    `<ul class="pl-lijst">` + PLAN.gekozen.map(i => {
      const t = r[i], af = PLAN.af.some(a => a.i === i);
      return `<li class="pl-kaart${af ? ' af' : ''}${i === actief ? ' nu' : ''}" data-i="${i}"
        onclick="plVier(event, ${i})">
        ${plKaartHtml(t, `<button class="pl-vink">${af ? '✓' : ''}</button><div class="balk"></div>`)}</li>`;
    }).join('') + `</ul>` +
    `<div class="pl-bonuskaart"><span class="em">${BONUS.e}</span>
      <span class="naam">${BONUS.n}</span><b id="plBonus"></b><div class="balk" id="plBonusBalk"></div></div>` +
    `<button class="pl-reset" onclick="plReset()">opnieuw beginnen</button>`;
}
// ── weergave 2: tijdlijn met vertrektijd ──
function hmNaarMin(hm){ const d = hm.split(':'); return (+d[0]) * 60 + (+d[1]); }
function balkModel(){
  const r = takenVan(PLAN.dagdeel);
  const startD = new Date(PLAN.start);
  const startMin = startD.getHours() * 60 + startD.getMinutes() + startD.getSeconds() / 60;
  const totaal = Math.max(1, hmNaarMin(PLAN.vertrek) - startMin);  // start → eindtijd
  const nu = (Date.now() - PLAN.start) / 60000;
  const actief = plActief();
  // kaartjes staan vast op hun geplande breedte; alleen bij een afvink verspringt de
  // lay-out (pad = tijd tot de start van de actieve taak). De klok loopt er los doorheen.
  const pad = (plActiefStart() - PLAN.start) / 60000;
  const segs = [{ pad: true, breed: Math.max(pad, 0.05) }];
  let planned = 0, restLive = 0;
  PLAN.gekozen.forEach(i => {
    if (PLAN.af.some(a => a.i === i)) return;
    const m = r[i].m;
    planned += m;
    if (i === actief){ restLive += Math.max(0, m - (nu - pad)); }
    else { restLive += m; }
    segs.push({ i, actief: i === actief, breed: m, op: i === actief && (nu - pad) > m });
  });
  const bonusBreed = Math.max(0, totaal - pad - planned);
  segs.push({ bonus: true, breed: Math.max(bonusBreed, 0.01) });
  const bonusLive = Math.max(0, (totaal - nu) - restLive);
  return { segs, totaal, nu, bonusMin: Math.round(bonusLive) };
}
function renderBalk(el, r, kop){
  const m = balkModel();
  const klok = ts => new Date(ts).toTimeString().slice(0, 5);
  el.innerHTML = kop +
    `<p class="pl-hint">De rode stip is de klok — blijf hem voor! Alles wat je overhoudt is
      ${BONUS.e} bonustijd. Klaar met een taak? Tik erop!</p>` +
    `<div class="pl-plank">🏆` +
      (PLAN.af.length
        ? PLAN.af.map((a, k) => `<span class="badge${k === PLAN.af.length - 1 ? ' nieuw' : ''}"
            style="border-color:${KLEUREN[a.i % KLEUREN.length]}">${r[a.i].e}</span>`).join('')
        : `<span class="leeg-plank">hier komen jouw medailles!</span>`) +
    `</div><div class="pl-balkwrap"><div class="pl-verleden" id="plVerleden"></div><div class="pl-balk2" id="plBalk2">` +
    m.segs.map(s => s.pad
      ? `<div class="pl-seg pad" id="plSegPad" style="flex:${s.breed} 1 0">🐾</div>`
      : s.bonus
      ? `<div class="pl-seg bonus" id="plSegBonus" style="flex:${s.breed} 1 0">
           <span class="em2">${BONUS.e}</span><span id="plBonus"></span></div>`
      : `<div class="pl-seg${s.actief ? ' nu2' : ''}${s.op ? ' op' : ''}" data-i="${s.i}"
           style="flex:${s.breed} 1 0;background:${KLEUREN[s.i % KLEUREN.length]}26"
           onclick="plVier(event, ${s.i})">
           <span class="em2">${r[s.i].e}</span>
           <span>${r[s.i].n.split(' ')[0]}</span><span>${r[s.i].m}m</span></div>`).join('') +
    `</div>
    <div class="pl-rail"><div class="vul" id="plRailVul"></div>
      <div class="stip" id="plRailStip">🦊</div><div class="nulabel" id="plRailNu"></div></div>
    <div class="pl-tijden"><span>▶ ${klok(PLAN.start)}</span>
      <span>🏁 ${PLAN.dagdeel === 'ochtend' ? 'vertrek' : 'bedtijd'} ${PLAN.vertrek}</span></div></div>` +
    `<div class="pl-timer">
      <svg viewBox="0 0 120 120">
        <circle class="ringbg" cx="60" cy="60" r="52"/>
        <circle class="ring buiten" id="ringTot" cx="60" cy="60" r="52"
          transform="rotate(-90 60 60)"/>
        <circle class="ringbg" cx="60" cy="60" r="40"/>
        <circle class="ring binnen" id="ringAct" cx="60" cy="60" r="40"
          transform="rotate(-90 60 60)"/>
      </svg>
      <div class="pl-timer-tekst"><b id="timerAct">–:–</b><span id="timerTot"></span></div>
    </div>` +
    `<button class="pl-reset" onclick="plReset()">opnieuw beginnen</button>`;
}
// ── weergave 3: sterren (blokhoogte = tijd; vroeg klaar = blok krimpt, bonus groeit) ──
function renderSterren(el, r, kop){
  const actief = plActief();
  const px = 9;  // hoogte per minuut
  el.innerHTML = kop +
    `<p class="pl-hint">Snel klaar? Dan wordt jouw ${BONUS.e}-blok groter en verdien je sterren! ⭐</p>` +
    `<ul class="pl-lijst">` + PLAN.gekozen.map(i => {
      const t = r[i];
      const afRec = PLAN.af.find(a => a.i === i);
      const volgIdx = PLAN.gekozen.indexOf(i);
      let minuten = t.m;
      if (afRec){
        const vorigeT = volgIdx === 0 ? PLAN.start : PLAN.af[volgIdx - 1].t;
        minuten = Math.max(1, (afRec.t - vorigeT) / 60000);
      }
      const hoogte = Math.max(afRec ? 2.4 : 3.6, minuten * px / 16) + 'rem';
      return `<li class="pl-kaart${afRec ? ' af' : ''}${i === actief ? ' nu' : ''}" data-i="${i}"
        style="min-height:${hoogte};height:${hoogte};transition:height .6s"
        onclick="plVier(event, ${i})">
        ${plKaartHtml(t, `<button class="pl-vink">${afRec ? '✓' : ''}</button><div class="balk"></div>`)}</li>`;
    }).join('') + `</ul>` +
    `<div class="pl-bonuskaart" id="plBonusGroei"><span class="em">${BONUS.e}</span>
      <span><span class="naam">${BONUS.n}</span><br><span class="pl-sterren" id="plSterren"></span></span>
      <b id="plBonus"></b></div>` +
    `<button class="pl-reset" onclick="plReset()">opnieuw beginnen</button>`;
}
function plTick(){
  if (!PLAN.start || document.getElementById('paneelPlan').style.display === 'none') return;
  const r = takenVan(PLAN.dagdeel);
  const bonus = bonusMinuten();
  const bonusEl = document.getElementById('plBonus');
  const actief = plActief();
  // waarschuwing: nog 1 minuut voor de actieve taak
  if (actief !== undefined){
    const rest = r[actief].m * 60 - (Date.now() - plActiefStart()) / 1000;
    if (rest <= 60 && rest > 0 && plWaarsch !== actief){ plWaarsch = actief; attentie(); }
  }
  if (PLAN.versie === 'balk'){
    const m = balkModel();
    m.segs.forEach(s => {
      const el = s.pad ? document.getElementById('plSegPad')
        : s.bonus ? document.getElementById('plSegBonus')
        : document.querySelector(`.pl-seg[data-i="${s.i}"]`);
      if (!el) return;
      el.style.flexGrow = s.breed;
      if (!s.bonus && !s.pad){ el.classList.toggle('nu2', !!s.actief); el.classList.toggle('op', !!s.op); }
    });
    const pct = Math.min(99.3, Math.max(0.7, m.nu / m.totaal * 100)) + '%';
    const verleden = document.getElementById('plVerleden');
    if (verleden) verleden.style.width = Math.min(100, m.nu / m.totaal * 100) + '%';
    const vul = document.getElementById('plRailVul');
    const stip = document.getElementById('plRailStip');
    const nulabel = document.getElementById('plRailNu');
    if (vul) vul.style.width = pct;
    if (stip) stip.style.left = pct;
    if (nulabel){ nulabel.style.left = pct;
      nulabel.textContent = new Date().toTimeString().slice(0, 5); }
    if (bonusEl) bonusEl.textContent = m.bonusMin + ' min';
    // grote timer: buitenring = totaal tot eindtijd, binnenring = huidige taak
    const ringAct = document.getElementById('ringAct');
    const ringTot = document.getElementById('ringTot');
    if (ringAct && ringTot){
      const CT = 2 * Math.PI * 52, CB = 2 * Math.PI * 40;
      const totRest = Math.max(0, m.totaal - m.nu);
      ringTot.style.strokeDasharray = CT;
      ringTot.style.strokeDashoffset = CT * (1 - totRest / m.totaal);
      let tekst = '🎉', fracA = 1, op = false;
      if (actief !== undefined){
        const actRest = r[actief].m * 60 - (Date.now() - plActiefStart()) / 1000;
        op = actRest <= 0;
        fracA = Math.max(0, actRest) / (r[actief].m * 60);
        const s = Math.max(0, Math.ceil(actRest));
        tekst = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
      }
      ringAct.style.strokeDasharray = CB;
      ringAct.style.strokeDashoffset = CB * (1 - fracA);
      ringAct.classList.toggle('op', op);
      document.getElementById('timerAct').textContent = tekst;
      document.getElementById('timerTot').textContent =
        actief !== undefined ? 'nog ' + Math.round(totRest) + ' min totaal' : 'alles af!';
    }
    // waarschuwing: nog 1 minuut tot vertrek/bedtijd
    const restTot = m.totaal - m.nu;
    if (restTot <= 1 && restTot > 0 && !plWaarschVertrek){ plWaarschVertrek = true; attentie(); }
  } else {
    // kaartjes & sterren: alleen de actieve kaart loopt, direct na de vorige afvink
    const startAct = plActiefStart();
    PLAN.gekozen.forEach(i => {
      const kaart = document.querySelector(`#paneelPlan .pl-kaart[data-i="${i}"]`);
      if (!kaart) return;
      const balk = kaart.querySelector('.balk');
      if (!balk) return;
      if (PLAN.af.some(a => a.i === i)) return;  // af = 100% via CSS
      if (i === actief){
        const frac = Math.min(1, (Date.now() - startAct) / 60000 / r[i].m);
        balk.style.width = (frac * 100) + '%';
        balk.style.background = frac >= 1 ? 'var(--rood)' : '';
      } else { balk.style.width = '0%'; }
    });
    if (bonusEl) bonusEl.textContent = bonus + ' min';
    const bb = document.getElementById('plBonusBalk');
    if (bb) bb.style.width = Math.min(100, bonus / (BONUS.basis * 3) * 100) + '%';
    const groei = document.getElementById('plBonusGroei');
    if (groei){
      groei.style.minHeight = (3.4 + bonus * 0.35) + 'rem';
      const sterren = document.getElementById('plSterren');
      if (sterren) sterren.textContent = '⭐'.repeat(Math.min(bonus, 15));
    }
  }
}
setInterval(plTick, 1000);
function plVier(ev, i){
  if (PLAN.af.some(a => a.i === i)) return;
  if (i !== plActief()) return;  // altijd in de gekozen volgorde
  const r = takenVan(PLAN.dagdeel);
  const gebruikt = (Date.now() - plActiefStart()) / 60000;
  const verdiend = Math.round(r[i].m - gebruikt);
  PLAN.af.push({ i, t: Date.now() }); plBewaar();
  const rect = ev.currentTarget.getBoundingClientRect();
  confetti(rect.left + rect.width / 2, rect.top + rect.height / 2, 60);
  if (verdiend > 0) popEffect(rect.right - 40, rect.top, `+${verdiend} min ⭐`);
  renderPlan();
  if (PLAN.af.length === PLAN.gekozen.length){
    const einde = document.getElementById('plKlaar');
    const eindBonus = PLAN.versie === 'balk' ? balkModel().bonusMin : bonusMinuten();
    einde.querySelector('p').textContent =
      `Alles is af — je hebt ${eindBonus} minuten ${BONUS.n.toLowerCase()} verdiend!`;
    einde.style.display = 'flex';
    confetti(innerWidth / 2, innerHeight / 3, 220);
    fanfare();
    setTimeout(() => { einde.style.display = 'none'; }, 7000);
  } else { deuntje(); }
}
function popEffect(x, y, tekst){
  const d = document.createElement('div');
  d.textContent = tekst;
  d.style.cssText = `position:fixed;left:${x}px;top:${y}px;z-index:95;font-size:1.3rem;` +
    `font-weight:800;color:var(--amber);pointer-events:none;white-space:nowrap`;
  document.body.appendChild(d);
  d.animate([{ transform:'translateY(0)', opacity:1 }, { transform:'translateY(-70px)', opacity:0 }],
            { duration: 1400, easing:'ease-out' }).onfinish = () => d.remove();
}
function attentie(){ [880, 660, 880].forEach((f, i) => noot(f, i * 0.22, 0.3)); }
// confetti + geluid (zelfvoorzienend, geen externe bestanden)
function confetti(x, y, n){
  const c = document.getElementById('plCanvas');
  c.width = innerWidth; c.height = innerHeight;
  const ctx = c.getContext('2d');
  const p = Array.from({ length: n }, () => ({
    x, y, vx: (Math.random() - .5) * 14, vy: -Math.random() * 12 - 3,
    r: Math.random() * 5 + 3, k: KLEUREN[Math.floor(Math.random() * KLEUREN.length)],
    a: Math.random() * Math.PI }));
  const t0 = performance.now();
  (function stap(t){
    const dt = (t - t0) / 1000;
    ctx.clearRect(0, 0, c.width, c.height);
    if (dt > 1.8) return;
    p.forEach(d => {
      d.x += d.vx; d.y += d.vy; d.vy += .45; d.a += .2;
      ctx.save(); ctx.translate(d.x, d.y); ctx.rotate(d.a);
      ctx.fillStyle = d.k; ctx.globalAlpha = Math.max(0, 1 - dt / 1.8);
      ctx.fillRect(-d.r, -d.r / 2, d.r * 2, d.r); ctx.restore();
    });
    requestAnimationFrame(stap);
  })(t0);
}
let audioCtx = null;
function noot(freq, wanneer, duur){
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.type = 'triangle'; o.frequency.value = freq;
    const t = audioCtx.currentTime + wanneer;
    g.gain.setValueAtTime(0.001, t);
    g.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
    g.gain.exponentialRampToValueAtTime(0.001, t + duur);
    o.connect(g); g.connect(audioCtx.destination);
    o.start(t); o.stop(t + duur + 0.05);
  } catch(e){}
}
function deuntje(){ [523, 659, 784].forEach((f, i) => noot(f, i * 0.12, 0.28)); }
function fanfare(){ [523, 659, 784, 1047, 784, 1047, 1319].forEach((f, i) => noot(f, i * 0.16, 0.34)); }
try { if (localStorage.getItem('birdy-tab') === 'plan') kiesTab('plan'); } catch(e){}

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
  if (ev.key !== 'Enter' && ev.keyCode !== 13) return;
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
function spraak(knop, cb){
  if (!SR) {
    if (!cb) { chatOpen(true); document.getElementById('chatveld').focus(); }
    toon('🎤 Spraak werkt in Chrome of Safari — typen kan altijd.');
    return;
  }
  if (luistert) { rec.stop(); return; }
  rec = new SR(); rec.lang = 'nl-NL'; rec.interimResults = false;
  rec.onstart = () => { luistert = true; knop.classList.add('luistert'); };
  rec.onend = () => { luistert = false; knop.classList.remove('luistert'); };
  rec.onerror = () => toon('🎤 Ik kon je niet verstaan — probeer nog eens.');
  rec.onresult = ev => (cb || stuur)(ev.results[0][0].transcript);
  rec.start();
}
document.getElementById('invoer').addEventListener('keydown',
  e => { if (e.key === 'Enter' || e.keyCode === 13) stuur(e.target.value); });
document.getElementById('chatveld').addEventListener('keydown',
  e => { if (e.key === 'Enter' || e.keyCode === 13) stuur(e.target.value); });
document.getElementById('tvVeld').addEventListener('keydown',
  e => { if (e.key === 'Enter' || e.keyCode === 13) tvStuur(e.target.value); });
</script></body></html>"""
