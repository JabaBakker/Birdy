"""Kiosk-dashboard voor de muurtablet (en telefoons).

Draait als aiohttp-server in de agent-container, alleen op localhost van de VPS;
ontsluiting gebeurt via Tailscale (tailscale serve → HTTPS, nodig voor de microfoon).
Aan/uit via DASHBOARD_TOKEN in .env: leeg = dashboard uit.

- GET  /                → kioskpagina (dark, auto-verversend, microfoonknop)
- GET  /api/overview    → agenda, overzicht, lijstjes, verjaardagen (JSON, cache 2 min)
- POST /api/message     → {"text": ...} → zelfde brein als de chat; antwoord terug + echo in Slack
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime

from aiohttp import web

from .brain import Brain
from .config import Config

log = logging.getLogger("fien.dashboard")

CACHE_TTL = 120  # seconden


def _agenda() -> list[dict]:
    from . import gcal

    rows: list[tuple[str, str]] = []
    for fn in (gcal._google_events, gcal._familywall_events):
        try:
            rows.extend(fn(7))
        except BaseException:  # SystemExit van de CLI-helpers telt ook
            pass
    seen, out = set(), []
    for when, line in sorted(rows):
        norm = line.replace(" (FamilyWall)", "")
        if norm in seen:
            continue
        seen.add(norm)
        titel = line.split(" | ", 1)[1] if " | " in line else line
        out.append({"wanneer": when, "titel": titel.replace(" (FamilyWall)", "")})
    return out[:22]


def _overzicht_kort(text: str) -> dict:
    """OVERZICHT.md (chat-format) samenvatten: urgente en deze-week-items + rest-telling."""
    secties: dict[str, list[str]] = {"🔴": [], "🟠": [], "🟡": [], "⏳": []}
    huidig = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        kop = next((k for k in secties if s.startswith(k)), None)
        if kop:
            huidig = kop
            continue
        if s[0] in "🔴🟠🟡⏳✅📋":  # andere kop (bijv. ✅) → sectie sluiten
            huidig = None
            continue
        if huidig and s.startswith(("•", "-", "*")):
            secties[huidig].append(s.lstrip("•-* ").strip())
    rest = []
    if secties["🟡"]:
        rest.append(f"{len(secties['🟡'])} voor later")
    if secties["⏳"]:
        rest.append(f"{len(secties['⏳'])} wachten op")
    return {"urgent": secties["🔴"][:5], "week": secties["🟠"][:5], "rest": " · ".join(rest)}


def _todoist_lijst(naam: str) -> list[str]:
    from . import todoist

    try:
        project = todoist._project(naam)
        tasks = todoist._list_all("/tasks", {"project_id": project["id"]})
        return [t["content"] for t in tasks][:12]
    except BaseException:
        return []


def _verjaardagen() -> list[dict]:
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
        naam = clean[5:].split("·")[0].strip(" ·—-")
        out.append({"datum": clean[:5], "naam": naam, "dagen": (volgende - vandaag).days})
    return sorted(out, key=lambda x: x["dagen"])[:5]


class Dashboard:
    def __init__(self, cfg: Config, brain: Brain, work_lock: asyncio.Lock, adapters: list):
        self.cfg = cfg
        self.brain = brain
        self.work_lock = work_lock
        self.adapters = adapters
        self._cache: dict | None = None
        self._cache_ts = 0.0
        self._runner: web.AppRunner | None = None

    # -- levenscyclus -------------------------------------------------------

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self.page)
        app.router.add_get("/api/overview", self.overview)
        app.router.add_post("/api/message", self.message)
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
        return web.Response(text=PAGE, content_type="text/html")

    async def overview(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "geen toegang"}, status=401)
        if not self._cache or time.time() - self._cache_ts > CACHE_TTL:
            overzicht_pad = self.cfg.workspace / "OVERZICHT.md"
            agenda, boodschappen, acties, jarigen = await asyncio.gather(
                asyncio.to_thread(_agenda),
                asyncio.to_thread(_todoist_lijst, "boodschappen"),
                asyncio.to_thread(_todoist_lijst, "acties"),
                asyncio.to_thread(_verjaardagen),
            )
            overzicht = overzicht_pad.read_text() if overzicht_pad.exists() else ""
            self._cache = {
                "agenda": agenda,
                "taken": _overzicht_kort(overzicht),
                "boodschappen": boodschappen,
                "acties": acties,
                "verjaardagen": jarigen,
            }
            self._cache_ts = time.time()
        data = dict(self._cache)
        data["nu"] = datetime.now().strftime("%A %d %B · %H:%M")
        return web.json_response(data)

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
        self._cache = None  # lijstjes kunnen net veranderd zijn
        for adapter in self.adapters:
            if adapter is not self:
                try:
                    await adapter.broadcast(f"🎤 Dashboard: “{text}”\n{reply}", kind="chat")
                except Exception:
                    pass
        return web.json_response({"reply": reply})


PAGE = """<!doctype html>
<html lang="nl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Birdy</title>
<style>
  :root { --bg:#14171a; --panel:#1d2126; --ink:#e8e6df; --dim:#8b948f; --accent:#7fbfa6;
          --amber:#d9a44e; --rood:#e07a6a; --lijn:#2a2f35; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--ink); font-family:system-ui,-apple-system,sans-serif;
         padding:1.2rem; min-height:100vh; }
  header { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:.9rem; }
  header h1 { font-size:1.4rem; } header h1 span { color:var(--accent); }
  #klok { color:var(--dim); font-size:1.05rem; text-transform:capitalize; }
  #tekstinvoer { display:flex; gap:.5rem; margin-bottom:1rem; }
  #tekstinvoer input { flex:1; background:var(--panel); border:1px solid #333a41; border-radius:12px;
                       color:var(--ink); padding:.85rem 1rem; font-size:1.05rem; }
  #tekstinvoer button { border:none; border-radius:12px; padding:0 1.2rem; font-size:1.3rem;
                        cursor:pointer; }
  #stuurknop { background:var(--accent); color:#14171a; }
  #mic { background:var(--panel); border:1px solid #333a41 !important; }
  #mic.luistert { background:var(--amber); animation:pulse 1.2s infinite; }
  @keyframes pulse { 50% { transform:scale(1.06); } }
  .grid { display:grid; gap:1rem; grid-template-columns:repeat(auto-fit,minmax(290px,1fr)); }
  .panel { background:var(--panel); border-radius:14px; padding:1rem 1.1rem; }
  .panel h2 { font-size:.8rem; letter-spacing:.1em; text-transform:uppercase; color:var(--accent);
              margin-bottom:.6rem; }
  .panel ul { list-style:none; } .panel li { padding:.26rem 0; font-size:1.02rem; line-height:1.35; }
  .panel li small { color:var(--dim); margin-right:.55rem; font-variant-numeric:tabular-nums;
                    display:inline-block; min-width:3.2rem; }
  li.dag { font-size:.78rem; letter-spacing:.08em; text-transform:uppercase; color:var(--amber);
           border-top:1px solid var(--lijn); margin-top:.5rem; padding-top:.55rem; }
  li.dag:first-child { border-top:none; margin-top:0; padding-top:0; }
  li.urgent::before { content:"● "; color:var(--rood); }
  li.week::before { content:"● "; color:var(--amber); }
  .rest { color:var(--dim); font-size:.9rem; margin-top:.5rem; }
  .leeg { color:var(--dim); font-style:italic; }
  #jarig li b { color:var(--amber); }
  #antwoord { position:fixed; right:1.2rem; bottom:1.2rem; max-width:min(440px,85vw);
              background:var(--panel); border:1px solid var(--accent); border-radius:14px;
              padding:.8rem 1rem; font-size:1rem; display:none; box-shadow:0 4px 20px rgba(0,0,0,.4); }
  #sleutel { display:none; padding:2rem; text-align:center; }
  #sleutel input { font-size:1.1rem; padding:.6rem; border-radius:8px; border:1px solid #444; }
