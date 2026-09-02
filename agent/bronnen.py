"""Gezinsbronnen voor het dashboard: het Google Doc "Wat loopt er" (onderwerpen), het
huishoudhandboek (regelzaken), de verjaardagenlijst, Birdy's AANDACHT.md en Homey."""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

log = logging.getLogger("fien.bronnen")

def datum_dagen(tekst: str, vandaag: date | None = None) -> int | None:
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


def onderwerpen_parse(text: str, vandaag: date | None = None) -> list[dict]:
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
            "dagen": datum_dagen(wanneer, vandaag) if wanneer else None,
            "stap": veld(s, "(?:volgende )?stap")[:160], "notitie": veld(s, "notitie")[:300],
        })
    out.sort(key=lambda o: (o["dagen"] is None, o["dagen"] if o["dagen"] is not None else 0))
    return out[:30]


def doc_tekst(pad: str) -> str:
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


def onderwerpen() -> list[dict]:
    return onderwerpen_parse(doc_tekst(ONDERWERPEN_DOC))


def aandacht_birdy(workspace: Path) -> dict:
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
    dagen = datum_dagen(tijd) if tijd else None
    if dagen is not None and dagen < -3:
        return {"tijd": tijd, "items": [], "oud": True}
    return {"tijd": tijd, "items": items[:3], "oud": False}


def regelzaken() -> list[dict]:
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


def thuis() -> dict | None:
    """Stand van het huis via Homey; None als niet gekoppeld of even onbereikbaar."""
    from . import homey

    if not homey.geconfigureerd():
        return None
    try:
        return homey.samenvatting()
    except BaseException:
        log.warning("Homey ophalen mislukt", exc_info=True)
        return None


def verjaardagen() -> list[dict]:
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
