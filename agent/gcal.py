"""Google Calendar-hulpje voor Fien (service-account).

De agent roept dit aan via Bash:
    python /app/agent/gcal.py list --days 7
    python /app/agent/gcal.py add "Titel" --start "2026-08-21 14:00" --duur 60
    python /app/agent/gcal.py add "Titel" --dag 2026-08-21          (hele dag)

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


def _familywall_events(days: int) -> list[tuple[str, str]]:
    """(sorteersleutel, regel) uit de FamilyWall iCal-leeslink. Leeg als niet ingesteld."""
    url = os.environ.get("FAMILYWALL_ICS_URL", "")
    if not url:
        return []
    import requests
    import icalendar
    import recurring_ical_events

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    cal = icalendar.Calendar.from_ical(resp.content)
    now = datetime.now()
    events = recurring_ical_events.of(cal).between(now, now + timedelta(days=days))
    out = []
    for ev in events:
        start = ev.get("DTSTART").dt
        eind = ev.get("DTEND")
        when = _tijdvak(start, eind.dt if eind and isinstance(start, datetime) else None)
        title = str(ev.get("SUMMARY", "(zonder titel)"))
        out.append((when[:16], f"{when} | {title} (FamilyWall)"))
    return out


def cmd_list(days: int) -> None:
    rows: list[tuple[str, str]] = []
    errors: list[str] = []
    for source, fn in (("Google-agenda", _google_events), ("FamilyWall", _familywall_events)):
        try:
            rows.extend(fn(days))
        except SystemExit as e:
            errors.append(f"{source}: {e}")
        except Exception as e:
            errors.append(f"{source}: {type(e).__name__}: {e}")
    seen = set()
    if rows:
        print(f"Tijden zijn Nederlandse tijd ({TZ}), begin–eind. Dit is de enige betrouwbare bron voor tijden.")
    for _, line in sorted(rows):
        # zelfde moment + titel uit beide bronnen → één keer tonen
        norm = line.replace(" (FamilyWall)", "")
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


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
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
    else:
        cmd_add(args.title, args.start, args.dag, args.duur, args.jaarlijks)


if __name__ == "__main__":
    main()