</style></head><body>
<div id="sleutel"><p>Vul de dashboard-sleutel in (staat in de .env op de server):</p><br>
  <input id="sleutelveld" placeholder="sleutel"> <button onclick="zetSleutel()">Opslaan</button></div>
<div id="app" style="display:none">
  <header><h1>🐦 <span>Birdy</span></h1><div id="klok"></div></header>
  <div id="tekstinvoer">
    <input id="invoer" placeholder="Zeg of typ iets tegen Birdy — “voeg kwark toe aan de boodschappen”">
    <button id="mic" title="Praat tegen Birdy">🎤</button>
    <button id="stuurknop" onclick="stuur(document.getElementById('invoer').value)">→</button>
  </div>
  <div class="grid">
    <div class="panel"><h2>📅 Agenda</h2><ul id="agenda"></ul></div>
    <div class="panel"><h2>📋 Wat loopt er</h2><ul id="taken"></ul><div class="rest" id="takenrest"></div></div>
    <div class="panel"><h2>🛒 Boodschappen</h2><ul id="boodschappen"></ul></div>
    <div class="panel"><h2>⚡ Acties</h2><ul id="acties"></ul></div>
    <div class="panel" id="jarig"><h2>🎂 Verjaardagen</h2><ul id="verjaardagen"></ul></div>
  </div>
