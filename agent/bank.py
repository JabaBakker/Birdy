"""Bankexports (3.1): ING-csv, ABN AMRO-pdf en ICS-creditcardafschriften → één lijst
transacties in workspace/memory/transacties.json, gecategoriseerd met de Herkenning-kolommen
uit het register en een set trefwoordregels. Geen LLM, geen tokens. Het dashboard toont er
per categorie een pop-out mee (maandtotalen + wat er precies is uitgegeven).

CLI voor het brein:
    python /app/agent/bank.py toon [--maand 2026-08] [--categorie Boodschappen]
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from datetime import date, timedelta, datetime
from pathlib import Path

MAX_TX = 20000

# ── parsers ──────────────────────────────────────────────────────────────────

def _bedrag(s) -> float:
    return float(str(s).replace(".", "").replace(",", "."))


def parse_ing_csv(data: bytes, rekening_naam: str = "") -> list[dict]:
    """ING-export (Engels of Nederlands kopregel)."""
    tekst = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(tekst)))
    out = []
    for r in rows:
        k = {kk.strip().lower(): v for kk, v in r.items() if kk}
        datum = k.get("date") or k.get("datum") or ""
        naam = k.get("name / description") or k.get("naam / omschrijving") or ""
        rek = k.get("account") or k.get("rekening") or ""
        tegen = k.get("counterparty") or k.get("tegenrekening") or ""
        af_bij = (k.get("debit/credit") or k.get("af bij") or "").strip().lower()
        bedrag = _bedrag(k.get("amount (eur)") or k.get("bedrag (eur)") or "0")
        soort = k.get("transaction type") or k.get("mutatiesoort") or ""
        oms = k.get("notifications") or k.get("mededelingen") or ""
        if not datum:
            continue
        try:
            d = datetime.strptime(datum, "%Y%m%d").date()
        except ValueError:
            continue
        b = -bedrag if af_bij in ("debit", "af") else bedrag
        out.append(_tx(d, rekening_naam or rek, naam, tegen, soort, b, oms))
    return out


def parse_abn_pdf(data: bytes, rekening_naam: str = "ABN") -> list[dict]:
    """ABN AMRO 'Bij- en afschrijvingen' pdf (layout-modus: kolompositie bepaalt af/bij)."""
    from pypdf import PdfReader

    rd = PdfReader(io.BytesIO(data))
    out, huidig = [], None
    for p in rd.pages:
        lines = p.extract_text(extraction_mode="layout").splitlines()
        kop = next((l for l in lines if "Bedrag af" in l and "Bedrag bij" in l), None)
        if not kop:
            continue
        grens = kop.index("Bedrag bij") - 2
        for l in lines:
            m = re.match(r"^(\d{2}-\d{2}-\d{4})\s+(.*)$", l)
            if m:
                if huidig:
                    out.append(huidig)
                rest = m.group(2)
                am = re.search(r"(-?\d{1,3}(?:\.\d{3})*,\d{2})\s*$", rest)
                b = None
                if am:
                    eind = l.rindex(am.group(1)) + len(am.group(1))
                    b = _bedrag(am.group(1)) * (-1 if eind <= grens else 1)
                    rest = rest[:am.start()].rstrip()
                huidig = {"datum": datetime.strptime(m.group(1), "%d-%m-%Y").date(), "oms": rest, "bedrag": b}
            elif huidig is not None and l.startswith(" ") and l.strip() and "Pagina" not in l and "Datum " not in l:
                s = l.strip()
                am = re.search(r"(-?\d{1,3}(?:\.\d{3})*,\d{2})\s*$", l)
                if huidig["bedrag"] is None and am and l.rindex(am.group(1)) > 60:
                    eind = l.rindex(am.group(1)) + len(am.group(1))
                    huidig["bedrag"] = _bedrag(am.group(1)) * (-1 if eind <= grens else 1)
                    s = s[:am.start() - (len(l) - len(l.lstrip()))].rstrip() if am.start() > 0 else ""
                huidig["oms"] += " " + s
    if huidig:
        out.append(huidig)
    res = []
    for h in out:
        if h["bedrag"] is None:
            continue
        o = re.sub(r"\s+", " ", h["oms"]).strip()
        naam, tegen, soort = "", "", "overig"
        if "/TRTP/" in o:
            m = re.search(r"/NAME/(.+?)/", o); naam = m.group(1).strip() if m else ""
            m = re.search(r"/IBAN/([A-Z]{2}\d{2}[A-Z0-9]{4}\d{10})", o); tegen = m.group(1) if m else ""
            ms = re.search(r"/TRTP/(.+?)/", o); s = ms.group(1).lower() if ms else ""
            soort = "incasso" if "incasso" in s else "ideal" if "ideal" in s else "overboeking" if "overboeking" in s else "overig"
            m = re.search(r"/REMI/(.+?)/", o); o = (m.group(1).strip() if m else o)
        elif o.startswith("BEA,"):
            soort = "pin"; naam = re.sub(r"^BEA, (Apple Pay )?", "", o).split(",PAS")[0].strip()
        elif "SEPA Incasso" in o:
            soort = "incasso"; m = re.search(r"Naam: (.+?) (Machtiging|Omschrijving):", o); naam = m.group(1).strip() if m else o[:40]
        elif "SEPA Overboeking" in o or "SEPA Periodieke overb" in o:
            soort = "overboeking"; m = re.search(r"Naam: (.+?) Omschrijving:", o); naam = m.group(1).strip() if m else o[:40]
            m2 = re.search(r"IBAN: ([A-Z]{2}\d{2}[A-Z0-9]{4}\d{10})", o); tegen = m2.group(1) if m2 else ""
        elif "SEPA iDEAL" in o:
            soort = "ideal"; m = re.search(r"Naam: (.+?) Omschrijving:", o); naam = m.group(1).strip() if m else o[:40]
        elif "RENTE" in o.upper():
            soort = "rente"; naam = "Bankrente"
        elif o.startswith("eCom,"):
            soort = "pin"; naam = re.sub(r"^eCom, (Apple Pay )?", "", o).split(",PAS")[0][:40].strip()
        else:
            naam = o[:40]
        res.append(_tx(h["datum"], rekening_naam, naam, tegen, soort, h["bedrag"], o))
    return res


def parse_ics_pdf(data: bytes, rekening_naam: str = "Creditcard") -> list[dict]:
    """ICS-creditcardafschrift (ABN AMRO creditcard)."""
    from pypdf import PdfReader

    MND = {"jan.": 1, "feb.": 2, "mrt.": 3, "apr.": 4, "mei": 5, "jun.": 6, "jul.": 7, "aug.": 8, "sep.": 9,
           "okt.": 10, "nov.": 11, "dec.": 12,
           "januari": 1, "februari": 2, "maart": 3, "april": 4, "juni": 6, "juli": 7, "augustus": 8,
           "september": 9, "oktober": 10, "november": 11, "december": 12}
    rd = PdfReader(io.BytesIO(data))
    tekst = "\n".join(p.extract_text() or "" for p in rd.pages)
    mj = re.search(r"Datum\s+ICS-klantnummer.*?\n\s*(\d{1,2}) (\w+\.?) (\d{4})", tekst, re.S)
    jaar = int(mj.group(3)) if mj else date.today().year
    afschrift_mnd = MND.get(mj.group(2).lower(), 0) if mj else 0
    out = []
    for l in tekst.splitlines():
        m = re.match(r"^(\d{2}) (\w+\.?) (\d{2}) (\w+\.?) (.+?) (\d{1,3}(?:\.\d{3})*,\d{2}) (Af|Bij)$", l.strip())
        if not m:
            continue
        dag, mnd = int(m.group(1)), MND.get(m.group(2).lower(), 0)
        if not mnd:
            continue
        j = jaar - 1 if (afschrift_mnd and mnd > afschrift_mnd) else jaar
        if date(j, mnd, dag) > date.today() + timedelta(days=7):  # vangnet: een afschrift ligt nooit in de toekomst
            j -= 1
        oms = m.group(5)
        vv = re.search(r" (\d[\d.,]*) ([A-Z]{3})$", oms)
        if vv:
            oms = oms[:vv.start()]
        bedrag = _bedrag(m.group(6)) * (1 if m.group(7) == "Bij" else -1)
        naam = re.sub(r"\s+[A-Z]{3}$", "", re.sub(r"\*.*$", "", oms)).strip()[:40]
        out.append(_tx(date(j, mnd, dag), rekening_naam, naam or oms[:40], "", "creditcard", bedrag, oms.strip()))
    return out


def _tx(d: date, rekening: str, naam: str, tegen: str, soort: str, bedrag: float, oms: str) -> dict:
    naam = re.sub(r"\s+", " ", naam or "").strip()[:60]
    oms = re.sub(r"\s+", " ", oms or "").strip()[:240]
    sleutel = hashlib.sha1(f"{d.isoformat()}|{rekening}|{round(bedrag, 2)}|{naam.lower()}|{oms[:80].lower()}".encode()).hexdigest()[:16]
    return {"id": sleutel, "datum": d.isoformat(), "rekening": rekening, "naam": naam, "tegen_iban": tegen,
            "soort": soort, "bedrag": round(bedrag, 2), "omschrijving": oms}


def parse_bestand(naam: str, data: bytes, rekening_naam: str = "") -> tuple[str, list[dict]]:
    """Herkent het type aan inhoud/naam. Geeft (rekeningnaam, transacties)."""
    n = naam.lower()
    if n.endswith(".csv"):
        kop = data[:300].decode("utf-8-sig", errors="replace").lower()
        if "name / description" in kop or "naam / omschrijving" in kop:
            return rekening_naam or "ING", parse_ing_csv(data, rekening_naam)
        raise ValueError("csv niet herkend (verwacht ING-export)")
    if n.endswith(".pdf"):
        from pypdf import PdfReader
        eerste = (PdfReader(io.BytesIO(data)).pages[0].extract_text() or "")[:600]
        if "International Card Services" in eerste or "ICS-klantnummer" in eerste:
            return rekening_naam or "Creditcard ICS", parse_ics_pdf(data, rekening_naam or "Creditcard ICS")
        if "Bij- en afschrijvingen" in eerste or "Rekeninghouder" in eerste:
            return rekening_naam or "ABN", parse_abn_pdf(data, rekening_naam or "ABN")
        raise ValueError("pdf niet herkend (verwacht ABN AMRO-afschrift of ICS-creditcard)")
    raise ValueError("alleen .csv (ING) of .pdf (ABN AMRO, ICS)")


# ── categoriseren ─────────────────────────────────────────────────────────────

REGELS: list[tuple[str, tuple[str, ...]]] = [
    ("Boodschappen", ("picnic", "albert heijn", "ah ", "jumbo", "lidl", "aldi", "plus ", "dirk", "ekoplaza", "spar ")),
    ("Wonen", ("essent", "vattenfall", "eneco retail", "brabant water", "vitens", "gemeente", "waterschap", "belastingsamenwerking")),
    ("Kinderen", ("smallsteps", "kindbureau", "kinderopvang", "bso", "school", "zwemles", "speelgoed", "intertoys", "baby-dump", "prenatal")),
    ("Verzekeringen", ("verzeker", "vgz", "cz ", "zilveren kruis", "menzis", "allianz", "asr", "a.s.r", "rheinland", "credit life", "schadev", "polis")),
    ("Hypotheek", ("hypotheek", "levverz", "grazi")),
    ("Auto & vervoer", ("q-park", "tap electric", "parkeer", "easypark", "shell", "bp ", "tinq", "tango", "esso", "total", "ns.nl", "ov-chip", "anwb", "kwikfit", "garage", "rdw", "mrb", "motorrijtuig", "eneco emobility", "fastned", "laadpaal")),
    ("Abonnementen & media", ("netflix", "spotify", "disney+", "videoland", "hbo", "apple.com", "google", "amazon prime", "amznprime", "microsoft", "openai", "anthropic", "claude", "remarkable", "runna", "lintberg", "flaticon", "hetzner", "tesla?nl?subscription", "odido", "kpn", "ziggo", "t-mobile", "vodafone")),
    ("Eten & drinken buiten de deur", ("thuisbezorgd", "domino", "eetpaleis", "la place", "restaurant", "cafe", "bakker", "bakkerij", "mcdonald", "kfc", "burger", "pizza", "sushi", "starbucks", "coffee")),
    ("Kleding & winkels", ("wibra", "zeeman", "action", "vanharen", "van haren", "hema", "h&m", "zara", "c&a", "primark", "toppy", "ibood", "coolblue", "bol.com", "mediamarkt", "mm den bosch", "ikea", "kwantum", "blokker", "kruidvat", "etos", "lucardi")),
    ("Uitjes & vakantie", ("landal", "vennenbos", "disney", "capfun", "francecomfort", "booking", "efteling", "camping", "sunweb", "tui", "transavia", "klm", "hotel", "airbnb", "bioscoop", "pathe", "zwembad", "combibad", "dierentuin", "ticket")),
    ("Gezondheid & sport", ("wellis", "apotheek", "tandarts", "fysio", "massage", "sportcentr", "sportschool", "basic-fit", "run2day", "ouraring", "oura")),
    ("Cadeaus & feestjes", ("tikkie", "party", "cadeau", "bloemen", "greetz")),
    ("Bank & rente", ("basispakket", "bankrente", "kosten tweede rekeninghouder", "rente")),
    ("Belasting", ("belastingdienst",)),
    ("Sparen & beleggen", ("flatex", "degiro", "bright", "meesman", "brand new day", "spaarrekening")),
    ("Studieschuld", ("duo",)),
    ("Creditcard", ("creditcard", "int card services", "ics ")),
    ("Salaris & inkomen", ("salaris", "loon", "expenses", "declaratie", "sociale verzekeringsbank", "svb", "toeslag", "kinderbijslag", "teruggaaf")),
]


def _herkenningen(register: dict | None) -> list[tuple[str, str, str]]:
    """Uit het register: (trefwoord, categorie, postnaam) — de Herkenning-kolommen winnen van de regels."""
    out = []
    if not register or not register.get("beschikbaar"):
        return out
    for l in register.get("vaste_lasten", []):
        for w in str(l.get("herkenning") or "").split("|"):
            if w.strip():
                out.append((w.strip().lower(), l.get("categorie") or "Vaste lasten", l["naam"]))
    for p in register.get("polissen", []):
        for w in str(p.get("herkenning") or "").split("|"):
            if w.strip():
                out.append((w.strip().lower(), "Verzekeringen", p["naam"]))
    for x in register.get("variabel", []):
        for w in str(x.get("herkenning") or "").split("|"):
            if w.strip():
                out.append((w.strip().lower(), x["naam"], x["naam"]))
    for i in register.get("inkomsten", []):
        for w in str(i.get("herkenning") or "").split("|"):
            if w.strip():
                out.append((w.strip().lower(), "Salaris & inkomen", i["naam"]))
    return out


def categoriseer(tx: list[dict], register: dict | None = None) -> None:
    """Zet per transactie 'categorie' en 'post' (registerregel) — in place."""
    herk = _herkenningen(register)
    eigen_ibans = {r["iban"].replace(" ", "").upper() for r in (register or {}).get("rekeningen", []) if r.get("iban")} if register else set()
    for t in tx:
        s = f"{t['naam']} {t['omschrijving']} {t['tegen_iban']}".lower()
        t["post"] = ""
        if t["tegen_iban"] and t["tegen_iban"].upper() in eigen_ibans or "maandelijkse inleg" in s or "gezamenlijke inleg" in s:
            t["categorie"] = "Overboeking eigen rekeningen"
            continue
        if "geincasseerd vorig saldo" in s:
            t["categorie"] = "Creditcard (verrekening)"
            continue
        for w, cat, post in herk:
            if w in s:
                t["categorie"], t["post"] = cat, post
                break
        else:
            for cat, woorden in REGELS:
                if any(w in s for w in woorden):
                    t["categorie"] = cat
                    break
            else:
                t["categorie"] = "Inkomen (overig)" if t["bedrag"] > 0 else "Overig"


# ── opslag & samenvatting ─────────────────────────────────────────────────────

def _pad(workspace: Path) -> Path:
    return workspace / "memory" / "transacties.json"


def laad(workspace: Path) -> list[dict]:
    try:
        d = json.loads(_pad(workspace).read_text())
        return list(d) if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def voeg_toe(workspace: Path, nieuw: list[dict], register: dict | None = None) -> dict:
    """Samenvoegen zonder dubbelen (op id), hercategoriseren, opslaan. Geeft tellingen."""
    bestaand = laad(workspace)
    ids = {t["id"] for t in bestaand}
    toegevoegd = [t for t in nieuw if t["id"] not in ids]
    alles = bestaand + toegevoegd
    categoriseer(alles, register)
    alles.sort(key=lambda t: t["datum"])
    alles = alles[-MAX_TX:]
    pad = _pad(workspace)
    pad.parent.mkdir(parents=True, exist_ok=True)
    tmp = pad.with_suffix(".tmp")
    tmp.write_text(json.dumps(alles, ensure_ascii=False))
    tmp.replace(pad)
    return {"toegevoegd": len(toegevoegd), "dubbel": len(nieuw) - len(toegevoegd), "totaal": len(alles),
            "van": alles[0]["datum"] if alles else "", "tot": alles[-1]["datum"] if alles else ""}


def partij_sleutel(naam: str) -> str:
    """'AH Jan Linders 4176 ROSMALEN' en 'AH - Jan Linders 4176' → 'ah jan linders': zelfde winkel, één regel."""
    n = re.sub(r"[^a-z ]", " ", re.sub(r"\d+", "", (naam or "").lower()))
    n = re.sub(r"\b(bv|b v|nv|via|stichting|mollie|pay|apple|nld|rosmalen|den bosch|shertogenbosch|eindhoven)\b", " ", n)
    return " ".join(n.split()[:3]) or (naam or "").lower()[:20]


def samenvatting(tx: list[dict], maanden: int = 6, categorie: str | None = None, post: str | None = None,
                 rekening: str | None = None) -> dict:
    """Per maand totalen (uit/in) en per tegenpartij, voor de pop-out."""
    sel = [t for t in tx if (not categorie or t.get("categorie") == categorie) and (not post or t.get("post") == post)
           and (not rekening or t["rekening"] == rekening)]
    per_maand: dict[str, dict] = defaultdict(lambda: {"uit": 0.0, "in": 0.0, "n": 0})
    for t in sel:
        m = t["datum"][:7]
        per_maand[m]["uit" if t["bedrag"] < 0 else "in"] += abs(t["bedrag"])
        per_maand[m]["n"] += 1
    mnd = sorted(per_maand)[-maanden:] if maanden else sorted(per_maand)
    per_partij: dict[str, dict] = defaultdict(lambda: {"n": 0, "totaal": 0.0, "laatste": "", "naam": ""})
    for t in sel:
        if mnd and t["datum"][:7] < mnd[0]:
            continue
        k = partij_sleutel(t["naam"] or t["omschrijving"][:30])
        p = per_partij[k]
        p["n"] += 1; p["totaal"] += t["bedrag"]; p["laatste"] = max(p["laatste"], t["datum"])
        if not p["naam"] or len(t["naam"]) < len(p["naam"]):
            p["naam"] = t["naam"] or t["omschrijving"][:30]
    volledige = [m for m in mnd if m != date.today().isoformat()[:7]]
    gem = (sum(per_maand[m]["uit"] for m in volledige) / len(volledige)) if volledige else 0.0
    return {
        "categorie": categorie or "", "post": post or "", "maanden": [{"maand": m, **{k: round(v, 2) if isinstance(v, float) else v for k, v in per_maand[m].items()}} for m in mnd],
        "gemiddeld_uit_pm": round(gem, 2),
        "partijen": sorted(({"sleutel": k, **{kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}} for k, v in per_partij.items()),
                           key=lambda x: x["totaal"])[:40],
        "transacties": [{**t, "sleutel": partij_sleutel(t["naam"] or t["omschrijving"][:30])} for t in sel if not mnd or t["datum"][:7] >= mnd[0]][-300:][::-1],
        "aantal": len(sel),
    }


def categorieen(tx: list[dict], maanden: int = 6) -> list[dict]:
    """Overzicht per categorie (uitgaven) over de laatste maanden, voor de kaarten."""
    mnd = sorted({t["datum"][:7] for t in tx})[-maanden:]
    volledige = [m for m in mnd if m != date.today().isoformat()[:7]] or mnd
    tot: dict[str, float] = defaultdict(float); n: dict[str, int] = defaultdict(int)
    for t in tx:
        if t["datum"][:7] in volledige and t["bedrag"] < 0 and t.get("categorie") not in ("Overboeking eigen rekeningen", "Creditcard (verrekening)"):
            tot[t["categorie"]] += -t["bedrag"]; n[t["categorie"]] += 1
    return sorted(({"categorie": c, "per_maand": round(v / max(1, len(volledige)), 2), "aantal": n[c]} for c, v in tot.items()),
                  key=lambda x: -x["per_maand"])


def toon(workspace: Path, maand: str | None = None, categorie: str | None = None) -> str:
    tx = laad(workspace)
    if not tx:
        return "Nog geen bankexports verwerkt (upload via het dashboard: Geld → ＋ → bankexport)."
    regels = [f"TRANSACTIES: {len(tx)} van {tx[0]['datum']} t/m {tx[-1]['datum']} · rekeningen: {sorted({t['rekening'] for t in tx})}"]
    if maand:
        sel = [t for t in tx if t["datum"].startswith(maand) and (not categorie or t["categorie"] == categorie)]
        regels.append(f"MAAND {maand}: {len(sel)} transacties · uit € {-sum(t['bedrag'] for t in sel if t['bedrag']<0):,.2f} · in € {sum(t['bedrag'] for t in sel if t['bedrag']>0):,.2f}")
        for t in sel[:200]:
            regels.append(f"  {t['datum']} {t['rekening'][:14]:14} {t['bedrag']:>9,.2f}  {t['categorie'][:22]:22} {t['naam'][:34]}")
    else:
        regels.append("PER CATEGORIE (gemiddeld per maand, laatste 6 volledige maanden):")
        for c in categorieen(tx):
            regels.append(f"  {c['categorie']:34} € {c['per_maand']:>9,.2f}  ({c['aantal']}x)")
    return "\n".join(regels)


def main() -> None:
    import argparse
    import os
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["toon"])
    p.add_argument("--maand"); p.add_argument("--categorie")
    a = p.parse_args()
    ws = Path(os.environ.get("AGENT_WORKSPACE", "/data/workspace"))
    print(toon(ws, a.maand, a.categorie))


if __name__ == "__main__":
    main()
