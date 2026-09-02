"""Gezinsagenda voor het dashboard: lezen (Google + iCal-feeds), verzetten, bewerken,
aanmaken en verwijderen. De CLI voor het brein zit in gcal.py; dit is de programmatische kant."""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

log = logging.getLogger("fien.agenda")

def rijk(days: int = 7) -> tuple[list[dict], bool]:
    """Afspraken mét eindtijd voor de komende `days` dagen (Vandaag-tab en weekcache)."""
    now = datetime.now()
    return bereik(now, now + timedelta(days=days))


def bereik(van: datetime, tot: datetime, zoek: str = "") -> tuple[list[dict], bool]:
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
                    "wie": "Birdy" if naam and naam == os.environ.get("AGENT_GOOGLE_ACCOUNT", "") else naam,
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


def compact(rijk: list[dict]) -> list[dict]:
    return [{
        "wanneer": ev["start"].replace("T", " "),
        "titel": ev["titel"],
    } for ev in rijk][:22]


def verzet(event_id: str, start: str, eind: str) -> bool:
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


def bewerk(event_id: str, velden: dict) -> bool:
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


def nieuw(velden: dict) -> dict | None:
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


def verwijder(event_id: str) -> bool:
    from . import gcal

    try:
        svc = gcal._service()
        svc.events().delete(calendarId=os.environ["GOOGLE_CALENDAR_ID"], eventId=event_id).execute()
        return True
    except BaseException:
        log.warning("afspraak verwijderen mislukt", exc_info=True)
        return False
