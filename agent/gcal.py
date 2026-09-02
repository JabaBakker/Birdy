"""Google Calendar-hulpje voor Fien (service-account).

De agent roept dit aan via Bash:
    python /app/agent/gcal.py list --days 7
    python /app/agent/gcal.py add "Titel" --start "2026-08-21 14:00" --duur 60
    python /app/agent/gcal.py add "Titel" --dag 2026-08-21          (hele dag)
    python /app/agent/gcal.py sync-ics   (AGENDA_SYNC_ICS-feeds in de Google-agenda zetten)

Vereist in .env: GOOGLE_CALENDAR_ID plus een Google-koppeling — bij voorkeur OAuth
(GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN, zie scripts/google_consent.py); het oude
service-account-bestand werkt als terugval tijdens de overgang.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

TZ = "Europe/Amsterdam"


def _lokaal(dt: datetime) -> datetime:
    """Elke tijd naar Nederlandse wandkloktijd. Tijdzone-bewuste tijden (UTC uit een API,
    Europe/Paris uit een iCal) worden omgerekend; naïeve tijden zijn al lokaal."""
    if dt.tzinfo is None:
        return dt
    from zoneinfo import ZoneInfo

    return dt.astimezone(ZoneInfo(TZ)).replace(tzinfo=None)


def _tijdvak(start: str | datetime, eind: str | datetime | None) -> str:
    """'2026-09-06 13:00–16:00' (met tijd) of '2026-09-06' (hele dag), altijd lokaal."""
    def parse(v):
        if isinstance(v, datetime):
            return _lokaal(v)
        if isinstance(v, str) and "T" in v:
            return _lokaal(datetime.fromisoformat(v))
        return v  # date of 'YYYY-MM-DD'
    s, e = parse(start), parse(eind) if eind else None
    if isinstance(s, datetime):
        tekst = s.strftime("%Y-%m-%d %H:%M")
        if isinstance(e, datetime) and e > s:
            tekst += "–" + (e.strftime("%H:%M") if e.date() == s.date() else e.strftime("%d-%m %H:%M"))
        return tekst
    return str(s)[:10]


try:
    from agent.google_auth import CALENDAR_SCOPE, configured, google_credentials
except ImportError:  # aangeroepen als los script: python /app/agent/gcal.py
    from google_auth import CALENDAR_SCOPE, configured, google_credentials


def _service():
    try:
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("google-api-python-client is niet geïnstalleerd")
    creds = google_credentials([CALENDAR_SCOPE])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _cal_id() -> str:
    cal = os.environ.get("GOOGLE_CALENDAR_ID", "")
    if not cal:
        sys.exit("GOOGLE_CALENDAR_ID ontbreekt in .env")
    return cal


def _google_events(days: int) -> list[tuple[str, str]]:
    """(sorteersleutel, regel) uit de Google-gezinsagenda. Leeg als niet gekoppeld."""
    if not (os.environ.get("GOOGLE_CALENDAR_ID") and configured()):
        return []
    svc = _service()
    now = datetime.now()
    result = svc.events().list(
        calendarId=_cal_id(),
        timeMin=now.astimezone().isoformat(),
        timeMax=(now + timedelta(days=days)).astimezone().isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=50,
        timeZone=TZ,  # anders geeft de API UTC terug (vers account zonder tijdzone)
    ).execute()
    out = []
    for ev in result.get("items", []):
        start = ev["start"].get("dateTime", ev["start"].get("date", ""))
        eind = ev.get("end", {}).get("dateTime")
        when = _tijdvak(start, eind)
        out.append((when[:16], f"{when} | {ev.get('summary', '(zonder titel)')}"))
    return out


def ics_feeds() -> list[tuple[str, str]]:
    """Alleen-lezen iCal-bronnen naast de Google-agenda: [(label, url), …].
    AGENDA_ICS_FEEDS="Label|https://…;Ander label|https://…" plus (oud) FAMILYWALL_ICS_URL
    als bron "FamilyWall". Staat er een persoonsnaam in het label ("Volleybal Yvette"),
    dan kleurt het dashboard die afspraken als van die persoon."""
    feeds: list[tuple[str, str]] = []
    fw = os.environ.get("FAMILYWALL_ICS_URL", "").strip()
    if fw:
        feeds.append(("FamilyWall", fw))
    for item in os.environ.get("AGENDA_ICS_FEEDS", "").split(";"):
        item = item.strip()
        if not item:
            continue
        label, _, url = item.partition("|")
        if not url.strip():  # alleen een url → label uit de hostnaam
            label, url = url or label, label
            label = label.split("//")[-1].split("/")[0]
        feeds.append((label.strip(), url.strip()))
    return feeds


def _ics_events(days: int) -> list[tuple[str, str]]:
    """(sorteersleutel, regel) uit alle iCal-feeds; elke regel eindigt op '(label)'."""
    feeds = ics_feeds()
    if not feeds:
        return []
    import requests
    import icalendar
    import recurring_ical_events

    now = datetime.now()
    out = []
    for label, url in feeds:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        cal = icalendar.Calendar.from_ical(resp.content)
        for ev in recurring_ical_events.of(cal).between(now, now + timedelta(days=days)):
            start = ev.get("DTSTART").dt
            eind = ev.get("DTEND")
            when = _tijdvak(start, eind.dt if eind and isinstance(start, datetime) else None)
            title = str(ev.get("SUMMARY", "(zonder titel)"))
            out.append((when[:16], f"{when} | {title} ({label})"))
    return out


def cmd_list(days: int) -> None:
    rows: list[tuple[str, str]] = []
    errors: list[str] = []
    for source, fn in (("Google-agenda", _google_events), ("iCal-feeds", _ics_events)):
        try:
            rows.extend(fn(days))
        except SystemExit as e:
            errors.append(f"{source}: {e}")
        except Exception as e:
            errors.append(f"{source}: {type(e).__name__}: {e}")
    seen = set()
    labels = [f" ({label})" for label, _ in ics_feeds()]
    if rows:
        print(f"Tijden zijn Nederlandse tijd ({TZ}), begin–eind. Dit is de enige betrouwbare bron voor tijden.")
    for _, line in sorted(rows):
        # zelfde moment + titel uit meerdere bronnen → één keer tonen
        norm = line
        for suffix in labels:
            norm = norm.replace(suffix, "")
        if norm in seen:
            continue
        seen.add(norm)
        print(line)
    if not rows:
        print(f"Geen afspraken in de komende {days} dagen."
              if not errors else "Geen agenda gekoppeld of bereikbaar.")
    for err in errors:
        print(f"[let op] {err}")


def cmd_add(title: str, start: str | None, dag: str | None, duur: int, jaarlijks: bool = False) -> None:
    svc = _service()
    if dag:
        body = {
            "summary": title,
            "start": {"date": dag},
            "end": {"date": (datetime.strptime(dag, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")},
        }
        if jaarlijks:
            body["recurrence"] = ["RRULE:FREQ=YEARLY"]
    elif start:
        begin = datetime.strptime(start, "%Y-%m-%d %H:%M")
        body = {
            "summary": title,
            "start": {"dateTime": begin.isoformat(), "timeZone": TZ},
            "end": {"dateTime": (begin + timedelta(minutes=duur)).isoformat(), "timeZone": TZ},
        }
    else:
        sys.exit("Geef --start of --dag op")
    ev = svc.events().insert(calendarId=_cal_id(), body=body).execute()
    print(f"Toegevoegd: {ev.get('summary')} ({ev.get('htmlLink', '')})")


# ── iCal-feed → Google-gezinsagenda synchroniseren (bijv. Nevobo-wedstrijdprogramma) ──
# Zo staan wedstrijden gewoon in Google Agenda (ook op ieders telefoon) en leest Birdy ze
# als normale afspraken. Gesynchroniseerde events dragen een privé-eigenschap
# birdy_sync=<label> + de iCal-UID; alleen die events worden aangeraakt.

SYNC_PROP = "birdy_sync"
SYNC_VENSTER_TERUG, SYNC_VENSTER_VOORUIT = 7, 400  # dagen


def sync_feeds() -> list[tuple[str, str]]:
    """AGENDA_SYNC_ICS="Label|url;Label|url" → feeds die ín de Google-agenda gezet worden."""
    feeds = []
    for item in os.environ.get("AGENDA_SYNC_ICS", "").split(";"):
        label, _, url = item.strip().partition("|")
        if url.strip():
            feeds.append((label.strip(), url.strip()))
    return feeds


def _ics_items(url: str, van: datetime, tot: datetime) -> dict[str, dict]:
    """Afspraken uit een iCal-feed als {sleutel: {summary, start, end, location, description}}.
    start/end zijn dicts zoals Google ze wil ({dateTime, timeZone} of {date})."""
    import requests
    import icalendar
    import recurring_ical_events

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    cal = icalendar.Calendar.from_ical(resp.content)
    items: dict[str, dict] = {}
    for ev in recurring_ical_events.of(cal).between(van, tot):
        start = ev.get("DTSTART").dt
        eind = ev.get("DTEND")
        eind = eind.dt if eind else None
        if isinstance(start, datetime):
            s, e = _lokaal(start), _lokaal(eind) if isinstance(eind, datetime) else _lokaal(start) + timedelta(hours=2)
            g_start = {"dateTime": s.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TZ}
            g_end = {"dateTime": e.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TZ}
            sleutel_tijd = s.strftime("%Y-%m-%dT%H:%M")
        else:
            g_start = {"date": start.strftime("%Y-%m-%d")}
            g_end = {"date": (eind or start + timedelta(days=1)).strftime("%Y-%m-%d")}
            sleutel_tijd = start.strftime("%Y-%m-%d")
        uid = str(ev.get("UID", "")) or f"{sleutel_tijd}|{ev.get('SUMMARY', '')}"
        # herhalende feeds: UID + begintijd is uniek per voorkomen
        sleutel = f"{uid}@{sleutel_tijd}"
        items[sleutel] = {
            "summary": str(ev.get("SUMMARY", "(zonder titel)")).strip(),
            "start": g_start, "end": g_end,
            "location": str(ev.get("LOCATION", "")).strip(),
            "description": str(ev.get("DESCRIPTION", "")).strip()[:1000],
        }
    return items


def _sync_plan(label: str, ics: dict[str, dict], google: list[dict]) -> tuple[list, list, list]:
    """Pure vergelijking: (aanmaken, bijwerken [(event_id, body)], verwijderen [event_id])."""
    voettekst = f"Automatisch uit {label}. Wijzigingen in Google worden bij de volgende synchronisatie overschreven."

    def body_voor(sleutel: str, it: dict) -> dict:
        beschrijving = (it["description"] + "\n\n" if it["description"] else "") + voettekst
        return {
            "summary": it["summary"], "start": it["start"], "end": it["end"],
            "location": it["location"], "description": beschrijving,
            "extendedProperties": {"private": {SYNC_PROP: label, "birdy_sync_key": sleutel}},
        }

    def kern(b: dict) -> tuple:
        s, e = b.get("start", {}), b.get("end", {})
        return (b.get("summary", ""), s.get("dateTime", s.get("date", ""))[:19],
                e.get("dateTime", e.get("date", ""))[:19], b.get("location", "") or "")

    bestaand = {}
    for ev in google:
        sleutel = (ev.get("extendedProperties", {}).get("private", {}) or {}).get("birdy_sync_key", "")
        if sleutel:
            bestaand.setdefault(sleutel, ev)
    aanmaken, bijwerken, verwijderen = [], [], []
    for sleutel, it in ics.items():
        body = body_voor(sleutel, it)
        if sleutel not in bestaand:
            aanmaken.append(body)
        elif kern(bestaand[sleutel]) != kern(body):
            bijwerken.append((bestaand[sleutel]["id"], body))
    for sleutel, ev in bestaand.items():
        if sleutel not in ics:
            verwijderen.append(ev["id"])
    # dubbele Google-events met dezelfde sleutel (mag niet voorkomen) → extra's weg
    gezien: set[str] = set()
    for ev in google:
        sleutel = (ev.get("extendedProperties", {}).get("private", {}) or {}).get("birdy_sync_key", "")
        if sleutel in gezien:
            verwijderen.append(ev["id"])
        elif sleutel:
            gezien.add(sleutel)
    return aanmaken, bijwerken, verwijderen


def sync_ics(label: str, url: str) -> dict:
    """Eén feed synchroniseren naar de Google-gezinsagenda. Geeft tellingen terug."""
    svc = _service()
    cal = _cal_id()
    nu = datetime.now()
    van, tot = nu - timedelta(days=SYNC_VENSTER_TERUG), nu + timedelta(days=SYNC_VENSTER_VOORUIT)
    ics = _ics_items(url, van, tot)
    google: list[dict] = []
    token = None
    while True:
        resp = svc.events().list(
            calendarId=cal, privateExtendedProperty=f"{SYNC_PROP}={label}",
            timeMin=van.astimezone().isoformat(), timeMax=tot.astimezone().isoformat(),
            singleEvents=True, maxResults=250, pageToken=token, timeZone=TZ,
            showDeleted=False,
        ).execute()
        google.extend(resp.get("items", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    aanmaken, bijwerken, verwijderen = _sync_plan(label, ics, google)
    for body in aanmaken:
        svc.events().insert(calendarId=cal, body=body).execute()
    for event_id, body in bijwerken:
        svc.events().patch(calendarId=cal, eventId=event_id, body=body).execute()
    for event_id in verwijderen:
        svc.events().delete(calendarId=cal, eventId=event_id).execute()
    return {"label": label, "in_feed": len(ics), "aangemaakt": len(aanmaken),
            "bijgewerkt": len(bijwerken), "verwijderd": len(verwijderen)}


def sync_all() -> list[dict]:
    """Alle AGENDA_SYNC_ICS-feeds synchroniseren; fouten per feed worden teruggegeven."""
    out = []
    for label, url in sync_feeds():
        try:
            out.append(sync_ics(label, url))
        except Exception as e:  # noqa: BLE001 - één kapotte feed mag de rest niet stoppen
            out.append({"label": label, "fout": f"{type(e).__name__}: {e}"})
    return out


def cmd_sync() -> None:
    if not sync_feeds():
        sys.exit("AGENDA_SYNC_ICS is leeg — niets te synchroniseren.")
    for r in sync_all():
        if "fout" in r:
            print(f"[let op] {r['label']}: {r['fout']}")
        else:
            print(f"{r['label']}: {r['in_feed']} in feed · {r['aangemaakt']} aangemaakt · "
                  f"{r['bijgewerkt']} bijgewerkt · {r['verwijderd']} verwijderd")


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync-ics", help="iCal-feeds uit AGENDA_SYNC_ICS in de Google-agenda zetten")
    pl = sub.add_parser("list")
    pl.add_argument("--days", type=int, default=7)
    pa = sub.add_parser("add")
    pa.add_argument("title")
    pa.add_argument("--start", help='"YYYY-MM-DD HH:MM"')
    pa.add_argument("--dag", help="YYYY-MM-DD (hele dag)")
    pa.add_argument("--duur", type=int, default=60, help="minuten")
    pa.add_argument("--jaarlijks", action="store_true",
                    help="herhaal elk jaar (alleen met --dag, bijv. verjaardagen)")
    args = p.parse_args()
    if args.cmd == "list":
        cmd_list(args.days)
    elif args.cmd == "sync-ics":
        cmd_sync()
    else:
        cmd_add(args.title, args.start, args.dag, args.duur, args.jaarlijks)


if __name__ == "__main__":
    main()
