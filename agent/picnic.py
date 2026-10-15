"""Picnic-koppeling: boodschappen in het Picnic-mandje zetten, met vaste productkeuzes.

Picnic heeft geen officiële API; dit gebruikt python-picnic-api2, dezelfde bibliotheek
als de Home Assistant-integratie. Birdy vult alleen het mandje; bestellen en betalen
doet het gezin zelf in de Picnic-app.

Vaste keuzes ("brood" → altijd hetzelfde bruine tijgerbrood) staan in
memory/picnic.json en zijn zichtbaar op het dashboard (tab Lijst).

Eenmalig koppelen (op de server, vraagt zo nodig om de sms-code):
    docker compose exec fien python /app/agent/picnic.py koppel

De agent roept dit aan via Bash:
    python /app/agent/picnic.py overzetten        (Todoist-boodschappen → mandje)
    python /app/agent/picnic.py zoek "tijgerbrood"
    python /app/agent/picnic.py kies "brood" s1018231 --naam "Tijgerbrood bruin heel" --eenheid "800 g"
    python /app/agent/picnic.py voeg s1018231 [--aantal 2]
    python /app/agent/picnic.py haal-weg s1018231 [--aantal 1]
    python /app/agent/picnic.py mandje
    python /app/agent/picnic.py keuzes  ·  vergeet-keuze "brood"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

try:  # als module (dashboard) of als los script (agent via Bash)
    from . import todoist
except ImportError:  # pragma: no cover - scriptmodus
    import todoist  # type: ignore


def _workspace() -> Path:
    return Path(os.environ.get("AGENT_WORKSPACE") or Path(__file__).resolve().parent.parent / "workspace")


def _token_pad() -> Path:
    return _workspace() / "secrets" / "picnic_token"


def _keuzes_pad() -> Path:
    return _workspace() / "memory" / "picnic.json"


def geconfigureerd() -> bool:
    return bool(os.environ.get("PICNIC_USERNAME") and os.environ.get("PICNIC_PASSWORD")) or _token_pad().exists()


class PicnicNietGekoppeld(Exception):
    """Geen gegevens, of er is eenmalig een sms-code nodig."""


# ── inloggen ──────────────────────────────────────────────────────────────

def _bewaar_token(token: str | None) -> None:
    if not token:
        return
    pad = _token_pad()
    pad.parent.mkdir(parents=True, exist_ok=True)
    pad.write_text(token)
    os.chmod(pad, 0o600)


def _nieuwe_api(token: str | None = None):
    from python_picnic_api2 import PicnicAPI

    return PicnicAPI(country_code=os.environ.get("PICNIC_COUNTRY", "NL"), auth_token=token)


def client():
    """Ingelogde PicnicAPI; gebruikt het bewaarde token en logt zo nodig opnieuw in."""
    from python_picnic_api2.session import Picnic2FARequired

    pad = _token_pad()
    if pad.exists():
        return _nieuwe_api(pad.read_text().strip())
    user, pw = os.environ.get("PICNIC_USERNAME", ""), os.environ.get("PICNIC_PASSWORD", "")
    if not (user and pw):
        raise PicnicNietGekoppeld("Picnic is niet gekoppeld (PICNIC_USERNAME/PICNIC_PASSWORD ontbreken in .env).")
    api = _nieuwe_api()
    try:
        api.login(user, pw)
    except Picnic2FARequired:
        raise PicnicNietGekoppeld(
            "Picnic vraagt een sms-code. Koppel eenmalig op de server: "
            "docker compose exec fien python /app/agent/picnic.py koppel")
    _bewaar_token(api.session.auth_token)
    return api


def _met_herlogin(fn):
    """Voer fn(api) uit; verloopt het bewaarde token, dan één keer opnieuw inloggen."""
    from python_picnic_api2.session import PicnicAuthError

    try:
        return fn(client())
    except PicnicAuthError:
        _token_pad().unlink(missing_ok=True)
        return fn(client())


def koppel() -> None:
    """Interactief: inloggen en zo nodig de sms-code invullen; bewaart het token."""
    from python_picnic_api2.session import Picnic2FARequired

    user, pw = os.environ.get("PICNIC_USERNAME", ""), os.environ.get("PICNIC_PASSWORD", "")
    if not (user and pw):
        sys.exit("Zet eerst PICNIC_USERNAME en PICNIC_PASSWORD in .env (en docker compose up -d --force-recreate).")
    api = _nieuwe_api()
    try:
        api.login(user, pw)
    except Picnic2FARequired:
        kanaal = (input("Picnic wil een code sturen. Via SMS of EMAIL? [SMS] ").strip().upper() or "SMS")
        api.generate_2fa_code(kanaal)
        code = input("Code: ").strip()
        api.verify_2fa_code(code)
    _bewaar_token(api.session.auth_token)
    naam = ""
    try:
        naam = (api.get_user().firstname or "")
    except Exception:  # noqa: BLE001 - alleen voor de bevestiging
        pass
    print(f"Gekoppeld{(' als ' + naam) if naam else ''}. Birdy kan nu het Picnic-mandje vullen.")


# ── vaste keuzes ──────────────────────────────────────────────────────────

def term(tekst: str) -> tuple[str, int]:
    """'2x Brood (Jaap)' → ('brood', 2). Haakjes, aantallen en leestekens eraf.
    Een aantal telt alleen met een x erbij ('2x', '2 x', 'x2'), zodat '2 liter melk' heel blijft."""
    t = re.sub(r"\([^)]*\)", " ", tekst or "").strip().lower()
    aantal = 1
    m = re.match(r"^(\d+)\s*[x×]\s+(.*)$", t) or re.match(r"^(.*?)\s+(?:(\d+)\s*[x×]|[x×]\s*(\d+))$", t)
    if m:
        g = [x for x in m.groups() if x is not None]
        if g[0].isdigit():
            aantal, t = int(g[0]), g[1]
        else:
            t, aantal = g[0], int(g[1])
    t = re.sub(r"[^\w\s&'-]", " ", t)
    return " ".join(t.split()), max(1, min(aantal, 20))


def keuzes() -> dict:
    try:
        return json.loads(_keuzes_pad().read_text()).get("keuzes", {})
    except (OSError, ValueError):
        return {}


def _schrijf_keuzes(k: dict) -> None:
    pad = _keuzes_pad()
    pad.parent.mkdir(parents=True, exist_ok=True)
    tmp = pad.with_suffix(".tmp")
    tmp.write_text(json.dumps({"keuzes": k}, ensure_ascii=False, indent=1))
    tmp.replace(pad)


def kies(woord: str, product_id: str, naam: str, eenheid: str = "") -> dict:
    sleutel, _ = term(woord)
    if not sleutel or not re.match(r"^[\w-]{3,40}$", product_id or ""):
        raise ValueError("geef een boodschap en een Picnic-product-id (zoals s1018231)")
    k = keuzes()
    k[sleutel] = {"id": product_id, "naam": (naam or "").strip()[:120],
                  "eenheid": (eenheid or "").strip()[:40], "sinds": date.today().isoformat()}
    _schrijf_keuzes(k)
    return k[sleutel]


def vergeet_keuze(woord: str) -> bool:
    sleutel, _ = term(woord)
    k = keuzes()
    if sleutel not in k:
        return False
    del k[sleutel]
    _schrijf_keuzes(k)
    return True


# ── mandje ───────────────────────────────────────────────────────────────

def _euro(cent: int | None) -> str:
    return "" if cent is None else f"€ {cent / 100:.2f}".replace(".", ",")


def _mandje_regels(cart) -> list[dict]:
    regels = []
    for line in cart.items or []:
        for art in line.items or []:
            aantal = next((d.quantity for d in art.decorators if d.type == "QUANTITY" and d.quantity), 1)
            regels.append({"id": art.id or "", "naam": art.name or "", "aantal": aantal,
                           "eenheid": art.unit_quantity or "",
                           "prijs": line.display_price if line.display_price is not None else line.price})
    return regels


def _tijdvak(start: str | None, eind: str | None) -> str:
    try:
        s, e = datetime.fromisoformat(start), datetime.fromisoformat(eind)
    except (TypeError, ValueError):
        return ""
    dagen = ["ma", "di", "wo", "do", "vr", "za", "zo"]
    return f"{dagen[s.weekday()]} {s.day}-{s.month} {s:%H:%M}–{e:%H:%M}"


def overzicht() -> dict:
    """Mandje, totaal, gekozen bezorgmoment en lopende bezorging, voor dashboard en agent."""
    def haal(api):
        cart = api.get_cart()
        gekozen = ""
        sel = cart.selected_slot.slot_id if cart.selected_slot else None
        for s in cart.delivery_slots or []:
            if sel and s.slot_id == sel:
                gekozen = _tijdvak(s.window_start, s.window_end)
        bezorging = ""
        try:
            for d in api.get_current_deliveries():
                if d.slot:
                    bezorging = _tijdvak(d.slot.window_start, d.slot.window_end)
                    break
        except Exception:  # noqa: BLE001 - bezorging is bijzaak
            pass
        return {"mandje": _mandje_regels(cart), "aantal": cart.total_count or 0,
                "totaal": cart.total_price, "totaal_tekst": _euro(cart.total_price),
                "bezorgmoment": gekozen, "bezorging": bezorging}
    return _met_herlogin(haal)


def zoek(woord: str, max_n: int = 8) -> list[dict]:
    res = _met_herlogin(lambda api: api.search(woord))
    out = []
    for it in res.items:
        pid = it.id or it.sole_article_id  # selling-unit-id, zoals add_product hem verwacht
        if not pid or not it.name:
            continue
        out.append({"id": pid, "naam": it.name, "eenheid": it.unit_quantity or "",
                    "prijs": it.display_price})
        if len(out) >= max_n:
            break
    return out


def voeg(product_id: str, aantal: int = 1) -> None:
    _met_herlogin(lambda api: api.add_product(product_id, count=aantal))


def haal_weg(product_id: str, aantal: int = 1) -> None:
    _met_herlogin(lambda api: api.remove_product(product_id, count=aantal))


def overzetten(lijst=None, afvinken=None, dry_run: bool = False) -> dict:
    """Boodschappen met een vaste keuze in het mandje zetten en afvinken in Todoist.
    Geeft {gedaan: [...], onbekend: [...], mislukt: [...]} terug."""
    lijst = lijst or (lambda: todoist.lijst("boodschappen"))
    afvinken = afvinken or todoist.afvinken
    k = keuzes()
    gedaan, onbekend, mislukt = [], [], []
    for item in lijst():
        sleutel, aantal = term(item["tekst"])
        keuze = k.get(sleutel)
        if not keuze:
            onbekend.append({"tekst": item["tekst"], "term": sleutel, "aantal": aantal, "todoist_id": item["id"]})
            continue
        if dry_run:
            gedaan.append({"tekst": item["tekst"], "product": keuze["naam"], "aantal": aantal})
            continue
        try:
            voeg(keuze["id"], aantal)
        except Exception as e:  # noqa: BLE001 - per item doorgaan
            mislukt.append({"tekst": item["tekst"], "fout": f"{type(e).__name__}: {e}"[:160]})
            continue
        afvinken(item["id"])
        gedaan.append({"tekst": item["tekst"], "product": keuze["naam"], "aantal": aantal})
    return {"gedaan": gedaan, "onbekend": onbekend, "mislukt": mislukt}


# ── CLI ──────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Picnic-mandje voor Birdy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("koppel")
    o = sub.add_parser("overzetten")
    o.add_argument("--proef", action="store_true", help="alleen laten zien, niets toevoegen")
    z = sub.add_parser("zoek"); z.add_argument("woord")
    k = sub.add_parser("kies"); k.add_argument("woord"); k.add_argument("product_id")
    k.add_argument("--naam", required=True); k.add_argument("--eenheid", default="")
    v = sub.add_parser("voeg"); v.add_argument("product_id"); v.add_argument("--aantal", type=int, default=1)
    h = sub.add_parser("haal-weg"); h.add_argument("product_id"); h.add_argument("--aantal", type=int, default=1)
    sub.add_parser("mandje")
    sub.add_parser("keuzes")
    vk = sub.add_parser("vergeet-keuze"); vk.add_argument("woord")
    args = ap.parse_args()
    try:
        if args.cmd == "koppel":
            koppel()
        elif args.cmd == "overzetten":
            r = overzetten(dry_run=args.proef)
            for g in r["gedaan"]:
                print(f"✓ {g['aantal']}× {g['product']}  (voor: {g['tekst']})")
            for m in r["mislukt"]:
                print(f"✗ {m['tekst']}: {m['fout']}")
            if r["onbekend"]:
                print("Nog geen vaste keuze (zoek, kies, voeg toe en vink af in Todoist):")
                for u in r["onbekend"]:
                    print(f"? {u['tekst']}  → term '{u['term']}', aantal {u['aantal']}, todoist-id {u['todoist_id']}")
            if not any(r.values()):
                print("De boodschappenlijst is leeg.")
        elif args.cmd == "zoek":
            for p in zoek(args.woord):
                print(f"{p['id']} | {p['naam']} | {p['eenheid']} | {_euro(p['prijs'])}")
        elif args.cmd == "kies":
            k_ = kies(args.woord, args.product_id, args.naam, args.eenheid)
            print(f"Vaste keuze voor '{term(args.woord)[0]}': {k_['naam']} {k_['eenheid']} ({k_['id']})")
        elif args.cmd == "voeg":
            voeg(args.product_id, args.aantal); print("In het mandje gezet.")
        elif args.cmd == "haal-weg":
            haal_weg(args.product_id, args.aantal); print("Uit het mandje gehaald.")
        elif args.cmd == "mandje":
            o_ = overzicht()
            for r_ in o_["mandje"]:
                print(f"{r_['aantal']}× {r_['naam']} {r_['eenheid']} | {_euro(r_['prijs'])} | {r_['id']}")
            print(f"Totaal: {o_['totaal_tekst'] or '€ 0,00'} ({o_['aantal']} artikelen)")
            if o_["bezorgmoment"]:
                print(f"Gekozen bezorgmoment: {o_['bezorgmoment']}")
            if o_["bezorging"]:
                print(f"Lopende bezorging: {o_['bezorging']}")
        elif args.cmd == "keuzes":
            k_ = keuzes()
            print("\n".join(f"{t}: {v_['naam']} {v_['eenheid']} ({v_['id']})" for t, v_ in sorted(k_.items()))
                  or "(nog geen vaste keuzes)")
        elif args.cmd == "vergeet-keuze":
            print("Vergeten." if vergeet_keuze(args.woord) else "Geen vaste keuze met die naam.")
    except PicnicNietGekoppeld as e:
        sys.exit(str(e) + " Meld dit kort in de chat in plaats van het opnieuw te proberen.")
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