</div>
<div id="antwoord"></div>
<script>
let KEY = null;
try { KEY = localStorage.getItem('birdy-key'); } catch (e) {}
const q = new URLSearchParams(location.search).get('key');
if (q) { KEY = q; try { localStorage.setItem('birdy-key', q); } catch (e) {} }
function zetSleutel(){ KEY = document.getElementById('sleutelveld').value.trim();
  try { localStorage.setItem('birdy-key', KEY); } catch(e){} ververs(); }

function vul(id, items, maak){ const el = document.getElementById(id);
  el.innerHTML = items.length ? items.map(maak).join('') : '<li class="leeg">niets 🎉</li>'; }

function dagLabel(d){
  const dt = new Date(d + 'T00:00'); const nu = new Date(); nu.setHours(0,0,0,0);
  const diff = Math.round((dt - nu) / 86400000);
  if (diff === 0) return 'Vandaag'; if (diff === 1) return 'Morgen';
  return dt.toLocaleDateString('nl-NL', { weekday:'long', day:'numeric', month:'short' });
}
function agendaHtml(items){
  if (!items.length) return '<li class="leeg">niets gepland 🎉</li>';
  const groepen = {};
  items.forEach(e => { const d = e.wanneer.slice(0,10); (groepen[d] = groepen[d]||[]).push(e); });
  return Object.keys(groepen).sort().map(d =>
    `<li class="dag">${dagLabel(d)}</li>` +
    groepen[d].map(e => `<li><small>${e.wanneer.length>10 ? e.wanneer.slice(11) : 'hele dag'}</small>${e.titel}</li>`).join('')
  ).join('');
}

async function ververs(){
  try {
    const r = await fetch('/api/overview', { headers: { 'X-Dashboard-Key': KEY } });
    if (r.status === 401) { toonSleutel(); return; }
    const d = await r.json();
    document.getElementById('sleutel').style.display = 'none';
    document.getElementById('app').style.display = 'block';
    document.getElementById('klok').textContent = d.nu;
    document.getElementById('agenda').innerHTML = agendaHtml(d.agenda);
    const t = d.taken || {urgent:[],week:[],rest:''};
    const rows = t.urgent.map(x => `<li class="urgent">${x}</li>`)
      .concat(t.week.map(x => `<li class="week">${x}</li>`));
    document.getElementById('taken').innerHTML =
      rows.length ? rows.join('') : '<li class="leeg">niets dringends 🎉</li>';
    document.getElementById('takenrest').textContent = t.rest ? 'verder: ' + t.rest : '';
    vul('boodschappen', d.boodschappen, x => `<li>• ${x}</li>`);
    vul('acties', d.acties, x => `<li>• ${x}</li>`);
    vul('verjaardagen', d.verjaardagen,
        j => `<li><small>${j.datum}</small>${j.naam} <b>${j.dagen===0?'vandaag! 🎉':'over '+j.dagen+' dgn'}</b></li>`);
  } catch (e) { /* volgende poging over 60s */ }
}
function toonSleutel(){ document.getElementById('app').style.display='none';
  document.getElementById('sleutel').style.display='block'; }
ververs(); setInterval(ververs, 60000);

async function stuur(tekst){
  tekst = (tekst||'').trim(); if (!tekst) return;
  document.getElementById('invoer').value = '';
  toon('🐦 …denkt na…');
  try {
    const r = await fetch('/api/message', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ text: tekst }) });
    const d = await r.json();
    toon('🐦 ' + (d.reply || d.error || 'er ging iets mis'));
    ververs();
  } catch(e){ toon('🐦 Birdy is even niet bereikbaar.'); }
}
function toon(t){ const a = document.getElementById('antwoord');
  a.textContent = t; a.style.display = 'block';
  clearTimeout(a._t); a._t = setTimeout(() => a.style.display='none', 30000); }

const mic = document.getElementById('mic');
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null, bezig = false;
mic.onclick = () => {
  if (!SR) {
    document.getElementById('invoer').focus();
    toon('🎤 Spraak werkt in Chrome of Safari — hier kun je wel gewoon typen.');
    return;
  }
  if (bezig) { rec.stop(); return; }
  rec = new SR(); rec.lang = 'nl-NL'; rec.interimResults = false;
  rec.onstart = () => { bezig = true; mic.classList.add('luistert'); };
  rec.onend = () => { bezig = false; mic.classList.remove('luistert'); };
  rec.onerror = () => toon('🎤 Ik kon je niet verstaan — probeer nog eens.');
  rec.onresult = ev => { const t = ev.results[0][0].transcript; toon('🎤 “' + t + '”'); stuur(t); };
  rec.start();
};
document.getElementById('invoer').addEventListener('keydown',
  e => { if (e.key === 'Enter') stuur(e.target.value); });
</script></body></html>"""
