"""Kiosk-dashboard voor de muurtablet (en telefoons).

Draait als aiohttp-server in de agent-container, alleen op localhost van de VPS;
ontsluiting gebeurt via Tailscale (tailscale serve → HTTPS, nodig voor de microfoon).
Aan/uit via DASHBOARD_TOKEN in .env: leeg = dashboard uit.

- GET  /                → kioskpagina (dark, auto-verversend, microfoonknop)
- GET  /api/overview    → agenda, onderwerpen (Doc "Wat loopt er"), aandacht (regels +
                          Birdy's briefingpunten uit AANDACHT.md), lijstjes, verjaardagen,
                          regelzaken, thuis (JSON, cache 2 min)
- POST /api/message     → {"text": ...} → zelfde brein als de chat; antwoord terug + echo in Slack
- GET/POST /api/plan    → gedeelde kinderplanning (workspace/memory/planning.json), zodat de
                          timer op tablet én telefoon dezelfde is
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from aiohttp import web

from . import agenda, bronnen, financien, signalen, todoist
from .brain import Brain
from .config import Config

STATIC_DIR = Path(__file__).parent / "static"  # dashboard.html/.css/.js + logo's + pwa
STATIC_BESTANDEN = ("dashboard.css", "dashboard.js", "manifest.webmanifest", "sw.js",
                    "icon-192.png", "icon-512.png")
PLAN_MAX_BYTES = 60_000  # gedeelde kinderplanning (planning.json) blijft klein


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


log = logging.getLogger("fien.dashboard")

CACHE_TTL = 120  # seconden


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
        self._geld: dict | None = None  # financieel register (Sheet), eigen cache van 5 min
        self._geld_ts = 0.0
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
        for naam in STATIC_BESTANDEN:
            app.router.add_get(f"/{naam}", self.static)
        app.router.add_get("/api/geld", self.geld)
        app.router.add_get("/api/verreken", self.verreken_get)
        app.router.add_post("/api/verreken", self.verreken_post)
        app.router.add_get("/api/besparingen", self.besparingen_get)
        app.router.add_post("/api/besparingen", self.besparingen_post)
        app.router.add_post("/api/upload", self.upload)
        app.router.add_get("/api/plan", self.plan_get)
        app.router.add_post("/api/plan", self.plan_post)
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
        if naam not in STATIC_BESTANDEN:
            raise web.HTTPNotFound()
        # css/js hebben een ?v=hash en mogen lang gecachet worden; manifest/sw/icons niet
        cache = "public, max-age=31536000" if naam.startswith("dashboard.") else "no-cache"
        return web.FileResponse(STATIC_DIR / naam, headers={"Cache-Control": cache})

    async def logo(self, request: web.Request) -> web.Response:
        naam = "logo-bird.png" if request.path.endswith("logo-bird.png") else "logo.png"
        pad = STATIC_DIR / naam
        if not pad.exists():
            raise web.HTTPNotFound()  # de pagina valt dan terug op het vogel-emoji
        return web.Response(body=pad.read_bytes(), content_type="image/png",
                            headers={"Cache-Control": "max-age=86400"})

    # -- geld: financieel register achter een pincode ------------------------

    async def _geld_lees(self) -> dict:
        if self._geld is None or time.time() - self._geld_ts > 300:
            try:
                self._geld = await asyncio.to_thread(financien.register)
            except BaseException:
                log.warning("financieel register lezen mislukt", exc_info=True)
                self._geld = self._geld or {"beschikbaar": False, "link": "", "woordenlijst": financien.WOORDENLIJST}
            self._geld_ts = time.time()
        return self._geld

    async def geld(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        if not self.cfg.dashboard_geld_pin:
            return web.json_response({"error": "geen pincode ingesteld (DASHBOARD_GELD_PIN)"}, status=403)
        if request.headers.get("X-Geld-Pin", "") != self.cfg.dashboard_geld_pin:
            await asyncio.sleep(0.8)  # raden ontmoedigen
            return web.json_response({"error": "pincode klopt niet"}, status=403)
        if request.query.get("ververs") == "1":
            self._geld = None
        data = await self._geld_lees()
        return web.json_response(data, headers={"Cache-Control": "no-store"})

    # -- verrekenen: losse posten die eigenlijk van de pot (gezamenlijk) waren ------

    def _verreken_pad(self) -> Path:
        return self.cfg.workspace / "memory" / "verrekenen.json"

    def _verreken_lees(self) -> dict:
        try:
            d = json.loads(self._verreken_pad().read_text())
            if isinstance(d, dict):
                return {"posten": list(d.get("posten") or []), "afrekeningen": list(d.get("afrekeningen") or [])}
        except (OSError, ValueError):
            pass
        return {"posten": [], "afrekeningen": []}

    def _verreken_schrijf(self, d: dict) -> None:
        pad = self._verreken_pad()
        pad.parent.mkdir(parents=True, exist_ok=True)
        tmp = pad.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
        tmp.replace(pad)

    def _geld_ok(self, request: web.Request) -> web.Response | None:
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        if not self.cfg.dashboard_geld_pin or request.headers.get("X-Geld-Pin", "") != self.cfg.dashboard_geld_pin:
            return web.json_response({"error": "pincode klopt niet"}, status=403)
        return None

    async def verreken_get(self, request: web.Request) -> web.Response:
        fout = self._geld_ok(request)
        if fout:
            return fout
        return web.json_response(self._verreken_lees(), headers={"Cache-Control": "no-store"})

    async def verreken_post(self, request: web.Request) -> web.Response:
        """{actie: 'toevoegen', wie, bedrag, omschrijving, richting: 'voor_pot'|'uit_pot', datum?}
        {actie: 'verwijderen', id} · {actie: 'afrekenen'} → alle open posten afsluiten."""
        fout = self._geld_ok(request)
        if fout:
            return fout
        try:
            body = await request.json()
            assert isinstance(body, dict)
        except Exception:
            return web.json_response({"error": "geen geldige json"}, status=400)
        d = self._verreken_lees()
        actie = str(body.get("actie", "")).strip()
        if actie == "toevoegen":
            wie = str(body.get("wie", "")).strip()[:30]
            oms = str(body.get("omschrijving", "")).strip()[:120]
            richting = str(body.get("richting", "voor_pot"))
            try:
                bedrag = round(float(str(body.get("bedrag", "")).replace(",", ".")), 2)
            except ValueError:
                bedrag = 0.0
            if not wie or bedrag <= 0 or richting not in ("voor_pot", "uit_pot"):
                return web.json_response({"error": "wie, bedrag (> 0) en richting zijn nodig"}, status=400)
            datum = str(body.get("datum") or date.today().isoformat())[:10]
            post = {"id": f"{int(time.time() * 1000)}", "datum": datum, "wie": wie, "bedrag": bedrag,
                    "omschrijving": oms, "richting": richting, "verrekend": ""}
            d["posten"].append(post)
            self._verreken_schrijf(d)
            return web.json_response({"ok": True, "post": post})
        if actie == "verwijderen":
            pid = str(body.get("id", ""))
            voor = len(d["posten"])
            d["posten"] = [p for p in d["posten"] if not (p["id"] == pid and not p.get("verrekend"))]
            self._verreken_schrijf(d)
            return web.json_response({"ok": len(d["posten"]) < voor})
        if actie == "afrekenen":
            open_ = [p for p in d["posten"] if not p.get("verrekend")]
            if not open_:
                return web.json_response({"error": "niets te verrekenen"}, status=400)
            saldo: dict[str, float] = {}
            for p in open_:
                saldo[p["wie"]] = round(saldo.get(p["wie"], 0) + (p["bedrag"] if p["richting"] == "voor_pot" else -p["bedrag"]), 2)
            vandaag = date.today().isoformat()
            regels = [f"pot → {w}: € {v:,.2f}" if v > 0 else f"{w} → pot: € {-v:,.2f}" for w, v in saldo.items() if abs(v) >= 0.005]
            afrekening = {"datum": vandaag, "aantal": len(open_), "saldo": saldo, "tekst": "; ".join(regels) or "in balans",
                          "van": min(p["datum"] for p in open_), "tot": max(p["datum"] for p in open_)}
            for p in open_:
                p["verrekend"] = vandaag
            d["afrekeningen"].insert(0, afrekening)
            d["afrekeningen"] = d["afrekeningen"][:24]
            self._verreken_schrijf(d)
            return web.json_response({"ok": True, "afrekening": afrekening})
        return web.json_response({"error": "onbekende actie"}, status=400)

    # -- besparingsvoorstellen die via het dashboard zijn toegevoegd (naast de Sheet) ----

    def _besparingen_pad(self) -> Path:
        return self.cfg.workspace / "memory" / "besparingen.json"

    def _besparingen_lees(self) -> list[dict]:
        try:
            d = json.loads(self._besparingen_pad().read_text())
            return list(d) if isinstance(d, list) else []
        except (OSError, ValueError):
            return []

    async def besparingen_get(self, request: web.Request) -> web.Response:
        fout = self._geld_ok(request)
        if fout:
            return fout
        return web.json_response({"items": self._besparingen_lees()}, headers={"Cache-Control": "no-store"})

    async def besparingen_post(self, request: web.Request) -> web.Response:
        """{actie:'toevoegen', voorstel, per_maand?, categorie?, notitie?, bron?} ·
        {actie:'status', id, status} · {actie:'verwijderen', id}"""
        fout = self._geld_ok(request)
        if fout:
            return fout
        try:
            body = await request.json()
            assert isinstance(body, dict)
        except Exception:
            return web.json_response({"error": "geen geldige json"}, status=400)
        items = self._besparingen_lees()
        actie = str(body.get("actie", ""))
        if actie == "toevoegen":
            voorstel = str(body.get("voorstel", "")).strip()[:160]
            if not voorstel:
                return web.json_response({"error": "voorstel ontbreekt"}, status=400)
            try:
                pm = round(float(str(body.get("per_maand", 0) or 0).replace(",", ".")), 2)
            except ValueError:
                pm = 0.0
            item = {"id": f"{int(time.time() * 1000)}", "voorstel": voorstel, "per_maand": pm,
                    "categorie": str(body.get("categorie", "")).strip()[:40], "status": "idee",
                    "bron": str(body.get("bron", "dashboard")).strip()[:40], "datum": date.today().isoformat(),
                    "notitie": str(body.get("notitie", "")).strip()[:300], "uit": "dashboard"}
            items.append(item)
        elif actie == "status":
            for it in items:
                if it["id"] == str(body.get("id", "")):
                    it["status"] = str(body.get("status", "idee")).strip().lower()[:20]
        elif actie == "verwijderen":
            items = [it for it in items if it["id"] != str(body.get("id", ""))]
        else:
            return web.json_response({"error": "onbekende actie"}, status=400)
        pad = self._besparingen_pad()
        pad.parent.mkdir(parents=True, exist_ok=True)
        tmp = pad.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1))
        tmp.replace(pad)
        return web.json_response({"ok": True, "items": items})

    # -- document uploaden vanaf het dashboard (bijv. een polis) → Birdy archiveert ------

    async def upload(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        reader = await request.multipart()
        bestand, naam, hint = None, "", ""
        while True:
            deel = await reader.next()
            if deel is None:
                break
            if deel.name == "hint":
                hint = (await deel.text())[:300]
            elif deel.name == "bestand":
                naam = Path(deel.filename or "document").name[:120]
                data = bytearray()
                while True:
                    chunk = await deel.read_chunk(65536)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > 15 * 1024 * 1024:
                        return web.json_response({"error": "bestand groter dan 15 MB"}, status=413)
                bestand = bytes(data)
        if not bestand or not naam:
            return web.json_response({"error": "geen bestand"}, status=400)
        map_ = self.cfg.workspace / "inbox" / "docs"
        map_.mkdir(parents=True, exist_ok=True)
        veilig = "".join(c if c.isalnum() or c in "._-" else "_" for c in naam)
        pad = map_ / f"{datetime.now():%Y%m%d-%H%M%S}_{veilig}"
        pad.write_bytes(bestand)
        rel = str(pad.relative_to(self.cfg.workspace))
        tekst = (f"Via het dashboard is een document geüpload: {rel}."
                 + (f" Toelichting: {hint}." if hint else "")
                 + " Lees het, archiveer het op de juiste plek in Drive (financiële stukken in "
                   "30 Financiën/<map>), en geef als het om een polis, contract of abonnement gaat de "
                   "kant-en-klare regel voor het register terug (tabblad + kolommen).")
        async with self.work_lock:
            reply = await self.brain.run("process_message.md", "dashboard-upload",
                                         sender="het dashboard", text=tekst, photo=rel)
        reply = reply or "Hmm, daar ging iets mis bij het verwerken van het document."
        for adapter in self.adapters:
            if adapter is not self:
                try:
                    await adapter.broadcast(f"📎 Document via het dashboard ({veilig})\n{reply}", kind="chat")
                except Exception:
                    log.exception("echo van upload mislukt")
        self._invalidate()
        return web.json_response({"ok": True, "reply": reply, "pad": rel})

    # -- gedeelde kinderplanning (zelfde timer op tablet én telefoon) -------

    def _plan_pad(self) -> Path:
        return self.cfg.workspace / "memory" / "planning.json"

    def _plan_lees(self) -> dict:
        try:
            data = json.loads(self._plan_pad().read_text())
            if isinstance(data, dict):
                return {"plan": data.get("plan"), "taken": data.get("taken") or {},
                        "bijgewerkt": int(data.get("bijgewerkt") or 0)}
        except (OSError, ValueError):
            pass
        return {"plan": None, "taken": {}, "bijgewerkt": 0}

    async def plan_get(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        return web.json_response(self._plan_lees(), headers={"Cache-Control": "no-store"})

    async def plan_post(self, request: web.Request) -> web.Response:
        """{plan?: {...}, taken?: {...}} → samenvoegen met wat er staat; laatste schrijver wint."""
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        try:
            body = await request.json()
            assert isinstance(body, dict)
        except Exception:
            return web.json_response({"error": "geen geldige json"}, status=400)
        huidig = self._plan_lees()
        if "plan" in body:
            if body["plan"] is not None and not isinstance(body["plan"], dict):
                return web.json_response({"error": "plan moet een object zijn"}, status=400)
            huidig["plan"] = body["plan"]
        if "taken" in body:
            if not isinstance(body["taken"], dict):
                return web.json_response({"error": "taken moet een object zijn"}, status=400)
            huidig["taken"] = body["taken"]
        huidig["bijgewerkt"] = int(time.time() * 1000)
        tekst = json.dumps(huidig, ensure_ascii=False)
        if len(tekst.encode()) > PLAN_MAX_BYTES:
            return web.json_response({"error": "planning te groot"}, status=413)
        pad = self._plan_pad()
        try:
            pad.parent.mkdir(parents=True, exist_ok=True)
            tmp = pad.with_suffix(".tmp")
            tmp.write_text(tekst)
            tmp.replace(pad)
        except OSError:
            log.warning("planning opslaan mislukt", exc_info=True)
            return web.json_response({"error": "opslaan mislukt"}, status=500)
        return web.json_response({"ok": True, "bijgewerkt": huidig["bijgewerkt"]})

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
                asyncio.to_thread(agenda.rijk),
                asyncio.to_thread(bronnen.verjaardagen),
                asyncio.to_thread(bronnen.regelzaken),
                asyncio.to_thread(bronnen.thuis),
                asyncio.to_thread(bronnen.onderwerpen),
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
            asyncio.to_thread(todoist.lijst, "boodschappen"),
            asyncio.to_thread(todoist.lijst, "acties"),
            asyncio.to_thread(todoist.afgevinkt, "boodschappen"),
            asyncio.to_thread(todoist.afgevinkt, "acties"),
        )
        onderwerpen = self._traag.get("onderwerpen", [])
        vers = {
            "agenda": agenda.compact(self._traag["week"]),
            "week": self._traag["week"],
            "personen": self.cfg.dashboard_personen,
            "onderwerpen": onderwerpen,
            "aandacht": {
                "birdy": bronnen.aandacht_birdy(self.cfg.workspace),
                "signalen": signalen.bereken(acties, self._traag.get("regelzaken", []),
                                      self._traag["verjaardagen"], self._traag["week"], onderwerpen)
                            + (((await self._geld_lees()).get("signalen", []))[:3] if self.cfg.dashboard_geld_pin else []),
            },
            "geld_tab": bool(self.cfg.dashboard_geld_pin),
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
        return await self._taak_actie(request, todoist.afvinken, self._patch_afgevinkt)

    async def reopen(self, request: web.Request) -> web.Response:
        return await self._taak_actie(request, todoist.heropen, self._patch_heropend)

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
        ok = await asyncio.to_thread(todoist.deadline, task_id, datum)
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
                agenda.bereik, datetime.combine(van, datetime.min.time()),
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
            ok = await asyncio.to_thread(agenda.verwijder, event_id)
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
            nieuw = await asyncio.to_thread(agenda.nieuw, velden)
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
        ok = await asyncio.to_thread(agenda.bewerk, event_id, velden)
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
        ok = await asyncio.to_thread(agenda.verzet, event_id, start, eind)
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
        thuis = await asyncio.to_thread(bronnen.thuis)
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
        taak = await asyncio.to_thread(todoist.toevoegen, lijst, tekst, datum)
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
