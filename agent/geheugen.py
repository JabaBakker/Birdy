"""Birdy's geheugen: één zichtbare lijst van wat Birdy onthoudt (memory/geheugen.json).

Elk punt heeft een onderwerp ("over": persoon of thema), een soort, een begindatum en
optioneel een "geldig tot"-datum. Verlopen punten gebruikt Birdy niet meer; ze blijven
30 dagen zichtbaar op het dashboard (tab Geheugen) en verdwijnen daarna. Wat iemand op het
dashboard "vergeet", blijft ook 30 dagen terug te halen.

De agent roept dit aan via Bash:
    python /app/agent/geheugen.py add "Chloe wil een tekenset" --over Chloe --soort cadeau --tot 2026-11-03
    python /app/agent/geheugen.py list            (actieve punten, met id)
    python /app/agent/geheugen.py wijzig g-1a2b3c --tot 2026-12-01 [--tekst ...] [--over ...]
    python /app/agent/geheugen.py vergeet g-1a2b3c
    python /app/agent/geheugen.py opruimen        (verlopen/vergeten punten ouder dan 30 dagen weg)
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

BEWAAR_DAGEN = 30          # zo lang blijven verlopen/vergeten punten nog zichtbaar
MAX_PUNTEN = 400           # ruim; voorkomt dat de lijst (en de prompt) onbeperkt groeit
MAX_TEKST = 500
SOORTEN = ("persoon", "voorkeur", "cadeau", "plan", "info")


def _pad() -> Path:
    # zelfde standaard als config.py: AGENT_WORKSPACE, anders <repo>/workspace
    ws = os.environ.get("AGENT_WORKSPACE") or Path(__file__).resolve().parent.parent / "workspace"
    return Path(ws) / "memory" / "geheugen.json"


@contextmanager
def _slot():
    """Exclusief slot voor lezen-wijzigen-schrijven (agent en dashboard schrijven allebei)."""
    pad = _pad()
    pad.parent.mkdir(parents=True, exist_ok=True)
    with open(pad.with_suffix(".lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _lees() -> list[dict]:
    try:
        data = json.loads(_pad().read_text())
    except (OSError, ValueError):
        return []
    return [p for p in data.get("punten", []) if isinstance(p, dict) and p.get("id")]


def _schrijf(punten: list[dict]) -> None:
    pad = _pad()
    tmp = pad.with_suffix(".tmp")
    tmp.write_text(json.dumps({"punten": punten}, ensure_ascii=False, indent=1))
    tmp.replace(pad)


def datum(v: str | None) -> str:
    """'2026-11-03', '03-11-2026' of '3-11-2026' → '2026-11-03'; leeg → ''. Anders ValueError."""
    v = (v or "").strip()
    if not v:
        return ""
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"onbekende datum '{v}' (gebruik JJJJ-MM-DD)")


def status(p: dict, vandaag: date | None = None) -> str:
    """'actief', 'verlopen' (tot-datum voorbij) of 'vergeten' (handmatig weggezet)."""
    if p.get("vergeten"):
        return "vergeten"
    vandaag = vandaag or date.today()
    if p.get("tot") and p["tot"] < vandaag.isoformat():
        return "verlopen"
    return "actief"


def _opruimen(punten: list[dict], vandaag: date) -> tuple[list[dict], int]:
    grens = (vandaag - timedelta(days=BEWAAR_DAGEN)).isoformat()
    blijft = []
    for p in punten:
        st = status(p, vandaag)
        sinds = p.get("vergeten") if st == "vergeten" else p.get("tot", "")
        if st != "actief" and sinds and sinds < grens:
            continue
        blijft.append(p)
    return blijft, len(punten) - len(blijft)


def alle(vandaag: date | None = None) -> list[dict]:
    """Alle punten met berekende status, nieuwste eerst."""
    vandaag = vandaag or date.today()
    out = [dict(p, status=status(p, vandaag)) for p in _lees()]
    out.sort(key=lambda p: (p.get("sinds", ""), p["id"]), reverse=True)
    return out


def actief(vandaag: date | None = None) -> list[dict]:
    return [p for p in alle(vandaag) if p["status"] == "actief"]


def voeg_toe(tekst: str, over: str = "", soort: str = "info", tot: str = "",
             bron: str = "", vandaag: date | None = None) -> dict:
    tekst = " ".join((tekst or "").split())[:MAX_TEKST]
    if not tekst:
        raise ValueError("lege tekst")
    vandaag = vandaag or date.today()
    tot = datum(tot)
    with _slot():
        punten, _ = _opruimen(_lees(), vandaag)
        for p in punten:  # zelfde tekst over hetzelfde onderwerp → bijwerken, niet dubbel
            if (status(p, vandaag) == "actief" and p["tekst"].lower() == tekst.lower()
                    and p.get("over", "").lower() == (over or "").strip().lower()):
                p.update(soort=soort or p.get("soort", "info"), tot=tot or p.get("tot", ""))
                _schrijf(punten)
                return p
        if len(punten) >= MAX_PUNTEN:
            raise ValueError(f"geheugen vol ({MAX_PUNTEN} punten) — ruim eerst op")
        p = {"id": "g-" + secrets.token_hex(3), "tekst": tekst, "over": (over or "").strip()[:60],
             "soort": (soort or "info").strip()[:20], "sinds": vandaag.isoformat(), "tot": tot,
             "bron": (bron or "").strip()[:80]}
        punten.append(p)
        _schrijf(punten)
        return p


def wijzig(pid: str, vandaag: date | None = None, **velden) -> dict:
    """Velden tekst/over/soort/tot aanpassen; vergeten=False haalt een punt terug."""
    vandaag = vandaag or date.today()
    with _slot():
        punten = _lees()
        for p in punten:
            if p["id"] == pid:
                if "tekst" in velden and velden["tekst"] is not None:
                    t = " ".join(velden["tekst"].split())[:MAX_TEKST]
                    if not t:
                        raise ValueError("lege tekst")
                    p["tekst"] = t
                for k in ("over", "soort"):
                    if velden.get(k) is not None:
                        p[k] = velden[k].strip()
                if velden.get("tot") is not None:
                    p["tot"] = datum(velden["tot"])
                if velden.get("vergeten") is False:
                    p.pop("vergeten", None)
                _schrijf(punten)
                return p
    raise KeyError(pid)


def vergeet(pid: str, vandaag: date | None = None) -> dict:
    vandaag = vandaag or date.today()
    with _slot():
        punten = _lees()
        for p in punten:
            if p["id"] == pid:
                p["vergeten"] = vandaag.isoformat()
                _schrijf(punten)
                return p
    raise KeyError(pid)


def opruimen(vandaag: date | None = None) -> int:
    vandaag = vandaag or date.today()
    with _slot():
        punten, weg = _opruimen(_lees(), vandaag)
        if weg:
            _schrijf(punten)
    return weg


def regel(p: dict) -> str:
    tot = f" · tot {p['tot']}" if p.get("tot") else ""
    over = f" · over {p['over']}" if p.get("over") else ""
    return f"[{p['id']}] ({p.get('soort', 'info')}{over}{tot}) {p['tekst']}"


def als_prompt(vandaag: date | None = None) -> str:
    """Actieve punten als tekst voor de systeemprompt. Verlopen punten komen er niet in."""
    punten = actief(vandaag)
    if not punten:
        return "(nog niets — je geheugen is leeg)"
    return "\n".join("- " + regel(p) for p in punten)


def main() -> None:
    ap = argparse.ArgumentParser(description="Birdy's zichtbare geheugen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("tekst")
    a.add_argument("--over", default="")
    a.add_argument("--soort", default="info", help="/".join(SOORTEN) + " (vrij)")
    a.add_argument("--tot", default="", help="geldig tot JJJJ-MM-DD (leeg = blijvend)")
    a.add_argument("--bron", default="")
    ls = sub.add_parser("list")
    ls.add_argument("--alles", action="store_true", help="ook verlopen en vergeten punten")
    w = sub.add_parser("wijzig")
    w.add_argument("id")
    w.add_argument("--tekst")
    w.add_argument("--over")
    w.add_argument("--soort")
    w.add_argument("--tot", help="nieuwe einddatum; '' maakt hem blijvend")
    v = sub.add_parser("vergeet")
    v.add_argument("id")
    sub.add_parser("opruimen")
    args = ap.parse_args()
    try:
        if args.cmd == "add":
            p = voeg_toe(args.tekst, args.over, args.soort, args.tot, args.bron)
            print("onthouden: " + regel(p))
        elif args.cmd == "list":
            punten = alle() if args.alles else actief()
            print("\n".join(
                regel(p) + ("" if p["status"] == "actief" else f"  [{p['status']}]") for p in punten
            ) or "(geheugen is leeg)")
        elif args.cmd == "wijzig":
            print("bijgewerkt: " + regel(wijzig(args.id, tekst=args.tekst, over=args.over,
                                                 soort=args.soort, tot=args.tot)))
        elif args.cmd == "vergeet":
            print("vergeten: " + regel(vergeet(args.id)))
        elif args.cmd == "opruimen":
            print(f"{opruimen()} oude punt(en) definitief verwijderd")
    except KeyError as e:
        sys.exit(f"geen geheugenpunt met id {e}; zie 'list'")
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
