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
    ).execute()
    out = []
    for ev in result.get("items", []):
        start = ev["start"].get("dateTime", ev["start"].get("date", ""))
        when = start.replace("T", " ")[:16]
        out.append((when, f"{when} | {ev.get('summary', '(zonder titel)')}"))
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
        if isinstance(start, datetime):
            when = start.strftime("%Y-%m-%d %H:%M")
        else:  # hele dag
            when = start.strftime("%Y-%m-%d")
        title = str(ev.get("SUMMARY", "(zonder titel)"))
        out.append((when, f"{when} | {title} (FamilyWall)"))
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


def cmd_add(title: str, start: str | None, dag: str | None, duur: int) -> None:
    svc = _service()
    if dag:
        body = {
            "summary": title,
            "start": {"date": dag},
            "end": {"date": (datetime.strptime(dag, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")},
        }
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
    args = p.parse_args()
    if args.cmd == "list":
        cmd_list(args.days)
    else:
        cmd_add(args.title, args.start, args.dag, args.duur)


if __name__ == "__main__":
    main()
