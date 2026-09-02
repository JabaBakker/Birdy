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

STATIC_DIR = Path(__file__).parent / "static"  # dashboard.html/.css/.js + logo's


def _static_versie() -> str:
    """Korte hash van css+js, als cache-buster in de pagina (verandert bij elke uitrol)."""
    import hashlib

    h = hashlib.sha1()
    for naam in ("dashboard.css", "dashboard.js"):
        try:
            h.update((STATIC_DIR / naam).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]

from .brain import Brain
from .config import Config

log = logging.getLogger("fien.dashboard")

CACHE_TTL = 120  # seconden


def _agenda_rijk(days: int = 7) -> tuple[list[dict], bool]:
    """Afspraken mét eindtijd voor de komende `days` dagen (Vandaag-tab en weekcache)."""
    now = datetime.now()
    return _agenda_bereik(now, now + timedelta(days=days))


def _agenda_bereik(van: datetime, tot: datetime, zoek: str = "") -> tuple[list[dict], bool]:
    """Afspraken uit beide bronnen in [van, tot): ([{start, eind, titel, …}], compleet).
    `zoek` filtert op titel/omschrijving/locatie (Google zoekt zelf, FamilyWall lokaal).
    compleet=False als een bron faalde — dan is het resultaat mogelijk (deels) leeg en
    houdt de cache liever de vorige complete versie vast."""
    events: list[dict] = []
    google_ok = not os.environ.get("GOOGLE_CALENDAR_ID")
    fw_ok = True
    zoek = zoek.strip().lower()
    try:
        from . import gcal
        if os.environ.get("GOOGLE_CALENDAR_ID"):
            svc = gcal._service()
            params = dict(
                calendarId=os.environ["GOOGLE_CALENDAR_ID"],
                timeMin=van.astimezone().isoformat(),
                timeMax=tot.astimezone().isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=120 if zoek else 60,
                timeZone="Europe/Amsterdam",  # anders komt alles in UTC (2 uur te vroeg)
            )
            if zoek:
                params["q"] = zoek
            resp = svc.events().list(**params).execute()
            for ev in resp.get("items", []):
                s, e = ev.get("start", {}), ev.get("end", {})
                wie = ev.get("creator", {}) or {}
                org = ev.get("organizer", {}) or {}
                naam = (org.get("displayName") or wie.get("displayName")
                        or wie.get("email") or org.get("email") or "")
                # door Birdy gesynchroniseerde feed (bijv. "Volleybal Yvette") → label als bron,
                # zodat het dashboard op de persoonsnaam in het label kan kleuren
                sync_label = ((ev.get("extendedProperties") or {}).get("private") or {}).get("birdy_sync", "")
                bron = f"{sync_label} · automatisch in de gezinsagenda" if sync_label else "Gezinsagenda (Google)"
                def lokaal(v: str) -> str:  # dateTime met offset → NL-wandkloktijd
                    if "T" in v:
                        return gcal._lokaal(datetime.fromisoformat(v)).strftime("%Y-%m-%dT%H:%M")
                    return v[:10]
                events.append({
                    "id": ev.get("id", ""),
                    "start": lokaal(s.get("dateTime") or s.get("date", "")),
                    "eind": lokaal(e.get("dateTime") or e.get("date", "")),
                    "titel": ev.get("summary", "(zonder titel)"),
                    "omschrijving": (ev.get("description") or "")[:600],
                    "locatie": ev.get("location", ""),
                    "wie": naam.replace("bakkerbirdy@gmail.com", "Birdy"),
                    "bron": bron,
                })
            google_ok = True
    except BaseException:  # SystemExit van de CLI-helpers telt ook
        log.warning("Google-agenda ophalen mislukt", exc_info=True)
    from . import gcal

    feeds = gcal.ics_feeds()
    fw_ok = not feeds
    if feeds:
        import icalendar
        import recurring_ical_events
        import requests

        def iso(v) -> str:
            if isinstance(v, datetime):
                return gcal._lokaal(v).strftime("%Y-%m-%dT%H:%M")
            return v.strftime("%Y-%m-%d") if v else ""

        alle_ok = True
        for label, url in feeds:
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                cal = icalendar.Calendar.from_ical(resp.content)
                for ev in recurring_ical_events.of(cal).between(van, tot):
                    start = ev.get("DTSTART").dt
                    eind = ev.get("DTEND")
                    if zoek and zoek not in " ".join(str(ev.get(k, "")) for k in
                                                     ("SUMMARY", "DESCRIPTION", "LOCATION")).lower():
                        continue
                    events.append({
                        "id": "",  # alleen-lezen: iCal-afspraken kunnen niet verzet worden
                        "start": iso(start),
                        "eind": iso(eind.dt if eind else None) or iso(start),
                        "titel": str(ev.get("SUMMARY", "(zonder titel)")),
                        "omschrijving": str(ev.get("DESCRIPTION", ""))[:600],
                        "locatie": str(ev.get("LOCATION", "")),
                        "wie": "",
                        "bron": label,
                    })
            except BaseException:
                alle_ok = False
                log.warning("iCal-feed %s ophalen mislukt", label, exc_info=True)
        fw_ok = alle_ok
    # zelfde moment + titel uit beide bronnen → één keer
    seen, uniek = set(), []
    for ev in sorted(events, key=lambda e: e["start"]):
        key = (ev["start"], ev["titel"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        uniek.append(ev)
    return uniek[:200 if zoek else 60], google_ok and fw_ok


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
    # ouder dan 3 dagen → niet meer tonen (Birdy schrijft ze bij de weekplanning of op verzoek)
    dagen = _datum_dagen(tijd) if tijd else None
    if dagen is not None and dagen < -3:
        return {"tijd": tijd, "items": [], "oud": True}
    return {"tijd": tijd, "items": items[:3], "oud": False}


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
    # acties van vandaag niet apart melden: die staan al onder "Nu" in de actiekolom

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
            naam = j["naam"].split("(")[0].strip()
            out.append({"tekst": f"🎂 {j['naam']} {wanneer}, nog geen cadeau-idee",
                        "l2": "verjaardagen", "ernst": 1 if j["dagen"] > 1 else 0,
                        "knop": {"label": "Cadeau-actie toevoegen", "tekst": f"Cadeau voor {naam}",
                                 "datum": (vandaag + timedelta(days=max(0, j["dagen"] - 1))).isoformat()}})

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

    for a_tekst, o_naam in _dubbelingen(acties, onderwerpen)[:2]:
        out.append({"tekst": f"👀 Mogelijk dubbel: “{a_tekst[:30]}” (actie) en “{o_naam[:30]}” (onderwerp)",
                    "l2": "onderwerpen", "ernst": 1})

    out.sort(key=lambda s: s["ernst"])
    return out[:8]


_STOPWOORDEN = {"voor", "naar", "over", "kopen", "regelen", "checken", "maken", "laten", "weten",
                "zodra", "bellen", "sturen", "versturen", "afmaken", "invullen", "geregeld", "vandaag",
                "morgen", "week", "deze", "die", "dat", "het", "een", "van", "met", "nog", "wordt"}


def _woorden(tekst: str) -> set[str]:
    import re

    return {w for w in re.findall(r"[a-zà-ÿ0-9]+", tekst.lower()) if len(w) >= 4 and w not in _STOPWOORDEN}


def _dubbelingen(acties: list[dict], onderwerpen: list[dict]) -> list[tuple[str, str]]:
    """Actie en onderwerp die (bijna) over hetzelfde gaan: minstens twee gedeelde kernwoorden
    én meer dan de helft van de woorden van de kortste van de twee. Een actie die het
    onderwerp als voorvoegsel draagt ('Kinderfeest Evi: gastenlijst invullen') is bewust
    zo gemaakt en telt niet mee."""
    out = []
    for a in acties:
        wa = _woorden(a.get("tekst", ""))
        if len(wa) < 2:
            continue
        for o in onderwerpen:
            if ":" in a.get("tekst", "") and a["tekst"].lower().startswith(o["naam"].lower()[:12]):
                continue
            wo = _woorden(o["naam"])
            if len(wo) < 2:
                continue
            gedeeld = wa & wo
            if len(gedeeld) >= 2 and len(gedeeld) / min(len(wa), len(wo)) > 0.5:
                out.append((a["tekst"], o["naam"]))
                break
    return out


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


def _todoist_toevoegen(lijst: str, tekst: str, datum: str = "") -> dict | None:
    from . import todoist

    try:
        project = todoist._project(lijst)
        body = {"content": tekst, "project_id": project["id"]}
        if datum:
            body["due_date"] = datum
        t = todoist._request("POST", "/tasks", json=body)
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


def _agenda_bewerk(event_id: str, velden: dict) -> bool:
    """Google-afspraak bewerken: titel, start/eind (met tijd 'YYYY-MM-DDTHH:MM' of hele dag
    'YYYY-MM-DD'), locatie, omschrijving. Alleen meegegeven velden worden aangepast."""
    from . import gcal

    body: dict = {}
    if "titel" in velden:
        body["summary"] = velden["titel"]
    if "start" in velden:
        s, e = velden["start"], velden["eind"]
        if "T" in s:
            body["start"] = {"dateTime": f"{s}:00", "timeZone": "Europe/Amsterdam"}
            body["end"] = {"dateTime": f"{e}:00", "timeZone": "Europe/Amsterdam"}
        else:  # hele dag: Google wil een exclusieve einddatum
            eind = (date.fromisoformat(e) + timedelta(days=1)).isoformat() if e == s else e
            body["start"], body["end"] = {"date": s}, {"date": eind}
    if "locatie" in velden:
        body["location"] = velden["locatie"]
    if "omschrijving" in velden:
        body["description"] = velden["omschrijving"]
    try:
        svc = gcal._service()
        svc.events().patch(calendarId=os.environ["GOOGLE_CALENDAR_ID"], eventId=event_id, body=body).execute()
        return True
    except BaseException:
        log.warning("afspraak bewerken mislukt", exc_info=True)
        return False


def _agenda_nieuw(velden: dict) -> dict | None:
    """Nieuwe Google-afspraak; geeft {id, start, eind} terug of None."""
    from . import gcal

    s, e = velden["start"], velden["eind"]
    if "T" in s:
        start = {"dateTime": f"{s}:00", "timeZone": "Europe/Amsterdam"}
        eind = {"dateTime": f"{e}:00", "timeZone": "Europe/Amsterdam"}
    else:
        start = {"date": s}
        eind = {"date": (date.fromisoformat(e) + timedelta(days=1)).isoformat() if e == s else e}
    body = {"summary": velden["titel"], "start": start, "end": eind,
            "location": velden.get("locatie", ""), "description": velden.get("omschrijving", "")}
    try:
        svc = gcal._service()
        ev = svc.events().insert(calendarId=os.environ["GOOGLE_CALENDAR_ID"], body=body).execute()
        return {"id": ev.get("id", ""), "start": s, "eind": e}
    except BaseException:
        log.warning("afspraak aanmaken mislukt", exc_info=True)
        return None


def _agenda_verwijder(event_id: str) -> bool:
    from . import gcal

    try:
        svc = gcal._service()
        svc.events().delete(calendarId=os.environ["GOOGLE_CALENDAR_ID"], eventId=event_id).execute()
        return True
    except BaseException:
        log.warning("afspraak verwijderen mislukt", exc_info=True)
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
        self._agenda_cache: dict[tuple, tuple[float, list]] = {}  # /api/agenda: (van,tot,zoek) → (ts, events)
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
        app.router.add_get("/dashboard.css", self.static)
        app.router.add_get("/dashboard.js", self.static)
        app.router.add_get("/api/overview", self.overview)
        app.router.add_get("/api/agenda", self.agenda)
        app.router.add_post("/api/message", self.message)
        app.router.add_post("/api/done", self.done)
        app.router.add_post("/api/add", self.add)
        app.router.add_post("/api/due", self.due)
        app.router.add_post("/api/reopen", self.reopen)
        app.router.add_post("/api/verzet", self.verzet)
        app.router.add_post("/api/event", self.event)
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
        # ?v=<versie> in de css/js-links zodat tablets na een uitrol geen oude bestanden cachen
        html = (STATIC_DIR / "dashboard.html").read_text().replace("{versie}", _static_versie())
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    async def static(self, request: web.Request) -> web.Response:
        naam = request.path.lstrip("/")
        if naam not in ("dashboard.css", "dashboard.js"):
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC_DIR / naam, headers={"Cache-Control": "public, max-age=31536000"})

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

    async def agenda(self, request: web.Request) -> web.Response:
        """Weekweergave buiten de komende 7 dagen (?van=JJJJ-MM-DD&tot=JJJJ-MM-DD) of zoeken
        (?zoek=term, 3 maanden terug t/m 18 maanden vooruit). Eigen cache van 2 minuten."""
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        zoek = request.query.get("zoek", "").strip()[:80]
        try:
            if zoek:
                vandaag = date.today()
                van, tot = vandaag - timedelta(days=90), vandaag + timedelta(days=548)
            else:
                van = date.fromisoformat(request.query.get("van", ""))
                tot = date.fromisoformat(request.query.get("tot", ""))
            if not (timedelta(0) < tot - van <= timedelta(days=640)):  # zoekvenster = 90 + 548
                raise ValueError
        except ValueError:
            return web.json_response({"error": "van/tot (JJJJ-MM-DD) of zoek ontbreekt"}, status=400)
        sleutel = (van.isoformat(), tot.isoformat(), zoek.lower())
        nu = time.time()
        hit = self._agenda_cache.get(sleutel)
        if hit and nu - hit[0] <= CACHE_TTL:
            events = hit[1]
        else:
            events, compleet = await asyncio.to_thread(
                _agenda_bereik, datetime.combine(van, datetime.min.time()),
                datetime.combine(tot, datetime.min.time()), zoek)
            if compleet:
                self._agenda_cache[sleutel] = (nu, events)
                if len(self._agenda_cache) > 40:  # niet eindeloos groeien
                    oudste = min(self._agenda_cache, key=lambda k: self._agenda_cache[k][0])
                    del self._agenda_cache[oudste]
        return web.json_response({"events": events, "van": van.isoformat(), "tot": tot.isoformat()})

    async def event(self, request: web.Request) -> web.Response:
        """Afspraak bewerken vanaf de detailkaart: {id, titel?, start?, eind?, locatie?, omschrijving?}.
        start/eind samen: beide 'JJJJ-MM-DDTUU:MM' (met tijd) of beide 'JJJJ-MM-DD' (hele dag)."""
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        event_id = str(body.get("id", "")).strip()
        actie = str(body.get("actie", "")).strip()  # '' = bewerken, 'nieuw', 'verwijder'
        if actie != "nieuw" and (not event_id or len(event_id) > 200):
            return web.json_response({"error": "geen afspraak-id"}, status=400)
        if actie == "verwijder":
            ok = await asyncio.to_thread(_agenda_verwijder, event_id)
            if ok:
                self._gen += 1
                self._cache = None
                self._agenda_cache = {}
                if self._traag:
                    self._traag["week"] = [e for e in self._traag["week"] if e.get("id") != event_id]
            return web.json_response({"ok": ok}, status=200 if ok else 502)

        def is_dag(t: str) -> bool:
            return len(t) == 10 and t[4] == t[7] == "-" and t.replace("-", "").isdigit()

        def is_tijd(t: str) -> bool:
            return len(t) == 16 and t[10] == "T" and is_dag(t[:10]) and t[11:].replace(":", "").isdigit()

        velden: dict = {}
        if "titel" in body:
            titel = str(body["titel"]).strip()[:200]
            if not titel:
                return web.json_response({"error": "titel mag niet leeg zijn"}, status=400)
            velden["titel"] = titel
        if "start" in body or "eind" in body:
            start, eind = str(body.get("start", "")).strip(), str(body.get("eind", "")).strip()
            if not ((is_tijd(start) and is_tijd(eind)) or (is_dag(start) and is_dag(eind))) or eind < start:
                return web.json_response({"error": "start/eind ongeldig"}, status=400)
            velden["start"], velden["eind"] = start, eind
        for k in ("locatie", "omschrijving"):
            if k in body:
                velden[k] = str(body[k]).strip()[:2000 if k == "omschrijving" else 200]
        if actie == "nieuw":
            if "titel" not in velden or "start" not in velden:
                return web.json_response({"error": "titel en datum zijn nodig"}, status=400)
            nieuw = await asyncio.to_thread(_agenda_nieuw, velden)
            if nieuw:
                self._gen += 1
                self._cache = None
                self._agenda_cache = {}
                if self._traag:
                    self._traag["week"].append({
                        "id": nieuw["id"], "start": velden["start"], "eind": velden["eind"],
                        "titel": velden["titel"], "omschrijving": velden.get("omschrijving", "")[:600],
                        "locatie": velden.get("locatie", ""), "wie": "Birdy", "bron": "Gezinsagenda (Google)"})
                    self._traag["week"].sort(key=lambda e: e["start"])
            return web.json_response({"ok": bool(nieuw), "id": (nieuw or {}).get("id", "")},
                                     status=200 if nieuw else 502)
        if not velden:
            return web.json_response({"error": "niets te wijzigen"}, status=400)
        ok = await asyncio.to_thread(_agenda_bewerk, event_id, velden)
        if ok:
            self._gen += 1
            self._cache = None
            self._agenda_cache = {}
            if self._traag:
                for e in self._traag["week"]:
                    if e.get("id") == event_id:
                        if "titel" in velden:
                            e["titel"] = velden["titel"]
                        if "start" in velden:
                            e["start"], e["eind"] = velden["start"], velden["eind"]
                        if "locatie" in velden:
                            e["locatie"] = velden["locatie"]
                        if "omschrijving" in velden:
                            e["omschrijving"] = velden["omschrijving"][:600]
                self._traag["week"].sort(key=lambda e: e["start"])
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
            self._agenda_cache = {}  # andere weken/zoekresultaten opnieuw ophalen
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
            datum = str(body.get("datum", "") or "").strip()[:10]
            if datum and not (len(datum) == 10 and datum[4] == datum[7] == "-" and datum.replace("-", "").isdigit()):
                datum = ""
        except Exception:
            lijst, tekst = "", ""
        if lijst not in ("boodschappen", "acties") or not tekst:
            return web.json_response({"error": "lijst of tekst ontbreekt"}, status=400)
        taak = await asyncio.to_thread(_todoist_toevoegen, lijst, tekst, datum)
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
