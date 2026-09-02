"""Regel-gebaseerde aandachtspunten voor het dashboard, zonder LLM: acties over datum,
onderwerpen op datum, regelzaken te laat, verjaardagen zonder cadeau-idee, agenda-overlap
en mogelijke dubbelingen tussen acties en onderwerpen."""
from __future__ import annotations

from datetime import date, timedelta

def bereken(acties: list[dict], regelzaken: list[dict], verjaardagen: list[dict],
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

    for a_tekst, o_naam in dubbelingen(acties, onderwerpen)[:2]:
        out.append({"tekst": f"👀 Mogelijk dubbel: “{a_tekst[:30]}” (actie) en “{o_naam[:30]}” (onderwerp)",
                    "l2": "onderwerpen", "ernst": 1})

    out.sort(key=lambda s: s["ernst"])
    return out[:8]


STOPWOORDEN = {"voor", "naar", "over", "kopen", "regelen", "checken", "maken", "laten", "weten",
                "zodra", "bellen", "sturen", "versturen", "afmaken", "invullen", "geregeld", "vandaag",
                "morgen", "week", "deze", "die", "dat", "het", "een", "van", "met", "nog", "wordt"}


def woorden(tekst: str) -> set[str]:
    import re

    return {w for w in re.findall(r"[a-zà-ÿ0-9]+", tekst.lower()) if len(w) >= 4 and w not in STOPWOORDEN}


def dubbelingen(acties: list[dict], onderwerpen: list[dict]) -> list[tuple[str, str]]:
    """Actie en onderwerp die (bijna) over hetzelfde gaan: minstens twee gedeelde kernwoorden
    én meer dan de helft van de woorden van de kortste van de twee. Een actie die het
    onderwerp als voorvoegsel draagt ('Kinderfeest Evi: gastenlijst invullen') is bewust
    zo gemaakt en telt niet mee."""
    out = []
    for a in acties:
        wa = woorden(a.get("tekst", ""))
        if len(wa) < 2:
            continue
        for o in onderwerpen:
            if ":" in a.get("tekst", "") and a["tekst"].lower().startswith(o["naam"].lower()[:12]):
                continue
            wo = woorden(o["naam"])
            if len(wo) < 2:
                continue
            gedeeld = wa & wo
            if len(gedeeld) >= 2 and len(gedeeld) / min(len(wa), len(wo)) > 0.5:
                out.append((a["tekst"], o["naam"]))
                break
    return out
