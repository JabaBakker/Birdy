let KEY = null;
try { KEY = localStorage.getItem('birdy-key'); } catch (e) {}
const q = new URLSearchParams(location.search).get('key');
if (q) { KEY = q; try { localStorage.setItem('birdy-key', q); } catch (e) {} }
function zetSleutel(){ KEY = document.getElementById('sleutelveld').value.trim();
  try { localStorage.setItem('birdy-key', KEY); } catch(e){} ververs(); }

function meetKop(){
  // hoogte van kopregel + paginaranden → de Vandaag-tab vult precies de rest van het scherm
  const h = document.querySelector('header');
  if (h) document.documentElement.style.setProperty('--kop', (h.offsetHeight + 52) + 'px');
}
window.addEventListener('resize', meetKop);
function kiesTab(t){
  meetKop();
  document.getElementById('paneelVandaag').style.display = t === 'vandaag' ? 'grid' : 'none';
  document.getElementById('paneelWeek').style.display = t === 'week' ? 'block' : 'none';
  document.getElementById('paneelPlan').style.display = t === 'plan' ? 'block' : 'none';
  document.getElementById('paneelGeld').style.display = t === 'geld' ? 'block' : 'none';
  document.getElementById('tabVandaag').classList.toggle('actief', t === 'vandaag');
  document.getElementById('tabWeek').classList.toggle('actief', t === 'week');
  document.getElementById('tabPlan').classList.toggle('actief', t === 'plan');
  document.getElementById('tabGeld').classList.toggle('actief', t === 'geld');
  try { localStorage.setItem('birdy-tab', t === 'geld' ? 'vandaag' : t); } catch(e){}  // geld nooit als starttab
  if (t === 'plan') renderPlan();
  if (t === 'geld') renderGeld();
}

function vul(id, items, maak){ const el = document.getElementById(id);
  el.innerHTML = items.length ? items.map(maak).join('') : '<li class="leeg">niets 🎉</li>'; }
function vulMeer(id, items, maak, max, l2){
  const el = document.getElementById(id);
  let html = items.length ? items.slice(0, max).map(maak).join('')
                          : '<li class="leeg">niets 🎉</li>';
  if (items.length > max)
    html += `<li class="leeg klik" onclick="openL2('${l2}')">… nog ${items.length - max} — alles ↗</li>`;
  el.innerHTML = html;
}
function esc(s){ const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function taakRij(x){
  const p = persoonMatch(x.tekst);
  // tekst links; rechts vast: initiaal van de persoon, datum-pil of '+' om een datum te prikken
  return `<li class="vink"${p ? ` style="--pc:${p.kleur}"` : ''} onclick="vink(this,'${x.id}')">` +
    `<span>${esc(x.tekst)}</span>` +
    (p ? `<i class="init" style="background:${p.kleur}" title="${esc(p.naam)}">${esc(p.naam[0])}</i>` : '') +
    (x.due ? `<span style="flex:0 0 auto;align-self:center">${dueBadge(x.due).replace('margin-left:.4rem', '')}</span>`
           : `<button class="duebtn" title="deadline prikken"` +
      ` onclick="event.stopPropagation();kiesDatum('${x.id}')">+</button>`) + `</li>`;
}
function actiesGroepen(items, max){
  // Nu (over datum of vandaag) · Binnenkort (binnen 7 dagen) · Later (rest of zonder datum)
  const nu = new Date(); nu.setHours(0,0,0,0);
  const dag = d => Math.round((new Date(d + 'T00:00') - nu) / 86400000);
  const g = { nu: [], binnenkort: [], later: [] };
  items.forEach(x => {
    if (!x.due) g.later.push(x);
    else { const d = dag(x.due); (d <= 0 ? g.nu : d <= 7 ? g.binnenkort : g.later).push(x); }
  });
  let html = '', n = 0, over = 0;
  [['nu', 'Nu'], ['binnenkort', 'Binnenkort'], ['later', 'Later']].forEach(([k, label]) => {
    if (!g[k].length) return;
    const ruimte = Math.max(0, max - n);
    const toon = g[k].slice(0, ruimte);
    over += g[k].length - toon.length;
    if (!toon.length) return;
    html += `<div class="groep">${label} <b>${g[k].length}</b></div><ul>` + toon.map(taakRij).join('') + '</ul>';
    n += toon.length;
  });
  if (!items.length) html = '<ul><li class="leeg">niets open 🎉</li></ul>';
  else if (over > 0) html += `<ul><li class="leeg klik" onclick="openL2('acties')">… nog ${over} — alles ↗</li></ul>`;
  return html;
}
function actiesRing(items, afgevinkt){
  // per week: afgevinkt in de laatste 7 dagen tegenover wat deze week op de rol staat
  // (over datum, vandaag of binnen 7 dagen); acties zonder datum of verder weg tellen niet mee
  const nu = new Date(); nu.setHours(0,0,0,0);
  const dezeWeek = items.filter(x => x.due && Math.round((new Date(x.due + 'T00:00') - nu) / 86400000) <= 7).length;
  const af = afgevinkt.length, totaal = dezeWeek + af, pct = totaal ? af / totaal : 0;
  const omtrek = 2 * Math.PI * 25;
  document.getElementById('ringVol').style.strokeDasharray = `${pct * omtrek} ${omtrek}`;
  document.getElementById('ringGetal').textContent = af;
  document.getElementById('ringTekst').innerHTML = `<b>${af} van ${totaal} deze week</b><small>` +
    (totaal === 0 ? 'niets op de rol 🎉' : af === 0 ? 'nog niets afgevinkt' : pct >= 1 ? 'alles af! 🎉'
     : pct >= .5 ? 'goed bezig! 💪' : `nog ${dezeWeek} te doen`) + '</small>';
}
function ico(titel, bron){
  const t = (titel + ' ' + (bron || '')).toLowerCase();
  const map = [[/jarig|verjaardag/, '🎂'], [/feest/, '🎈'], [/zwem/, '🏊'], [/hardlo|wandel|fiets/, '🏃'],
    [/volleybal|spirit|training|wedstrijd|sport/, '🏐'], [/school|kijkavond|ouderavond|studiedag/, '🏫'],
    [/tandarts|dokter|huisarts|ortho|fysio|dierenarts/, '🩺'], [/kapper/, '💇'], [/oppas/, '🧸'],
    [/bezoek|visite|oma|opa|familie/, '👥'], [/opruim|schoonma|klus/, '🧹'], [/eten|diner|bbq|borrel|lunch/, '🍽️'],
    [/vakantie|reis|weekend weg/, '✈️'], [/vergader|werk|overleg/, '💼'], [/muziek|les/, '🎵']];
  for (const [re, e] of map) if (re.test(t)) return e;
  return '•';
}
function aandachtKaart(s, i){
  // regel-signaal als kaartje: kopje op bron, tekst, en één knop als er iets te doen valt
  const kop = { acties: 'Acties', regelzaken: 'Regelzaak', verjaardagen: 'Verjaardag', onderwerpen: 'Onderwerp',
                week: 'Agenda' }[s.l2] || 'Signaal';
  const kl = s.ernst === 0 ? 'var(--rood)' : 'var(--amber)';
  const open = s.l2 === 'week' ? "kiesTab('week')" : `openL2('${s.l2}')`;
  let knop = '';
  if (s.knop) knop = `<button class="aknop" onclick="event.stopPropagation();snelActie(${JSON.stringify(s.knop.tekst)},${JSON.stringify(s.knop.datum || '')},this)">＋ ${esc(s.knop.label)}</button>`;
  else if (s.l2 === 'week') knop = `<button class="aknop" onclick="event.stopPropagation();kiesTab('week')">Agenda bekijken</button>`;
  return `<div class="akaart" style="--kl:${kl}" onclick="${open}"><div class="akop">${kop}</div>` +
    `<div class="atekst">${esc(s.tekst)}</div>${knop}</div>`;
}
function birdyKaart(tekst, tijd){
  return `<div class="akaart" style="--kl:var(--amber)" onclick="openL2('aandacht')">` +
    `<div class="akop"><img src="/logo-bird.png" class="bird" onerror="this.replaceWith('🐦')"> Birdy${tijd ? `<small>${esc(tijd)}</small>` : ''}</div>` +
    `<div class="atekst">${esc(tekst)}</div></div>`;
}
async function snelActie(tekst, datum, knop){
  if (knop){ knop.disabled = true; knop.textContent = '… toevoegen'; }
  try {
    const r = await fetch('/api/add', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ lijst: 'acties', tekst, datum }) });
    if (!r.ok) throw new Error();
    toon(`⚡ “${tekst}” staat op de actielijst`); await ververs();
  } catch(e){ toon('Toevoegen lukte even niet — probeer nog eens.'); if (knop){ knop.disabled = false; knop.textContent = 'Opnieuw'; } }
}
function afRij(x){
  return `<li class="afitem"><span>${esc(x.tekst)}</span>` +
    `<button class="herstelknop" title="terugzetten" onclick="herstel('${x.id}')">↩</button></li>`;
}
function jarigRij(j){
  return `<li><small>${j.datum}</small><span>${esc(j.naam)} ` +
    `<b>${j.dagen===0 ? 'vandaag! 🎉' : 'over ' + j.dagen + ' dgn'}</b></span></li>`;
}

// ── verdiepende pagina's (L2) ─────────────────────────────────────────────
let DATA = null, L2open = null, L2filter = 'alle';
function openL2(naam){ L2open = naam; L2filter = 'alle';
  document.getElementById('l2').style.display = 'flex'; renderL2(); }
function sluitL2(){ L2open = null; document.getElementById('l2').style.display = 'none'; }
function zetFilter(f){ L2filter = f; renderL2(); }
function filterChips(){
  const namen = ['alle', ...PERSONEN, 'overig'];
  return `<div class="fchips">` + namen.map(n =>
    `<button class="fchip${L2filter === n ? ' actief' : ''}"` +
    ` onclick="zetFilter('${n}')">${esc(n)}</button>`).join('') + `</div>`;
}
function filterItems(items, tekstVan){
  if (L2filter === 'alle') return items;
  return items.filter(x => {
    const p = persoonMatch(tekstVan(x));
    return L2filter === 'overig' ? !p : (p && p.naam === L2filter);
  });
}
function kw(w){ return (Math.round((w || 0) / 100) / 10).toFixed(1).replace('.', ',') + ' kW'; }
// P1-meter (net_w): + = afnemen van het net, − = terugleveren. Huisverbruik = zon + net.
function energie(th){
  const zon = th.zon_w, net = th.net_w;
  return {
    zon,
    huis: (zon !== null && net !== null) ? Math.max(0, zon + net) : (zon === null ? net : null),
    terug: net !== null && net < 0 ? -net : 0,
    vanNet: net !== null && net > 0 ? net : 0,
  };
}
function nettoLabel(th){
  if (th.net_w === null) return '';
  const e = energie(th);
  return e.terug > 0
    ? `<small class="nu">↑ ${kw(e.terug)}</small>`
    : `<small>↓ ${kw(e.vanNet)}</small>`;
}
async function lampUit(id){
  try {
    const r = await fetch('/api/homey/lamp', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ id, aan: false }) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'fout');
    toon('💡 Uit gezet'); ververs();
  } catch(e){ toon('Lamp schakelen lukte niet: ' + e.message); }
}
function signaalRij(s){
  const actie = s.l2 === 'week' ? "kiesTab('week')" : `openL2('${s.l2}')`;
  return `<li class="signaal ernst${s.ernst}" onclick="${actie}"><span>${esc(s.tekst)}</span></li>`;
}
function dagenLabel(d){
  if (d === null || d === undefined) return '';
  if (d < 0) return 'te laat';
  if (d === 0) return 'vandaag';
  if (d === 1) return 'morgen';
  return 'over ' + d + ' d';
}
async function regelzaakGedaan(i){
  const z = (DATA.regelzaken || [])[i]; if (!z) return;
  const tekst = `We hebben net "${z.naam}" gedaan. Werk het huishoudhandboek bij: laatst = vandaag,` +
    ` volgende = vandaag + het interval. Bevestig kort.`;
  chatVoeg('ik', `✓ ${z.naam} gedaan`);
  toon(`✓ Doorgegeven aan Birdy: "${z.naam}" is gedaan — handboek wordt bijgewerkt…`);
  try {
    const r = await fetch('/api/message', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ text: tekst }) });
    const d = await r.json();
    chatVoeg('birdy', d.reply || d.error || 'er ging iets mis');
    toon('🐦 ' + (d.reply || d.error || 'er ging iets mis'));
    ververs();
  } catch(e){ toon('Doorgeven lukte even niet — probeer nog eens.'); }
}
function taakRijL2(x){
  const p = persoonMatch(x.tekst);
  return `<li class="vink"${p ? ` style="--pc:${p.kleur}"` : ''} onclick="vink(this,'${x.id}')">` +
    `<span>${esc(x.tekst)}${dueBadge(x.due)}` +
    (x.notitie ? `<br><span class="notitie">${esc(x.notitie)}</span>` : '') + `</span>` +
    `<button class="vraagknop" title="vraag Birdy hierover"` +
    ` onclick="event.stopPropagation();vraagOver('${L2open}','${x.id}')">💬</button>` +
    (x.due ? '' : `<button class="duebtn" title="deadline prikken"` +
      ` onclick="event.stopPropagation();kiesDatum('${x.id}')">+</button>`) + `</li>`;
}
let TV = null;  // open taak-dialoog: {lijst, id}
function vraagOver(lijst, id){
  const x = (DATA[lijst] || []).find(t => t.id === id); if (!x) return;
  TV = { lijst, id };
  document.getElementById('tvTitel').textContent = '💬 ' + x.tekst;
  const n = document.getElementById('tvNotitie');
  n.textContent = x.notitie || ''; n.style.display = x.notitie ? 'block' : 'none';
  const a = document.getElementById('tvAntwoord');
  a.textContent = ''; a.style.display = 'none';
  document.getElementById('tvVeld').value = '';
  document.getElementById('taakvraag').style.display = 'flex';
  document.getElementById('tvVeld').focus();
}
function sluitTaakVraag(){ TV = null; document.getElementById('taakvraag').style.display = 'none'; }
async function tvStuur(vraag){
  vraag = (vraag || '').trim(); if (!vraag || !TV) return;
  const x = (DATA[TV.lijst] || []).find(t => t.id === TV.id); if (!x) return;
  document.getElementById('tvVeld').value = '';
  const a = document.getElementById('tvAntwoord');
  a.style.display = 'block'; a.textContent = '🐦 …denkt na…';
  const soort = TV.lijst === 'acties' ? 'actie' : 'boodschap';
  try {
    const r = await fetch('/api/message', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ text: `Over de ${soort} "${x.tekst}": ${vraag}` }) });
    const d = await r.json();
    a.textContent = '🐦 ' + (d.reply || d.error || 'er ging iets mis');
    await ververs();  // vernieuwde notitie ophalen (L2 eronder ververst mee)
    if (TV){
      const nx = (DATA[TV.lijst] || []).find(t => t.id === TV.id);
      const n = document.getElementById('tvNotitie');
      if (nx && nx.notitie){ n.textContent = nx.notitie; n.style.display = 'block'; }
    }
  } catch(e){ a.textContent = '🐦 Even niet bereikbaar — probeer het zo nog eens.'; }
}
function renderL2(){
  if (!L2open || !DATA) return;
  document.getElementById('l2Titel').textContent = {
    onderwerpen: '📂 Onderwerpen — wat loopt er', aandacht: '💡 Aandacht',
    zoek: '🔍 Zoeken in de agenda',
    boodschappen: '🛒 Boodschappen',
    acties: '⚡ Acties — alles', verjaardagen: '🎂 Verjaardagen & cadeau-ideeën',
    regelzaken: '🔁 Regelzaken — huishoudhandboek', thuis: '🏠 Thuis — via Homey',
  }[L2open];
  let html = '';
  if (L2open === 'thuis'){
    const th = DATA.thuis;
    if (!th){ document.getElementById('l2Inhoud').innerHTML = '<p class="leeg">Homey is even niet bereikbaar.</p>'; return; }
    html += '<h4>⚡ Energie</h4><ul>';
    const e = energie(th);
    if (e.zon !== null) html += `<li><span>☀️ Zonnepanelen leveren</span><small class="waarde">${kw(e.zon)}</small></li>`;
    if (e.huis !== null) html += `<li><span>🏠 Huis verbruikt</span><small class="waarde">${kw(e.huis)}</small></li>`;
    if (th.net_w !== null){
      html += e.terug > 0
        ? `<li><span>↑ Teruglevering aan het net</span><small class="waarde">${kw(e.terug)}</small></li>`
        : `<li><span>↓ Afname van het net</span><small class="waarde">${kw(e.vanNet)}</small></li>`;
    }
    html += '</ul>';
    if ((th.klimaat || []).length){
      html += '<h4>🌡️ Klimaat</h4><ul>' + th.klimaat.map(k =>
        `<li><span>${esc(k.kamer)}</span><small class="waarde">${k.temp}°${k.doel !== null && k.doel !== undefined ? ' · doel ' + k.doel + '°' : ''}</small></li>`).join('') + '</ul>';
    }
    const lampen = th.lampen_aan || [];
    html += `<h4>💡 Lampen aan (${lampen.length})</h4><ul>` + (lampen.length ? lampen.map(l =>
      `<li><span>${esc(l.naam)}${l.kamer ? ` <span class="notitie">· ${esc(l.kamer)}</span>` : ''}</span>` +
      `<button class="herstelknop" onclick="lampUit('${l.id}')">uit</button></li>`).join('')
      : '<li class="leeg">alles uit 🌙</li>') + '</ul>';
    const app = [];
    if (th.auto) app.push(`<li><span>🚗 ${esc(th.auto.naam)}</span><small class="waarde">${th.auto.batterij ?? '?'}%${th.auto.laadt ? ' · laadt ⚡' : ''}</small></li>`);
    if (th.deur) app.push(`<li><span>🚪 ${esc(th.deur.naam)}</span><small class="waarde">${th.deur.dicht === true ? '🔒 op slot' : th.deur.dicht === false ? '🔓 open' : 'onbekend'}</small></li>`);
    if (th.stofzuiger) app.push(`<li><span>🧹 ${esc(th.stofzuiger.naam)}</span><small class="waarde">${th.stofzuiger.batterij ?? '?'}%</small></li>`);
    if (th.tv_aan !== null && th.tv_aan !== undefined) app.push(`<li><span>📺 TV</span><small class="waarde">${th.tv_aan ? 'aan' : 'uit'}</small></li>`);
    if (app.length) html += '<h4>🔌 Apparaten</h4><ul>' + app.join('') + '</ul>';
    html += `<p class="notitie" style="margin-top:.8rem">${th.aantal} apparaten via Homey Pro</p>`;
    document.getElementById('l2Inhoud').innerHTML = html; return;
  }
  if (L2open === 'regelzaken'){
    const items = DATA.regelzaken || [];
    html = '<ul>' + (items.length ? items.map((z, i) =>
      `<li><span>${esc(z.naam)}${z.wie ? persChip(z.wie) : ''}` +
      `<span class="due ${z.dagen !== null && z.dagen < 0 ? 'laat' : z.dagen === 0 ? 'nu' : 'straks'}">` +
      `${dagenLabel(z.dagen) || 'geen datum'}</span><br>` +
      `<span class="notitie">${esc([z.elke ? 'elke ' + z.elke : '', z.laatst ? 'laatst ' + z.laatst : '',
        z.volgende ? 'volgende ' + z.volgende : ''].filter(Boolean).join(' · '))}</span></span>` +
      `<button class="herstelknop" title="net gedaan — Birdy werkt het handboek bij"` +
      ` onclick="regelzaakGedaan(${i})">✓ gedaan</button></li>`).join('')
      : '<li class="leeg">Nog geen regelzaken — zeg tegen Birdy: "de grijze bak gaat elke 2 weken aan straat".</li>') + '</ul>';
    document.getElementById('l2Inhoud').innerHTML = html; return;
  }
  if (L2open === 'onderwerpen'){
    const items = filterItems(DATA.onderwerpen || [], o => o.naam + ' ' + o.wie);
    html = filterChips() + '<ul>' + (items.length ? items.map(o =>
      `<li><span>${esc(o.naam)}${o.wie ? persChip(o.wie) : ''}` +
      (o.dagen !== null ? `<span class="due ${o.dagen < 0 ? 'laat' : o.dagen <= 1 ? 'nu' : 'straks'}">` +
        `${esc(o.wanneer)}${o.dagen === 0 ? ' · vandaag' : o.dagen === 1 ? ' · morgen' : o.dagen < 0 ? ' · te laat' : ''}</span>`
        : (o.wanneer ? `<span class="due straks">${esc(o.wanneer)}</span>` : '')) +
      (o.stap ? `<br><span class="notitie">→ ${esc(o.stap)}</span>` : '') +
      (o.notitie ? `<br><span class="notitie">${esc(o.notitie)}</span>` : '') +
      `</span></li>`).join('')
      : '<li class="leeg">niets voor dit filter</li>') + '</ul>';
    html += `<p class="notitie" style="margin-top:.8rem">Uit het Google Doc “Wat loopt er” in de Drive-map — Birdy houdt het bij; zelf aanpassen mag ook.</p>`;
  } else if (L2open === 'zoek'){
    const z = ZOEK;
    html = `<p class="notitie">“${esc(z.term)}” · 3 maanden terug t/m 18 maanden vooruit</p>`;
    if (z.items === null) html += '<p class="leeg">zoeken…</p>';
    else if (z.fout) html += '<p class="leeg">zoeken lukte even niet</p>';
    else if (!z.items.length) html += '<p class="leeg">niets gevonden</p>';
    else {
      const vandaag = isoDag(weekStart(0));
      html += '<ul>' + z.items.map((e, i) => {
        const dag = e.start.slice(0,10), voorbij = dag < vandaag;
        const tijd = e.start.length > 10 ? e.start.slice(11,16) + ((e.eind && e.eind.length > 10) ? '–' + e.eind.slice(11,16) : '') : 'hele dag';
        const k = kleurVoor(e.titel, e.bron);
        return `<li class="signaal" style="${voorbij ? 'opacity:.55' : ''}" onclick='gaNaar(ZOEK.items[${i}])'>` +
          `<small style="flex:0 0 7.5rem">${esc(new Date(dag + 'T00:00').toLocaleDateString('nl-NL', { weekday:'short', day:'numeric', month:'short', year: dag.slice(0,4) !== vandaag.slice(0,4) ? 'numeric' : undefined }))}</small>` +
          `<span><b style="color:${k}">${esc(e.titel)}</b> <span class="notitie">· ${tijd}${e.locatie ? ' · ' + esc(e.locatie) : ''}${e.bron && !e.bron.startsWith('Gezinsagenda') ? ' · ' + esc(e.bron) : ''}</span></span></li>`;
      }).join('') + '</ul>';
    }
  } else if (L2open === 'aandacht'){
    const a = DATA.aandacht || { birdy: { items: [] }, signalen: [] };
    const b = a.birdy || { items: [] };
    html = `<h4><img src="/logo-bird.png" class="bird" onerror="this.replaceWith('🐦')"> Wat Birdy opviel` +
      (b.tijd && b.items.length ? ` <span class="notitie">· ${esc(b.tijd)}</span>` : '') + '</h4><ul>' +
      (b.items.length ? b.items.map(x => `<li class="birdy"><img src="/logo-bird.png" class="bird" onerror="this.replaceWith('🐦')"><span>${esc(x)}</span></li>`).join('')
        : `<li class="leeg">${b.oud ? 'de vorige punten zijn ouder dan drie dagen' : 'nog niets'} — vraag Birdy hieronder om een verse blik, of wacht op de ochtendupdate van 07:15</li>`) + '</ul>';
    html += `<button class="blikknop" onclick="sluitL2(); stuur('Werk je aandachtspunten bij (AANDACHT.md): kijk over agenda, acties, onderwerpen en handboek heen en geef me de drie punten die nu het meest aandacht verdienen.')">🐦 Vraag Birdy om een verse blik</button>`;
    html += '<h4>Signalen uit agenda, acties en handboek</h4>' +
      ((a.signalen || []).length ? a.signalen.map(aandachtKaart).join('') : '<p class="leeg">niets dat aandacht vraagt 🙂</p>');
  } else if (L2open === 'boodschappen' || L2open === 'acties'){
    const items = filterItems(DATA[L2open] || [], x => x.tekst);
    html = filterChips() + '<ul>' +
      (items.length ? items.map(taakRijL2).join('') : '<li class="leeg">niets voor dit filter</li>') +
      '</ul>';
    html += `<div class="toevoeg"><input placeholder="+ toevoegen…" enterkeyhint="done"
      onkeydown="voegToe(event,'${L2open}',this)"></div>`;
    const af = filterItems(DATA[L2open + '_af'] || [], x => x.tekst);
    if (af.length) html += '<h4>↩ Onlangs afgevinkt</h4><ul>' + af.map(afRij).join('') + '</ul>';
  } else if (L2open === 'verjaardagen'){
    const items = DATA.verjaardagen || [];
    html = '<ul>' + (items.length ? items.map(j =>
      `<li><small>${j.datum}</small><span>${esc(j.naam)} ` +
      `<b>${j.dagen===0 ? 'vandaag! 🎉' : 'over ' + j.dagen + ' dgn'}</b>` +
      (j.notitie ? `<br><span class="notitie">${esc(j.notitie)}</span>` : '') +
      `</span></li>`).join('') : '<li class="leeg">nog geen verjaardagen — zeg ze tegen Birdy!</li>') + '</ul>';
  }
  document.getElementById('l2Inhoud').innerHTML = html;
}

function dagLabel(d){
  const dt = new Date(d + 'T00:00'); const nu = new Date(); nu.setHours(0,0,0,0);
  const diff = Math.round((dt - nu) / 86400000);
  if (diff === 0) return 'Vandaag'; if (diff === 1) return 'Morgen';
  return dt.toLocaleDateString('nl-NL', { weekday:'long', day:'numeric', month:'short' });
}
function agendaHtml(items, max){
  // tijdlijn: dag-kop met bolletje, per afspraak tijd + pil met icoontje; max regels zodat het past
  if (!items.length) return '<li class="leeg">niets gepland 🎉</li>';
  max = max || 14;
  const groepen = {};
  items.forEach(e => { const d = e.start.slice(0,10); (groepen[d] = groepen[d]||[]).push(e); });
  let html = '', n = 0, over = 0;
  Object.keys(groepen).sort().forEach(d => {
    if (n >= max){ over += groepen[d].length; return; }
    const dt = new Date(d + 'T00:00');
    const kort = dt.toLocaleDateString('nl-NL', { weekday:'short', day:'numeric', month:'short' });
    const lbl = dagLabel(d);
    html += `<li class="dag">${lbl.startsWith('Vandaag') || lbl.startsWith('Morgen') ? lbl : lbl.split(' ')[0].replace(/^./, c => c.toUpperCase())}<small>· ${kort}</small></li>`;
    groepen[d].forEach(e => {
      if (n >= max){ over++; return; }
      const tijd = e.start.length > 10 ? e.start.slice(11,16) : '<span style="opacity:.7">dag</span>';
      const k = kleurVoor(e.titel, e.bron);
      html += `<li class="ev" onclick="detailVanVandaag(${e._i})"><small>${tijd}</small>` +
        `<div class="evpil" style="border-left:2px solid ${k}"><em>${ico(e.titel, e.bron)}</em><span>${esc(e.titel)}</span></div></li>`;
      n++;
    });
  });
  if (over > 0) html += `<li class="leeg klik" onclick="kiesTab('week')">… nog ${over} deze week — weekoverzicht ↗</li>`;
  return html;
}
function detailVanVandaag(i){ if (WEEK[i]) detailEv(i); }
function dueBadge(d){
  if (!d) return '';
  const dt = new Date(d + 'T00:00'); const nu = new Date(); nu.setHours(0,0,0,0);
  const diff = Math.round((dt - nu) / 86400000);
  if (diff < 0)  return `<span class="due laat">te laat</span>`;
  if (diff === 0) return `<span class="due nu">vandaag</span>`;
  if (diff === 1) return `<span class="due straks">morgen</span>`;
  const label = diff < 7
    ? dt.toLocaleDateString('nl-NL', { weekday:'short' })
    : dt.toLocaleDateString('nl-NL', { day:'numeric', month:'short' });
  return `<span class="due straks">${label}</span>`;
}

// ── weekweergave ──────────────────────────────────────────────────────────
const KLEUREN = ['#7fbfa6', '#d9a44e', '#e07a6a', '#8ab4d8', '#b39ddb', '#f2a1c2'];
let PERSONEN = [], WEEK = [];
function persoonMatch(tekst){
  const t = tekst.toLowerCase();
  for (let i = 0; i < PERSONEN.length; i++)
    if (t.includes(PERSONEN[i].toLowerCase()))
      return { naam: PERSONEN[i], kleur: KLEUREN[i % KLEUREN.length] };
  return null;
}
function kleurVoor(titel, bron){
  // een persoonsnaam in de titel of in het bronlabel ("Volleybal Yvette") bepaalt de kleur
  const p = persoonMatch(titel) || (bron && !bron.startsWith('Gezinsagenda') ? persoonMatch(bron) : null);
  return p ? p.kleur : '#5b6570';
}
function persChip(tekst){
  const p = persoonMatch(tekst);
  return p ? ` <span class="pers" style="background:${p.kleur}22;color:${p.kleur}">${esc(p.naam)}</span>` : '';
}
const U0 = 7, U1 = 22, HOOG = 510, PPU = HOOG / (U1 - U0);
function minuten(iso){ return parseInt(iso.slice(11,13),10)*60 + parseInt(iso.slice(14,16),10); }
// ── weken bladeren + zoeken (buiten de komende 7 dagen via /api/agenda) ──
let WEEKOFFSET = 0;  // 0 = de lopende week (vanaf vandaag), ±n = n weken verder/terug
function weekStart(offset){
  const s = new Date(); s.setHours(0,0,0,0); s.setDate(s.getDate() + 7 * (offset ?? WEEKOFFSET));
  return s;
}
function isoDag(d){ return d.toLocaleDateString('sv-SE'); }
async function weekStap(richting){
  WEEKOFFSET = richting === 0 ? 0 : WEEKOFFSET + richting;
  await weekLaad();
}
async function weekLaad(){
  if (WEEKOFFSET === 0){ WEEK = (DATA && DATA.week) || []; renderWeek(WEEK); return; }
  const van = weekStart(), tot = new Date(van); tot.setDate(tot.getDate() + 7);
  document.getElementById('wkLabel').textContent = 'laden…';
  try {
    const r = await fetch(`/api/agenda?van=${isoDag(van)}&tot=${isoDag(tot)}`, { headers: { 'X-Dashboard-Key': KEY } });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'fout');
    WEEK = d.events || []; renderWeek(WEEK);
  } catch(e){ toon('Agenda ophalen lukte even niet.'); renderWeek(WEEK); }
}
async function zoekAgenda(term){
  term = (term || '').trim(); if (term.length < 2) return;
  openL2('zoek'); ZOEK = { term, items: null };
  renderL2();
  try {
    const r = await fetch(`/api/agenda?zoek=${encodeURIComponent(term)}`, { headers: { 'X-Dashboard-Key': KEY } });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'fout');
    ZOEK = { term, items: d.events || [] };
  } catch(e){ ZOEK = { term, items: [], fout: true }; }
  renderL2();
}
let ZOEK = { term: '', items: null };
async function gaNaar(ev){
  // spring naar de week van deze afspraak en open de detailkaart
  const dag = new Date(ev.start.slice(0,10) + 'T00:00'); const nu = weekStart(0);
  WEEKOFFSET = Math.floor((dag - nu) / (7 * 86400000));
  sluitL2(); kiesTab('week');
  await weekLaad();
  const i = WEEK.findIndex(e => e.start === ev.start && e.titel === ev.titel);
  if (i >= 0) detailEv(i);
}
function renderWeek(events){
  const grid = document.getElementById('wkgrid');
  const delen = bouwWeek(events, weekStart());
  grid.innerHTML = delen;
  const van = weekStart(), tot = new Date(van); tot.setDate(tot.getDate() + 6);
  const f = d => d.toLocaleDateString('nl-NL', { day:'numeric', month:'short' });
  document.getElementById('wkLabel').textContent =
    (WEEKOFFSET === 0 ? 'komende 7 dagen' : `${f(van)} – ${f(tot)}`) +
    (WEEKOFFSET ? ` · ${WEEKOFFSET > 0 ? '+' : ''}${WEEKOFFSET} ${Math.abs(WEEKOFFSET) === 1 ? 'week' : 'weken'}` : '');
  const leg = document.getElementById('legenda');
  leg.innerHTML = PERSONEN.map((p,i) =>
    `<span><i style="background:${KLEUREN[i%KLEUREN.length]}"></i>${esc(p)}</span>`).join('') +
    `<span><i style="background:#5b6570"></i>overig</span><span>⚠ rode rand = overlap</span>`;
}
function bouwWeek(events, start){
  const dagen = {}; start = start || weekStart(0);
  const vandaagKey = isoDag(weekStart(0));
  const volgorde = [];
  for (let i = 0; i < 7; i++){
    const d = new Date(start); d.setDate(d.getDate() + i);
    const key = d.toLocaleDateString('sv-SE');
    volgorde.push(key); dagen[key] = { datum:d, heledag:[], tijd:[] };
  }
  events.forEach((e, i) => {
    e._i = i;
    const d = e.start.slice(0,10); if (!(d in dagen)) return;
    (e.start.length <= 10 ? dagen[d].heledag : dagen[d].tijd).push(e);
  });
  let html = '<div></div>';
  volgorde.forEach((key, i) => {
    const g = dagen[key];
    const isVandaag = key === vandaagKey;
    const label = isVandaag ? 'Vandaag'
      : g.datum.toLocaleDateString('nl-NL', { weekday:'short', day:'numeric', month: i === 0 || g.datum.getDate() === 1 ? 'short' : undefined });
    html += `<div class="kop${isVandaag?' vandaag':''}">${label}</div>`;
  });
  html += '<div></div>';
  volgorde.forEach(key => {
    const g = dagen[key];
    html += `<div class="heledag">` + g.heledag.map(e => {
      const k = kleurVoor(e.titel, e.bron);
      return `<div class="chip" style="border-color:${k};background:${k}22"` +
        ` onclick="detailEv(${e._i})">${esc(e.titel)}</div>`;
    }).join('') + `</div>`;
  });
  let uuras = '<div class="uuras">';
  for (let u = U0; u <= U1; u += 2)
    uuras += `<div style="top:${(u-U0)*PPU}px">${String(u).padStart(2,'0')}</div>`;
  html += uuras + '</div>';
  volgorde.forEach(key => {
    const g = dagen[key];
    const t = g.tijd.map(e => ({ ev: e, s: minuten(e.start),
      e: (e.eind && e.eind.length > 10) ? Math.max(minuten(e.eind), minuten(e.start) + 30)
                                        : minuten(e.start) + 60 }));
    t.sort((a,b) => a.s - b.s || b.e - a.e);
    // kolomtoewijzing zoals een echte kalender: overlappers netjes naast elkaar
    const actief = [];
    t.forEach(x => {
      for (let i = actief.length - 1; i >= 0; i--) if (actief[i].e <= x.s) actief.splice(i, 1);
      const bezet = new Set(actief.map(a => a.col));
      x.col = 0; while (bezet.has(x.col)) x.col++;
      actief.push(x);
    });
    let ci = 0;  // clusters van aaneengesloten overlap → gedeelde kolombreedte
    while (ci < t.length){
      let cj = ci, eind = t[ci].e;
      while (cj + 1 < t.length && t[cj + 1].s < eind){ cj++; eind = Math.max(eind, t[cj].e); }
      const groep = t.slice(ci, cj + 1);
      const cols = Math.max(...groep.map(y => y.col)) + 1;
      groep.forEach(y => { y.cols = cols; });
      ci = cj + 1;
    }
    let vak = '<div class="tijdvak">';
    for (let u = U0; u <= U1; u += 2) vak += `<div class="uurlijn" style="top:${(u-U0)*PPU}px"></div>`;
    t.forEach(x => {
      const top = Math.max(0, (x.s/60 - U0) * PPU);
      const hoogte = Math.max(24, Math.min(HOOG - top - 2, (x.e - x.s) / 60 * PPU - 2));
      const k = kleurVoor(x.ev.titel, x.ev.bron);
      const breedte = 100 / x.cols;
      const tijd = x.ev.start.slice(11,16) +
        ((x.ev.eind && x.ev.eind.length > 10) ? '–' + x.ev.eind.slice(11,16) : '');
      const sleepbaar = !!x.ev.id;
      vak += `<div class="blok${x.cols > 1 ? ' conflict' : ''}${sleepbaar ? ' sleepbaar' : ''}"` +
        ` style="top:${top}px;height:${hoogte}px;border-color:${k};background:${k}26;` +
        `left:calc(${x.col * breedte}% + 3px);width:calc(${breedte}% - 6px);color:var(--ink)"` +
        ` onclick="if(!onderdruktKlik)detailEv(${x.ev._i})"` +
        (sleepbaar ? ` onpointerdown="blokDown(event,${x.ev._i})"` +
          ` onpointermove="blokMove(event)" onpointerup="blokUp(event)"` +
          ` onpointercancel="blokUp(event)"` : '') + `>` +
        `<b>${esc(x.ev.titel)}</b><small>${tijd}</small></div>`;
    });
    html += vak + '</div>';
  });
  return html;
}
// ── afspraken verslepen (alleen Google; FamilyWall is alleen-lezen) ──
let sleepData = null, onderdruktKlik = false;
function blokDown(ev, i){
  sleepData = { i, el: ev.currentTarget, x0: ev.clientX, y0: ev.clientY, dx: 0, dy: 0, bezig: false };
  try { ev.currentTarget.setPointerCapture(ev.pointerId); } catch(e){}
}
function blokMove(ev){
  if (!sleepData) return;
  sleepData.dx = ev.clientX - sleepData.x0;
  sleepData.dy = ev.clientY - sleepData.y0;
  if (!sleepData.bezig && Math.abs(sleepData.dx) + Math.abs(sleepData.dy) > 8){
    sleepData.bezig = true;
    sleepData.el.classList.add('sleept');
  }
  if (sleepData.bezig)
    sleepData.el.style.transform = `translate(${sleepData.dx}px, ${sleepData.dy}px)`;
}
function blokUp(ev){
  if (!sleepData) return;
  const s = sleepData; sleepData = null;
  s.el.style.transform = ''; s.el.classList.remove('sleept');
  if (!s.bezig) return;  // gewone tik → onclick opent de detailkaart
  onderdruktKlik = true; setTimeout(() => { onderdruktKlik = false; }, 300);
  const kolomBreedte = s.el.parentElement.getBoundingClientRect().width + 6;
  const dagen = Math.round(s.dx / kolomBreedte);
  const minuten = Math.round((s.dy / PPU) * 60 / 15) * 15;  // per kwartier
  if (dagen === 0 && minuten === 0) return;
  const e = WEEK[s.i];
  const oudS = e.start, oudE = e.eind;
  verzetNaar(s.i, schuifIso(e.start, dagen, minuten),
             (e.eind && e.eind.length > 10) ? schuifIso(e.eind, dagen, minuten) : '',
             () => verzetNaar(s.i, oudS, oudE, null));
}
function schuifIso(iso, dagen, minuten){
  const d = new Date(iso);
  d.setDate(d.getDate() + dagen); d.setMinutes(d.getMinutes() + minuten);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}
async function verzetNaar(i, nieuwS, nieuwE, undo){
  const e = WEEK[i];
  nieuwE = nieuwE || nieuwS;
  const oudS = e.start, oudE = e.eind;
  e.start = nieuwS; e.eind = nieuwE; renderWeek(WEEK);  // optimistisch
  try {
    const r = await fetch('/api/verzet', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ id: e.id, start: nieuwS, eind: nieuwE }) });
    if (!r.ok) throw new Error();
    const label = `${dagLabel(nieuwS.slice(0,10))} ${nieuwS.slice(11,16)}`;
    if (undo) toonMetKnop(`📅 “${e.titel}” verzet naar ${label}`, 'Ongedaan maken', undo);
    else toon(`📅 “${e.titel}” staat weer op ${label}`);
  } catch(err){
    e.start = oudS; e.eind = oudE; renderWeek(WEEK);
    toon('Verzetten lukte even niet — probeer nog eens.');
  }
}
function detailEv(i){
  const e = WEEK[i]; if (!e) return;
  document.getElementById('dTitel').textContent = e.titel;
  const tijd = e.start.length > 10
    ? `${dagLabel(e.start.slice(0,10))} · ${e.start.slice(11,16)}` +
      ((e.eind && e.eind.length > 10) ? '–' + e.eind.slice(11,16) : '')
    : `${dagLabel(e.start.slice(0,10))} · hele dag`;
  let rows = `<div>🕐 ${esc(tijd)}</div>`;
  if (e.locatie) rows += `<div>📍 ${esc(e.locatie)}</div>`;
  if (e.wie) rows += `<div>👤 ${esc(e.wie)}</div>`;
  if (e.bron) rows += `<div>🔗 ${esc(e.bron)}</div>`;
  if (e.omschrijving) rows += `<div class="omschr">${esc(e.omschrijving)}</div>`;
  document.getElementById('dRegels').innerHTML = rows;
  // bewerken: alleen Google-afspraken die niet uit een gesynchroniseerde feed komen
  const bewerkbaar = !!e.id && !/automatisch/i.test(e.bron || '');
  document.getElementById('dKnoppen').innerHTML =
    (bewerkbaar ? `<button class="primair" onclick="bewerkEv(${i})">✏️ Bewerken</button>` : '') +
    (!e.id ? `<span class="notitie" style="align-self:center">alleen-lezen (${esc(e.bron || 'externe bron')})</span>` : '') +
    `<button onclick="document.getElementById('detail').style.display='none'">Sluiten</button>`;
  document.getElementById('detail').style.display = 'flex';
}
function nieuwEv(){
  // leeg formulier: vandaag, eerstvolgende hele uur, één uur lang
  const nu = new Date(); const u = Math.min(22, nu.getHours() + 1);
  const p = n => String(n).padStart(2, '0');
  const dag = isoDag(nu);
  WEEK.push({ id: '', _nieuw: true, start: `${dag}T${p(u)}:00`, eind: `${dag}T${p(Math.min(23, u + 1))}:00`,
              titel: '', locatie: '', omschrijving: '', bron: 'Gezinsagenda (Google)', wie: '' });
  document.getElementById('detail').style.display = 'flex';
  bewerkEv(WEEK.length - 1);
}
function bewerkEv(i){
  const e = WEEK[i]; if (!e) return;
  const heleDag = e.start.length <= 10;
  const dag = e.start.slice(0,10);
  const eindDag = (e.eind || e.start).slice(0,10);
  const s = heleDag ? '09:00' : e.start.slice(11,16);
  const t = heleDag ? '10:00' : (e.eind && e.eind.length > 10 ? e.eind.slice(11,16) : s);
  document.getElementById('dTitel').textContent = e._nieuw ? 'Nieuwe afspraak' : 'Afspraak bewerken';
  document.getElementById('dRegels').innerHTML = `<div class="bewerk">
    <label>Titel</label><input id="bwTitel" value="${esc(e.titel)}" maxlength="200">
    <label>Datum</label><div class="tijden"><input type="date" id="bwDag" value="${dag}">
      <label><input type="checkbox" id="bwHeleDag" ${heleDag ? 'checked' : ''} onchange="document.getElementById('bwTijden').style.display=this.checked?'none':'flex'"> hele dag</label></div>
    <label>Tijd</label><div class="tijden" id="bwTijden" style="${heleDag ? 'display:none' : ''}">
      <input type="time" id="bwStart" value="${s}" step="300"> <span>tot</span> <input type="time" id="bwEind" value="${t}" step="300"></div>
    <label>Locatie</label><input id="bwLocatie" value="${esc(e.locatie || '')}" maxlength="200">
    <label>Notitie</label><textarea id="bwOmschr" maxlength="2000">${esc(e.omschrijving || '')}</textarea>
  </div>`;
  document.getElementById('dKnoppen').innerHTML =
    `<button class="primair" onclick="bewaarEv(${i}, ${heleDag && eindDag !== dag ? `'${eindDag}'` : 'null'})">Opslaan</button>` +
    (e._nieuw ? `<button onclick="WEEK.splice(${i},1);document.getElementById('detail').style.display='none'">Annuleren</button>`
              : `<button onclick="detailEv(${i})">Annuleren</button>` +
                `<button style="margin-left:auto;color:var(--rood)" onclick="verwijderEv(${i})">🗑 Verwijderen</button>`);
  setTimeout(() => document.getElementById('bwTitel').focus(), 50);
}
async function bewaarEv(i, meerdaagsEind){
  const e = WEEK[i]; if (!e) return;
  const titel = document.getElementById('bwTitel').value.trim();
  const dag = document.getElementById('bwDag').value;
  const heleDag = document.getElementById('bwHeleDag').checked;
  let start, eind;
  if (heleDag){
    start = dag;
    // meerdaagse afspraak: einddatum meeschuiven met het verschil van de begindatum
    eind = meerdaagsEind ? schuifIso(meerdaagsEind + 'T00:00', Math.round((new Date(dag + 'T00:00') - new Date(e.start.slice(0,10) + 'T00:00')) / 86400000), 0).slice(0,10) : dag;
  } else {
    start = `${dag}T${document.getElementById('bwStart').value}`;
    eind = `${dag}T${document.getElementById('bwEind').value}`;
    if (eind <= start) { toon('De eindtijd moet na de begintijd liggen.'); return; }
  }
  if (!titel || !dag) { toon('Titel en datum zijn nodig.'); return; }
  const body = { id: e.id, titel, start, eind, actie: e._nieuw ? 'nieuw' : '',
    locatie: document.getElementById('bwLocatie').value.trim(),
    omschrijving: document.getElementById('bwOmschr').value.trim() };
  const knop = document.querySelector('#dKnoppen .primair'); if (knop){ knop.disabled = true; knop.textContent = '… opslaan'; }
  try {
    const r = await fetch('/api/event', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY }, body: JSON.stringify(body) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'fout');
    Object.assign(e, { titel, start, eind, locatie: body.locatie, omschrijving: body.omschrijving });
    if (e._nieuw){ e.id = d.id || ''; delete e._nieuw; WEEK.sort((a, b) => a.start < b.start ? -1 : 1); i = WEEK.indexOf(e); }
    renderWeek(WEEK); detailEv(i);
    toon(`📅 “${titel}” ${body.actie === 'nieuw' ? 'toegevoegd' : 'bijgewerkt'}`); ververs();
  } catch(err){ toon('Opslaan lukte even niet: ' + err.message); if (knop){ knop.disabled = false; knop.textContent = 'Opslaan'; } }
}
async function verwijderEv(i){
  const e = WEEK[i]; if (!e || !e.id) return;
  if (!confirm(`“${e.titel}” uit de gezinsagenda verwijderen?`)) return;
  try {
    const r = await fetch('/api/event', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ id: e.id, actie: 'verwijder' }) });
    if (!r.ok) throw new Error();
    WEEK.splice(i, 1); renderWeek(WEEK);
    document.getElementById('detail').style.display = 'none';
    toon(`🗑 “${e.titel}” verwijderd`); ververs();
  } catch(err){ toon('Verwijderen lukte even niet — probeer nog eens.'); }
}

async function ververs(){
  try {
    const r = await fetch('/api/overview', { headers: { 'X-Dashboard-Key': KEY } });
    if (r.status === 401) { toonSleutel(); return; }
    const d = await r.json();
    document.getElementById('sleutel').style.display = 'none';
    document.getElementById('app').style.display = 'block';
    document.getElementById('klok').textContent = d.nu;
    DATA = d;
    PERSONEN = d.personen || [];
    document.getElementById('tabGeld').style.display = d.geld_tab ? '' : 'none';
    if (WEEKOFFSET === 0){ WEEK = d.week || []; renderWeek(WEEK); }  // andere week: laten staan
    // vandaag-agenda: tijdlijn uit dezelfde weekdata (indexen verwijzen naar WEEK voor de detailkaart)
    const wk = (d.week || []).map((e, i) => Object.assign({}, e, { _i: i }));
    const vandaagKey = isoDag(weekStart(0));
    document.getElementById('agenda').innerHTML = agendaHtml(wk.filter(e => e.start.slice(0,10) >= vandaagKey), 40);
    meetKop();
    // aandacht: Birdy's punten en regel-signalen als kaartjes; samen max 4 op het bord
    const a = d.aandacht || { birdy: { items: [] }, signalen: [] };
    const bItems = (a.birdy && a.birdy.items) || [];
    const bTijd = a.birdy && a.birdy.tijd ? a.birdy.tijd.slice(-5) : '';
    const sig = a.signalen || [];
    const bTonen = bItems.slice(0, 3), sTonen = sig.slice(0, Math.max(2, 6 - bTonen.length));
    const kaarten = bTonen.map(x => birdyKaart(x, bTijd)).concat(sTonen.map(aandachtKaart));
    document.getElementById('aandacht').innerHTML =
      kaarten.length ? kaarten.join('') : '<p class="leeg">niets dat aandacht vraagt 🙂</p>';
    document.getElementById('aaCount').textContent = (bItems.length + sig.length) || '';
    const restN = (bItems.length - bTonen.length) + (sig.length - sTonen.length);
    document.getElementById('aandachtrest').innerHTML = restN > 0
      ? `<span class="klik" onclick="openL2('aandacht')">nog ${restN} ${restN > 1 ? 'punten' : 'punt'} — alles ↗</span>` : '';
    // zijbalk: onderwerpen
    const mo = d.onderwerpen || [];
    document.getElementById('moCount').textContent = mo.length || '';
    document.getElementById('moList').innerHTML = mo.length
      ? mo.slice(0, 3).map(o => `<li><span>${esc(o.naam)}</span>` +
          `<small class="${o.dagen !== null && o.dagen < 0 ? 'laat' : o.dagen === 0 || o.dagen === 1 ? 'nu' : ''}">` +
          `${o.dagen !== null ? dagenLabel(o.dagen) : esc(o.wanneer || '')}</small></li>`).join('') +
        (mo.length > 3 ? `<li class="meer">… nog ${mo.length - 3}</li>` : '')
      : '<li class="leeg">niets lopends 🎉</li>';
    document.getElementById('acties').innerHTML = actiesGroepen(d.acties || [], 40);
    actiesRing(d.acties || [], d.acties_af || []);
    const afWrap = document.getElementById('actiesAfWrap');
    afWrap.style.display = (d.acties_af && d.acties_af.length) ? 'block' : 'none';
    document.getElementById('actiesAf').innerHTML = (d.acties_af || []).map(afRij).join('');
    // zijbalk: compacte mini-kaarten
    const mb = d.boodschappen || [];
    document.getElementById('mbCount').textContent = mb.length || '';
    document.getElementById('mbList').innerHTML = mb.length
      ? mb.slice(0, 3).map(x => `<li><span>${esc(x.tekst)}</span></li>`).join('') +
        (mb.length > 3 ? `<li class="meer">… nog ${mb.length - 3}</li>` : '')
      : '<li class="leeg">lijst is leeg 🎉</li>';
    const mv = d.verjaardagen || [];
    document.getElementById('mvCount').textContent = mv.length || '';
    document.getElementById('mrCount').textContent = (d.regelzaken || []).length || '';
    document.getElementById('mvList').innerHTML = mv.length
      ? mv.slice(0, 2).map(j => `<li><span>${esc(j.naam)}</span>` +
          `<small class="${j.dagen === 0 ? 'nu' : ''}">${dagenLabel(j.dagen)}</small></li>`).join('')
      : '<li class="leeg">geen bekend</li>';
    const mr = d.regelzaken || [];
    document.getElementById('mrList').innerHTML = mr.length
      ? mr.slice(0, 3).map(z => `<li><span>${esc(z.naam)}</span>` +
          `<small class="${z.dagen !== null && z.dagen < 0 ? 'laat' : z.dagen === 0 ? 'nu' : ''}">` +
          `${dagenLabel(z.dagen)}</small></li>`).join('') +
        (mr.length > 3 ? `<li class="meer">… nog ${mr.length - 3}</li>` : '')
      : '<li class="leeg">handboek nog leeg</li>';
    const th = d.thuis;
    const miniThuis = document.getElementById('miniThuis');
    miniThuis.style.display = th ? 'block' : 'none';
    if (th){
      const rijen = [];
      if (th.zon_w !== null || th.net_w !== null){
        const e = energie(th);
        let r = [e.zon !== null ? `☀️ ${kw(e.zon)}` : '', e.huis !== null ? `🏠 ${kw(e.huis)}` : '']
                  .filter(Boolean).join(' · ');
        rijen.push(`<li><span>${r}</span>${nettoLabel(th)}</li>`);
      }
      const woon = (th.klimaat || []).find(k => /woon/i.test(k.kamer)) || (th.klimaat || [])[0];
      if (woon) rijen.push(`<li><span>🌡️ ${woon.temp}° ${esc(woon.kamer)}</span></li>`);
      if (th.auto) rijen.push(`<li><span>🚗 ${esc(th.auto.naam)}</span>` +
        `<small>${th.auto.batterij ?? '?'}%${th.auto.laadt ? ' ⚡' : ''}</small></li>`);
      const lampen = (th.lampen_aan || []).length;
      const deur = th.deur ? (th.deur.dicht === true ? '🔒 op slot' : th.deur.dicht === false ? '🔓 open' : '') : '';
      if (lampen || deur) rijen.push(`<li><span>${lampen ? `💡 ${lampen} aan` : ''}${lampen && deur ? ' · ' : ''}${deur}</span></li>`);
      document.getElementById('mtList').innerHTML = rijen.join('') || '<li class="leeg">geen gegevens</li>';
    }
    renderL2();
  } catch (e) { /* volgende poging over 60s */ }
}
function toonSleutel(){ document.getElementById('app').style.display='none';
  document.getElementById('sleutel').style.display='block'; }
try {  // 'plan' wordt pas hersteld nadat de planningsfuncties geladen zijn (regel onderaan)
  const t0 = localStorage.getItem('birdy-tab') || 'vandaag';
  if (t0 !== 'plan') kiesTab(t0);
} catch(e){ kiesTab('vandaag'); }
ververs(); setInterval(ververs, 60000);

// ── planning: kinderroutine met slots, drie weergaven en bonus ────────────
const ROUTINES = {
  ochtend: [
    { e:'🚽', n:'Naar de wc', m:3 },
    { e:'👕', n:'Aankleden', m:5 },
    { e:'🥣', n:'Ontbijten', m:15 },
    { e:'🪥', n:'Tandenpoetsen', m:3 },
    { e:'🧺', n:'Haren en wassen', m:4 },
    { e:'🎒', n:'Tas en schoenen', m:4 },
  ],
  avond: [
    { e:'🧸', n:'Speelgoed opruimen', m:5 },
    { e:'🛁', n:'Wassen', m:10 },
    { e:'🩳', n:'Pyjama aan', m:3 },
    { e:'🪥', n:'Tandenpoetsen', m:3 },
    { e:'🚽', n:'Nog even plassen', m:2 },
  ],
};
const BONUS = { e:'🎬', n:'Filmpje of boekje', basis: 5 };
function plVandaag(){ return new Date().toLocaleDateString('sv-SE'); }
function plLeeg(dagdeel){
  let versie = 'kaart';
  try { versie = localStorage.getItem('birdy-plan-versie') || 'kaart'; } catch(e){}
  return { datum: plVandaag(), dagdeel, versie, gekozen: [], start: 0, af: [],
           vertrek: dagdeel === 'ochtend' ? '08:00' : '19:30' };
}
function plGeldig(s){ return s && s.datum === plVandaag() && Array.isArray(s.gekozen); }
function plLaad(){
  // eerste vulling uit de browseropslag (offline-terugval); de server is leidend (plSync)
  let s = null;
  try { s = JSON.parse(localStorage.getItem('birdy-plan')); } catch(e){}
  if (!plGeldig(s)) s = plLeeg(new Date().getHours() < 14 ? 'ochtend' : 'avond');
  return s;
}
let PLAN = plLaad();
// ── gedeelde planning: één status op de server, zodat tablet en telefoon dezelfde timer zien ──
let PLTAKEN = {};            // eigen taaklijsten per dagdeel (gedeeld)
try { PLTAKEN = JSON.parse(localStorage.getItem('birdy-plan-taken')) || {}; } catch(e){}
let plServerTs = 0, plPushTimer = null, plPushBezig = false, plPushWacht = null;
function plSyncPush(deel){
  plPushWacht = Object.assign(plPushWacht || {}, deel);
  clearTimeout(plPushTimer);
  plPushTimer = setTimeout(async () => {
    const body = plPushWacht; plPushWacht = null; plPushBezig = true;
    try {
      const r = await fetch('/api/plan', { method:'POST',
        headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY }, body: JSON.stringify(body) });
      const d = await r.json();
      if (r.ok && d.bijgewerkt) plServerTs = d.bijgewerkt;
    } catch(e){ /* offline: lokale kopie blijft, volgende wijziging probeert opnieuw */ }
    plPushBezig = false;
  }, 250);
}
async function plSync(){
  if (!KEY || plPushBezig || plPushWacht) return;
  try {
    const r = await fetch('/api/plan', { headers: { 'X-Dashboard-Key': KEY } });
    if (!r.ok) return;
    const d = await r.json();
    if (!d.bijgewerkt || d.bijgewerkt <= plServerTs) return;
    plServerTs = d.bijgewerkt;
    if (d.taken && typeof d.taken === 'object') PLTAKEN = d.taken;
    if (plGeldig(d.plan)) PLAN = d.plan;
    try { localStorage.setItem('birdy-plan', JSON.stringify(PLAN)); localStorage.setItem('birdy-plan-taken', JSON.stringify(PLTAKEN)); } catch(e){}
    if (document.getElementById('paneelPlan').style.display !== 'none') renderPlan();
  } catch(e){ /* volgende poging */ }
}
plSync(); setInterval(plSync, 3000);
const EMOJIS = ['🚽','👕','🥣','🪥','🧺','🎒','🧸','🛁','🩳','📖','🧦','🍎','🐕','🎨','✏️','🚲'];
function takenVan(d){
  if (Array.isArray(PLTAKEN[d]) && PLTAKEN[d].length) return PLTAKEN[d];
  return ROUTINES[d];
}
function takenBewaar(d, lijst){
  PLTAKEN[d] = lijst;
  try { localStorage.setItem('birdy-plan-taken', JSON.stringify(PLTAKEN)); } catch(e){}
  plSyncPush({ taken: PLTAKEN });
}
function taakTijd(i, delta){
  const lijst = takenVan(PLAN.dagdeel).map(t => ({ ...t }));
  lijst[i].m = Math.max(1, Math.min(60, lijst[i].m + delta));
  takenBewaar(PLAN.dagdeel, lijst); renderPlan();
}
function taakWeg(i){
  const lijst = takenVan(PLAN.dagdeel).map(t => ({ ...t }));
  lijst.splice(i, 1);
  PLAN.gekozen = PLAN.gekozen.filter(g => g !== i).map(g => g > i ? g - 1 : g);
  takenBewaar(PLAN.dagdeel, lijst); plBewaar(); renderPlan();
}
function takenHerstel(){
  delete PLTAKEN[PLAN.dagdeel];
  try { localStorage.setItem('birdy-plan-taken', JSON.stringify(PLTAKEN)); } catch(e){}
  plSyncPush({ taken: PLTAKEN });
  PLAN.gekozen = []; plBewaar(); renderPlan();
}
let plNieuwEmoji = EMOJIS[0], plNieuwMin = 5;
function plNieuwOpen(){
  plNieuwEmoji = EMOJIS[0]; plNieuwMin = 5;
  document.getElementById('pnNaam').value = '';
  plNieuwTeken();
  document.getElementById('plNieuw').style.display = 'flex';
  document.getElementById('pnNaam').focus();
}
function plNieuwTeken(){
  document.getElementById('pnEmojis').innerHTML = EMOJIS.map(e =>
    `<button class="pn-em${e === plNieuwEmoji ? ' actief' : ''}"
       onclick="plNieuwEmoji='${e}';plNieuwTeken()">${e}</button>`).join('');
  document.getElementById('pnMin').textContent = plNieuwMin + ' min';
}
function plNieuwMinStap(d){ plNieuwMin = Math.max(1, Math.min(60, plNieuwMin + d)); plNieuwTeken(); }
function plNieuwToevoegen(){
  const naam = document.getElementById('pnNaam').value.trim();
  if (!naam){ toon('Geef het taakje een naam!'); return; }
  const lijst = takenVan(PLAN.dagdeel).map(t => ({ ...t }));
  lijst.push({ e: plNieuwEmoji, n: naam.slice(0, 30), m: plNieuwMin });
  takenBewaar(PLAN.dagdeel, lijst);
  document.getElementById('plNieuw').style.display = 'none';
  renderPlan();
}
let plWaarsch = null, plWaarschVertrek = false;
function plBewaar(){
  try { localStorage.setItem('birdy-plan', JSON.stringify(PLAN)); } catch(e){}
  plSyncPush({ plan: PLAN });
}
function plDagdeel(d){ PLAN = plLeeg(d); PLAN.dagdeel = d;
  PLAN.vertrek = d === 'ochtend' ? '08:00' : '19:30'; plBewaar(); renderPlan(); }
function plVersie(v){ PLAN.versie = v;
  try { localStorage.setItem('birdy-plan-versie', v); } catch(e){}
  plBewaar(); renderPlan(); }
function plReset(){ PLAN = plLeeg(PLAN.dagdeel); plWaarsch = null; plWaarschVertrek = false;
  plBewaar(); renderPlan(); }
function plStart(){
  if (!PLAN.gekozen.length) { toon('Sleep eerst wat kaartjes naar links!'); return; }
  const t = document.getElementById('plVertrek');
  if (t && t.value) PLAN.vertrek = t.value;
  const nuK = new Date();
  if (hmNaarMin(PLAN.vertrek) <= nuK.getHours() * 60 + nuK.getMinutes() + 1){
    toon('⏰ De eindtijd is al (bijna) geweest — kies een latere tijd!'); return;
  }
  PLAN.start = Date.now(); PLAN.af = []; plWaarsch = null; plWaarschVertrek = false;
  plBewaar(); renderPlan(); deuntje();
}
function plKaartHtml(t, extra){
  return `<span class="em">${t.e}</span><span class="naam">${t.n}</span>` +
         `<span class="tijd">${t.m} min</span>` + (extra || '');
}
// actieve taak = eerste onafgevinkte in de gekozen volgorde; die start zodra de
// vorige is afgevinkt (dus vroeg klaar = volgende kaart begint direct te lopen)
function plActief(){ return PLAN.gekozen[PLAN.af.length]; }
function plActiefStart(){
  return PLAN.af.length ? PLAN.af[PLAN.af.length - 1].t : PLAN.start;
}
function bonusMinuten(){
  const r = takenVan(PLAN.dagdeel);
  const klaarPlanned = PLAN.af.reduce((som, a) => som + r[a.i].m, 0);
  const alles = PLAN.af.length === PLAN.gekozen.length;
  const eind = alles && PLAN.af.length
    ? (PLAN.af[PLAN.af.length - 1].t - PLAN.start) / 60000
    : (Date.now() - PLAN.start) / 60000;
  return Math.max(0, Math.round(BONUS.basis + klaarPlanned - eind));
}
function renderPlan(){
  const el = document.getElementById('paneelPlan');
  el.classList.toggle('breed', !!PLAN.start && PLAN.versie === 'balk');
  const r = takenVan(PLAN.dagdeel);
  const kop = `<div class="pl-kop"><h2>${PLAN.dagdeel === 'ochtend' ? '🌞 Goedemorgen!' : '🌙 Avondprogramma'}</h2>
    <div class="pl-versie">
      <button class="${PLAN.versie==='kaart'?'actief':''}" onclick="plVersie('kaart')">Kaartjes</button>
      <button class="${PLAN.versie==='balk'?'actief':''}" onclick="plVersie('balk')">Tijdlijn</button>
      <button class="${PLAN.versie==='ster'?'actief':''}" onclick="plVersie('ster')">Sterren</button>
    </div>
    <div class="pl-dagdeel">
      <button class="${PLAN.dagdeel==='ochtend'?'actief':''}" onclick="plDagdeel('ochtend')">Ochtend</button>
      <button class="${PLAN.dagdeel==='avond'?'actief':''}" onclick="plDagdeel('avond')">Avond</button>
    </div></div>`;
  if (!PLAN.start){ renderOpzet(el, r, kop); return; }
  if (PLAN.versie === 'balk') renderBalk(el, r, kop);
  else if (PLAN.versie === 'ster') renderSterren(el, r, kop);
  else renderKaarten(el, r, kop);
  plTick();
}
// ── opzet: placeholders links, voorraad rechts ──
function renderOpzet(el, r, kop){
  PLAN.gekozen = PLAN.gekozen.filter(g => g < r.length);
  const slots = PLAN.gekozen.map((i, s) =>
    `<li class="pl-slot vol" data-s="${s}"><div class="pl-kaart mini" onclick="plWeg(${s})"
      title="tik om terug te leggen">${plKaartHtml(r[i])}</div></li>`)
    .concat(PLAN.gekozen.length < r.length
      ? [`<li class="pl-slot" data-s="${PLAN.gekozen.length}">${PLAN.gekozen.length + 1}e taak hier</li>`] : []);
  const bak = r.map((t, i) => PLAN.gekozen.includes(i) ? '' :
    `<li class="pl-kaart mini sleepbaar" data-i="${i}"
       onpointerdown="plPak(event,${i})" onpointermove="plSleepMove(event)"
       onpointerup="plLos(event)" onpointercancel="plLos(event)">
       <span class="em">${t.e}</span><span class="naam">${t.n}</span>
       <span class="tijd"><button class="stap" onpointerdown="event.stopPropagation()"
         onclick="event.stopPropagation();taakTijd(${i},-1)">−</button> ${t.m}m
         <button class="stap" onpointerdown="event.stopPropagation()"
         onclick="event.stopPropagation();taakTijd(${i},1)">+</button></span>
       <button class="weg" title="taakje weghalen" onpointerdown="event.stopPropagation()"
         onclick="event.stopPropagation();taakWeg(${i})">✕</button></li>`).join('');
  el.innerHTML = kop +
    `<p class="pl-hint">Sleep de taken die je gaat doen naar links, in jouw volgorde.
      Wat je niet hoeft, laat je gewoon staan!</p>` +
    `<div class="pl-opzet">
      <div><div class="pl-kolomkop">📋 Mijn plan</div><ul class="pl-slots" id="plSlots">${slots.join('')}</ul></div>
      <div><div class="pl-kolomkop">🧺 Taken</div><ul class="pl-bak">${bak}
        <li class="pl-kaart mini nieuw" onclick="plNieuwOpen()">
          <span class="em">➕</span><span class="naam">Nieuw taakje…</span></li></ul></div>
    </div>` +
    `<div class="pl-tijd-rij"><span>${PLAN.dagdeel === 'ochtend' ? '🕗 We vertrekken om' : '🕢 Bedtijd om'}</span>
      <input type="time" id="plVertrek" value="${PLAN.vertrek}">
      <span>· daarna: ${BONUS.e} ${BONUS.n.toLowerCase()} (${BONUS.basis} min + bonus!)</span></div>` +
    `<button class="pl-start" onclick="plStart()">Start! ▶</button>` +
    `<button class="pl-reset" onclick="plReset()">opnieuw beginnen</button>
     <button class="pl-reset" onclick="takenHerstel()">standaardtaken herstellen</button>`;
}
function plWeg(s){ PLAN.gekozen.splice(s, 1); plBewaar(); renderPlan(); }
let plPakData = null;
function plPak(ev, i){
  const li = ev.currentTarget;
  plPakData = { li, i, x0: ev.clientX, y0: ev.clientY, bezig: false };
  try { li.setPointerCapture(ev.pointerId); } catch(e){}
  window.addEventListener('pointerup', plLos, { once: true });
  window.addEventListener('pointercancel', plLos, { once: true });
}
function plSleepMove(ev){
  if (!plPakData) return;
  const s = plPakData, dx = ev.clientX - s.x0, dy = ev.clientY - s.y0;
  if (!s.bezig && Math.abs(dx) + Math.abs(dy) > 8){ s.bezig = true; s.li.classList.add('tilt'); }
  if (s.bezig){ s.li.style.transform = `translate(${dx}px, ${dy}px)`;
    s.li.style.zIndex = 20; s.px = ev.clientX; s.py = ev.clientY; }
}
function plLos(ev){
  if (!plPakData) return;
  const s = plPakData; plPakData = null;
  s.li.style.transform = ''; s.li.style.zIndex = ''; s.li.classList.remove('tilt');
  if (!s.bezig){
    PLAN.gekozen.push(s.i); plBewaar(); renderPlan(); return;
  }
  s.li.style.visibility = 'hidden';
  const doel = document.elementFromPoint(s.px || 0, s.py || 0);
  s.li.style.visibility = '';
  const slot = doel && doel.closest ? doel.closest('.pl-slot') : null;
  if (slot){
    const plek = Math.min(parseInt(slot.dataset.s, 10), PLAN.gekozen.length);
    PLAN.gekozen.splice(plek, 0, s.i);
    plBewaar(); renderPlan();
  }
}
// ── weergave 1: kaartjes (opeenvolgend: balk van de actieve taak loopt) ──
function renderKaarten(el, r, kop){
  const actief = plActief();
  el.innerHTML = kop +
    `<ul class="pl-lijst">` + PLAN.gekozen.map(i => {
      const t = r[i], af = PLAN.af.some(a => a.i === i);
      return `<li class="pl-kaart${af ? ' af' : ''}${i === actief ? ' nu' : ''}" data-i="${i}"
        onclick="plVier(event, ${i})">
        ${plKaartHtml(t, `<button class="pl-vink">${af ? '✓' : ''}</button><div class="balk"></div>`)}</li>`;
    }).join('') + `</ul>` +
    `<div class="pl-bonuskaart"><span class="em">${BONUS.e}</span>
      <span class="naam">${BONUS.n}</span><b id="plBonus"></b><div class="balk" id="plBonusBalk"></div></div>` +
    `<button class="pl-reset" onclick="plReset()">opnieuw beginnen</button>`;
}
// ── weergave 2: tijdlijn met vertrektijd ──
function hmNaarMin(hm){ const d = hm.split(':'); return (+d[0]) * 60 + (+d[1]); }
function balkModel(){
  const r = takenVan(PLAN.dagdeel);
  const startD = new Date(PLAN.start);
  const startMin = startD.getHours() * 60 + startD.getMinutes() + startD.getSeconds() / 60;
  const totaal = Math.max(1, hmNaarMin(PLAN.vertrek) - startMin);  // start → eindtijd
  const nu = (Date.now() - PLAN.start) / 60000;
  const actief = plActief();
  // kaartjes staan vast op hun geplande breedte; alleen bij een afvink verspringt de
  // lay-out (pad = tijd tot de start van de actieve taak). De klok loopt er los doorheen.
  const pad = (plActiefStart() - PLAN.start) / 60000;
  const segs = [{ pad: true, breed: Math.max(pad, 0.05) }];
  let planned = 0, restLive = 0;
  PLAN.gekozen.forEach(i => {
    if (PLAN.af.some(a => a.i === i)) return;
    const m = r[i].m;
    planned += m;
    if (i === actief){ restLive += Math.max(0, m - (nu - pad)); }
    else { restLive += m; }
    segs.push({ i, actief: i === actief, breed: m, op: i === actief && (nu - pad) > m });
  });
  const bonusBreed = Math.max(0, totaal - pad - planned);
  segs.push({ bonus: true, breed: Math.max(bonusBreed, 0.01) });
  const bonusLive = Math.max(0, (totaal - nu) - restLive);
  return { segs, totaal, nu, bonusMin: Math.round(bonusLive) };
}
function renderBalk(el, r, kop){
  const m = balkModel();
  const klok = ts => new Date(ts).toTimeString().slice(0, 5);
  el.innerHTML = kop +
    `<p class="pl-hint">De rode stip is de klok — blijf hem voor! Alles wat je overhoudt is
      ${BONUS.e} bonustijd. Klaar met een taak? Tik erop!</p>` +
    `<div class="pl-plank">🏆` +
      (PLAN.af.length
        ? PLAN.af.map((a, k) => `<span class="badge${k === PLAN.af.length - 1 ? ' nieuw' : ''}"
            style="border-color:${KLEUREN[a.i % KLEUREN.length]}">${r[a.i].e}</span>`).join('')
        : `<span class="leeg-plank">hier komen jouw medailles!</span>`) +
    `</div><div class="pl-balkwrap"><div class="pl-verleden" id="plVerleden"></div><div class="pl-balk2" id="plBalk2">` +
    m.segs.map(s => s.pad
      ? `<div class="pl-seg pad" id="plSegPad" style="flex:${s.breed} 1 0">🐾</div>`
      : s.bonus
      ? `<div class="pl-seg bonus" id="plSegBonus" style="flex:${s.breed} 1 0">
           <span class="em2">${BONUS.e}</span><span id="plBonus"></span></div>`
      : `<div class="pl-seg${s.actief ? ' nu2' : ''}${s.op ? ' op' : ''}" data-i="${s.i}"
           style="flex:${s.breed} 1 0;background:${KLEUREN[s.i % KLEUREN.length]}26"
           onclick="plVier(event, ${s.i})">
           <span class="em2">${r[s.i].e}</span>
           <span>${r[s.i].n.split(' ')[0]}</span><span>${r[s.i].m}m</span></div>`).join('') +
    `</div>
    <div class="pl-rail"><div class="vul" id="plRailVul"></div>
      <div class="stip" id="plRailStip">🦊</div><div class="nulabel" id="plRailNu"></div></div>
    <div class="pl-tijden"><span>▶ ${klok(PLAN.start)}</span>
      <span>🏁 ${PLAN.dagdeel === 'ochtend' ? 'vertrek' : 'bedtijd'} ${PLAN.vertrek}</span></div></div>` +
    `<div class="pl-timer">
      <svg viewBox="0 0 120 120">
        <circle class="ringbg" cx="60" cy="60" r="52"/>
        <circle class="ring buiten" id="ringTot" cx="60" cy="60" r="52"
          transform="rotate(-90 60 60)"/>
        <circle class="ringbg" cx="60" cy="60" r="40"/>
        <circle class="ring binnen" id="ringAct" cx="60" cy="60" r="40"
          transform="rotate(-90 60 60)"/>
      </svg>
      <div class="pl-timer-tekst"><b id="timerAct">–:–</b><span id="timerTot"></span></div>
    </div>` +
    `<button class="pl-reset" onclick="plReset()">opnieuw beginnen</button>`;
}
// ── weergave 3: sterren (blokhoogte = tijd; vroeg klaar = blok krimpt, bonus groeit) ──
function renderSterren(el, r, kop){
  const actief = plActief();
  const px = 9;  // hoogte per minuut
  el.innerHTML = kop +
    `<p class="pl-hint">Snel klaar? Dan wordt jouw ${BONUS.e}-blok groter en verdien je sterren! ⭐</p>` +
    `<ul class="pl-lijst">` + PLAN.gekozen.map(i => {
      const t = r[i];
      const afRec = PLAN.af.find(a => a.i === i);
      const volgIdx = PLAN.gekozen.indexOf(i);
      let minuten = t.m;
      if (afRec){
        const vorigeT = volgIdx === 0 ? PLAN.start : PLAN.af[volgIdx - 1].t;
        minuten = Math.max(1, (afRec.t - vorigeT) / 60000);
      }
      const hoogte = Math.max(afRec ? 2.4 : 3.6, minuten * px / 16) + 'rem';
      return `<li class="pl-kaart${afRec ? ' af' : ''}${i === actief ? ' nu' : ''}" data-i="${i}"
        style="min-height:${hoogte};height:${hoogte};transition:height .6s"
        onclick="plVier(event, ${i})">
        ${plKaartHtml(t, `<button class="pl-vink">${afRec ? '✓' : ''}</button><div class="balk"></div>`)}</li>`;
    }).join('') + `</ul>` +
    `<div class="pl-bonuskaart" id="plBonusGroei"><span class="em">${BONUS.e}</span>
      <span><span class="naam">${BONUS.n}</span><br><span class="pl-sterren" id="plSterren"></span></span>
      <b id="plBonus"></b></div>` +
    `<button class="pl-reset" onclick="plReset()">opnieuw beginnen</button>`;
}
function plTick(){
  if (!PLAN.start || document.getElementById('paneelPlan').style.display === 'none') return;
  const r = takenVan(PLAN.dagdeel);
  const bonus = bonusMinuten();
  const bonusEl = document.getElementById('plBonus');
  const actief = plActief();
  // waarschuwing: nog 1 minuut voor de actieve taak
  if (actief !== undefined){
    const rest = r[actief].m * 60 - (Date.now() - plActiefStart()) / 1000;
    if (rest <= 60 && rest > 0 && plWaarsch !== actief){ plWaarsch = actief; attentie(); }
  }
  if (PLAN.versie === 'balk'){
    const m = balkModel();
    m.segs.forEach(s => {
      const el = s.pad ? document.getElementById('plSegPad')
        : s.bonus ? document.getElementById('plSegBonus')
        : document.querySelector(`.pl-seg[data-i="${s.i}"]`);
      if (!el) return;
      el.style.flexGrow = s.breed;
      if (!s.bonus && !s.pad){ el.classList.toggle('nu2', !!s.actief); el.classList.toggle('op', !!s.op); }
    });
    const pct = Math.min(99.3, Math.max(0.7, m.nu / m.totaal * 100)) + '%';
    const verleden = document.getElementById('plVerleden');
    if (verleden) verleden.style.width = Math.min(100, m.nu / m.totaal * 100) + '%';
    const vul = document.getElementById('plRailVul');
    const stip = document.getElementById('plRailStip');
    const nulabel = document.getElementById('plRailNu');
    if (vul) vul.style.width = pct;
    if (stip) stip.style.left = pct;
    if (nulabel){ nulabel.style.left = pct;
      nulabel.textContent = new Date().toTimeString().slice(0, 5); }
    if (bonusEl) bonusEl.textContent = m.bonusMin + ' min';
    // grote timer: buitenring = totaal tot eindtijd, binnenring = huidige taak
    const ringAct = document.getElementById('ringAct');
    const ringTot = document.getElementById('ringTot');
    if (ringAct && ringTot){
      const CT = 2 * Math.PI * 52, CB = 2 * Math.PI * 40;
      const totRest = Math.max(0, m.totaal - m.nu);
      ringTot.style.strokeDasharray = CT;
      ringTot.style.strokeDashoffset = CT * (1 - totRest / m.totaal);
      let tekst = '🎉', fracA = 1, op = false;
      if (actief !== undefined){
        const actRest = r[actief].m * 60 - (Date.now() - plActiefStart()) / 1000;
        op = actRest <= 0;
        fracA = Math.max(0, actRest) / (r[actief].m * 60);
        const s = Math.max(0, Math.ceil(actRest));
        tekst = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
      }
      ringAct.style.strokeDasharray = CB;
      ringAct.style.strokeDashoffset = CB * (1 - fracA);
      ringAct.classList.toggle('op', op);
      document.getElementById('timerAct').textContent = tekst;
      document.getElementById('timerTot').textContent =
        actief !== undefined ? 'nog ' + Math.round(totRest) + ' min totaal' : 'alles af!';
    }
    // waarschuwing: nog 1 minuut tot vertrek/bedtijd
    const restTot = m.totaal - m.nu;
    if (restTot <= 1 && restTot > 0 && !plWaarschVertrek){ plWaarschVertrek = true; attentie(); }
  } else {
    // kaartjes & sterren: alleen de actieve kaart loopt, direct na de vorige afvink
    const startAct = plActiefStart();
    PLAN.gekozen.forEach(i => {
      const kaart = document.querySelector(`#paneelPlan .pl-kaart[data-i="${i}"]`);
      if (!kaart) return;
      const balk = kaart.querySelector('.balk');
      if (!balk) return;
      if (PLAN.af.some(a => a.i === i)) return;  // af = 100% via CSS
      if (i === actief){
        const frac = Math.min(1, (Date.now() - startAct) / 60000 / r[i].m);
        balk.style.width = (frac * 100) + '%';
        balk.style.background = frac >= 1 ? 'var(--rood)' : '';
      } else { balk.style.width = '0%'; }
    });
    if (bonusEl) bonusEl.textContent = bonus + ' min';
    const bb = document.getElementById('plBonusBalk');
    if (bb) bb.style.width = Math.min(100, bonus / (BONUS.basis * 3) * 100) + '%';
    const groei = document.getElementById('plBonusGroei');
    if (groei){
      groei.style.minHeight = (3.4 + bonus * 0.35) + 'rem';
      const sterren = document.getElementById('plSterren');
      if (sterren) sterren.textContent = '⭐'.repeat(Math.min(bonus, 15));
    }
  }
}
setInterval(plTick, 1000);
function plVier(ev, i){
  if (PLAN.af.some(a => a.i === i)) return;
  if (i !== plActief()) return;  // altijd in de gekozen volgorde
  const r = takenVan(PLAN.dagdeel);
  const gebruikt = (Date.now() - plActiefStart()) / 60000;
  const verdiend = Math.round(r[i].m - gebruikt);
  PLAN.af.push({ i, t: Date.now() }); plBewaar();
  const rect = ev.currentTarget.getBoundingClientRect();
  confetti(rect.left + rect.width / 2, rect.top + rect.height / 2, 60);
  if (verdiend > 0) popEffect(rect.right - 40, rect.top, `+${verdiend} min ⭐`);
  renderPlan();
  if (PLAN.af.length === PLAN.gekozen.length){
    const einde = document.getElementById('plKlaar');
    const eindBonus = PLAN.versie === 'balk' ? balkModel().bonusMin : bonusMinuten();
    einde.querySelector('p').textContent =
      `Alles is af — je hebt ${eindBonus} minuten ${BONUS.n.toLowerCase()} verdiend!`;
    einde.style.display = 'flex';
    confetti(innerWidth / 2, innerHeight / 3, 220);
    fanfare();
    setTimeout(() => { einde.style.display = 'none'; }, 7000);
  } else { deuntje(); }
}
function popEffect(x, y, tekst){
  const d = document.createElement('div');
  d.textContent = tekst;
  d.style.cssText = `position:fixed;left:${x}px;top:${y}px;z-index:95;font-size:1.3rem;` +
    `font-weight:800;color:var(--amber);pointer-events:none;white-space:nowrap`;
  document.body.appendChild(d);
  d.animate([{ transform:'translateY(0)', opacity:1 }, { transform:'translateY(-70px)', opacity:0 }],
            { duration: 1400, easing:'ease-out' }).onfinish = () => d.remove();
}
function attentie(){ [880, 660, 880].forEach((f, i) => noot(f, i * 0.22, 0.3)); }
// confetti + geluid (zelfvoorzienend, geen externe bestanden)
function confetti(x, y, n){
  const c = document.getElementById('plCanvas');
  c.width = innerWidth; c.height = innerHeight;
  const ctx = c.getContext('2d');
  const p = Array.from({ length: n }, () => ({
    x, y, vx: (Math.random() - .5) * 14, vy: -Math.random() * 12 - 3,
    r: Math.random() * 5 + 3, k: KLEUREN[Math.floor(Math.random() * KLEUREN.length)],
    a: Math.random() * Math.PI }));
  const t0 = performance.now();
  (function stap(t){
    const dt = (t - t0) / 1000;
    ctx.clearRect(0, 0, c.width, c.height);
    if (dt > 1.8) return;
    p.forEach(d => {
      d.x += d.vx; d.y += d.vy; d.vy += .45; d.a += .2;
      ctx.save(); ctx.translate(d.x, d.y); ctx.rotate(d.a);
      ctx.fillStyle = d.k; ctx.globalAlpha = Math.max(0, 1 - dt / 1.8);
      ctx.fillRect(-d.r, -d.r / 2, d.r * 2, d.r); ctx.restore();
    });
    requestAnimationFrame(stap);
  })(t0);
}
let audioCtx = null;
function noot(freq, wanneer, duur){
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.type = 'triangle'; o.frequency.value = freq;
    const t = audioCtx.currentTime + wanneer;
    g.gain.setValueAtTime(0.001, t);
    g.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
    g.gain.exponentialRampToValueAtTime(0.001, t + duur);
    o.connect(g); g.connect(audioCtx.destination);
    o.start(t); o.stop(t + duur + 0.05);
  } catch(e){}
}
function deuntje(){ [523, 659, 784].forEach((f, i) => noot(f, i * 0.12, 0.28)); }
function fanfare(){ [523, 659, 784, 1047, 784, 1047, 1319].forEach((f, i) => noot(f, i * 0.16, 0.34)); }
try { if (localStorage.getItem('birdy-tab') === 'plan') kiesTab('plan'); } catch(e){}

// ── chat ──────────────────────────────────────────────────────────────────
let chatGesch = [];
try { chatGesch = JSON.parse(localStorage.getItem('birdy-chat')) || []; } catch (e) {}
function chatBewaar(){ try { localStorage.setItem('birdy-chat',
  JSON.stringify(chatGesch.slice(-60))); } catch (e) {} }
function chatRender(){ const log = document.getElementById('chatlog');
  log.innerHTML = chatGesch.map(m => `<div class="bub ${m.wie}">${esc(m.tekst)}</div>`).join('');
  log.scrollTop = log.scrollHeight; }
function chatOpen(open){
  document.getElementById('chat').classList.toggle('open', open);
  document.getElementById('chatfab').style.display = open ? 'none' : 'flex';
  if (open) { chatRender(); document.getElementById('chatveld').focus(); }
}
function chatVoeg(wie, tekst){ chatGesch.push({ wie, tekst }); chatBewaar(); chatRender(); }

async function stuur(tekst){
  tekst = (tekst || '').trim(); if (!tekst) return;
  document.getElementById('invoer').value = '';
  document.getElementById('chatveld').value = '';
  chatOpen(true); chatVoeg('ik', tekst);
  const log = document.getElementById('chatlog');
  const wacht = document.createElement('div');
  wacht.className = 'bub birdy wacht'; wacht.textContent = '…denkt na…';
  log.appendChild(wacht); log.scrollTop = log.scrollHeight;
  try {
    const r = await fetch('/api/message', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ text: tekst }) });
    const d = await r.json();
    wacht.remove(); chatVoeg('birdy', d.reply || d.error || 'er ging iets mis');
    ververs();
  } catch(e){ wacht.remove(); chatVoeg('birdy', 'Ik ben even niet bereikbaar — probeer het zo nog eens.'); }
}

async function voegToe(ev, lijst, input){
  if (ev.key !== 'Enter' && ev.keyCode !== 13) return;
  const tekst = input.value.trim(); if (!tekst) return;
  input.value = ''; input.placeholder = '… toevoegen';
  try {
    const r = await fetch('/api/add', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ lijst, tekst }) });
    if (!r.ok) throw new Error();
    await ververs();
  } catch(e){ input.value = tekst; toon('Toevoegen lukte even niet — probeer nog eens.'); }
  input.placeholder = '+ toevoegen…';
}
async function zetDatum(id, datum){
  try {
    const r = await fetch('/api/due', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ id, datum }) });
    if (!r.ok) throw new Error();
    await ververs();
  } catch(e){ toon('Deadline zetten lukte even niet — probeer nog eens.'); }
}
function kiesDatum(id){
  const inp = document.createElement('input');
  inp.type = 'date'; inp.min = new Date().toISOString().slice(0,10);
  inp.style.cssText = 'position:fixed;bottom:0;left:0;opacity:0;pointer-events:none';
  document.body.appendChild(inp);
  inp.onchange = () => { if (inp.value) zetDatum(id, inp.value); inp.remove(); };
  try { inp.showPicker(); }
  catch(e){ inp.remove();
    const d = prompt('Deadline (JJJJ-MM-DD):');
    if (d && /^\d{4}-\d{2}-\d{2}$/.test(d.trim())) zetDatum(id, d.trim()); }
}
async function vink(el, id){
  el.classList.add('gedaan');
  const tekst = (el.querySelector('span')?.childNodes[0]?.textContent || 'taak').trim();
  try {
    const r = await fetch('/api/done', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ id }) });
    if (!r.ok) throw new Error();
    toonMetKnop(`✓ Afgevinkt: ${tekst}`, 'Ongedaan maken', () => herstel(id));
    setTimeout(ververs, 800);
  } catch(e){
    el.classList.remove('gedaan');
    toon('Afvinken lukte even niet — probeer nog eens.');
  }
}
async function herstel(id){
  try {
    const r = await fetch('/api/reopen', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY },
      body: JSON.stringify({ id }) });
    if (!r.ok) throw new Error();
    toon('Hersteld 👍'); ververs();
  } catch(e){ toon('Herstellen lukte even niet — check de Todoist-app.'); }
}
function toon(t){ const a = document.getElementById('melding');
  a.textContent = t; a.style.display = 'block';
  clearTimeout(a._t); a._t = setTimeout(() => a.style.display='none', 8000); }
function toonMetKnop(t, knoptekst, actie){
  const a = document.getElementById('melding');
  a.textContent = t;
  const b = document.createElement('button');
  b.textContent = knoptekst;
  b.onclick = () => { a.style.display = 'none'; actie(); };
  a.appendChild(b);
  a.style.display = 'block';
  clearTimeout(a._t); a._t = setTimeout(() => a.style.display='none', 10000); }

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null, luistert = false;
function spraak(knop, cb){
  if (!SR) {
    if (!cb) { chatOpen(true); document.getElementById('chatveld').focus(); }
    toon('🎤 Spraak werkt in Chrome of Safari — typen kan altijd.');
    return;
  }
  if (luistert) { rec.stop(); return; }
  rec = new SR(); rec.lang = 'nl-NL'; rec.interimResults = false;
  rec.onstart = () => { luistert = true; knop.classList.add('luistert'); };
  rec.onend = () => { luistert = false; knop.classList.remove('luistert'); };
  rec.onerror = () => toon('🎤 Ik kon je niet verstaan — probeer nog eens.');
  rec.onresult = ev => (cb || stuur)(ev.results[0][0].transcript);
  rec.start();
}
document.getElementById('invoer').addEventListener('keydown',
  e => { if (e.key === 'Enter' || e.keyCode === 13) stuur(e.target.value); });
document.getElementById('chatveld').addEventListener('keydown',
  e => { if (e.key === 'Enter' || e.keyCode === 13) stuur(e.target.value); });
document.getElementById('tvVeld').addEventListener('keydown',
  e => { if (e.key === 'Enter' || e.keyCode === 13) tvStuur(e.target.value); });

// ── PWA: installeerbaar op het beginscherm van telefoon/tablet ──
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => { navigator.serviceWorker.register('/sw.js').catch(() => {}); });
}

// ── Geld-tab: financieel register uit de Sheet, achter een pincode (per apparaat één keer) ──
let GELD = null, GELDPIN = null, VERREKEN = null;
try { GELDPIN = sessionStorage.getItem('birdy-geld-pin'); } catch(e){}
function euro(n){ return '€ ' + (Math.round(n || 0)).toLocaleString('nl-NL'); }
function euro2(n){ return '€ ' + (n || 0).toLocaleString('nl-NL', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
async function renderGeld(){
  const el = document.getElementById('paneelGeld');
  if (!GELDPIN){
    el.innerHTML = `<div class="geld-pin"><h2>🔒 Geld</h2><p class="notitie">Pincode om het financieel overzicht te openen</p>
      <input id="geldPin" type="password" inputmode="numeric" pattern="[0-9]*" maxlength="8" autocomplete="off"
        onkeydown="if(event.key==='Enter')geldOntgrendel()">
      <button onclick="geldOntgrendel()">Openen</button></div>`;
    setTimeout(() => { const i = document.getElementById('geldPin'); if (i) i.focus(); }, 50);
    return;
  }
  if (!GELD){ el.innerHTML = '<p class="leeg" style="margin:2rem">overzicht laden…</p>'; await geldLaad(); if (!GELD) return; }
  const g = GELD;
  if (!g.beschikbaar){
    el.innerHTML = `<div class="geld-kop"><h2>💶 Geld</h2></div><p class="leeg">Nog geen register. Vraag Birdy: “maak het financieel overzicht aan” (of draai <code>financien.py maak</code>).</p>` + geldWoordenlijst(g);
    return;
  }
  const t = g.totalen, h = g.hypotheek, v = g.verrekening;
  const cats = Object.entries(g.per_categorie || {});
  const catTot = cats.reduce((a, [, n]) => a + n, 0) || 1;
  const kleuren = ['#7fbfa6', '#d9a44e', '#8ab4d8', '#b39ddb', '#e07a6a', '#f2a1c2', '#5b6570'];
  let html = `<div class="geld-kop"><h2>💶 Geld</h2><span class="notitie">stand ${esc(g.vandaag || '')} · per maand</span>
    ${g.link ? `<a href="${esc(g.link)}" target="_blank" rel="noopener">📄 Sheet openen</a>` : ''}
    <button class="klein" onclick="geldLaad(true).then(renderGeld)">↻ vernieuwen</button>
    <button class="klein" onclick="GELDPIN=null;try{sessionStorage.removeItem('birdy-geld-pin')}catch(e){};renderGeld()">🔒 sluiten</button></div>`;
  html += '<div class="geld-grid">';
  // 1. vaste lasten
  html += `<div class="panel"><div class="kaartkop"><span class="ico">📉</span><h2>Vaste lasten</h2></div>
    <div class="geld-groot">${euro(t.vast_pm)}<small>vaste lasten per maand</small></div>
    ${t.inkomen_pm ? `<div class="geld-rij"><span>Inkomen (${(g.inkomsten || []).length} bronnen)</span><b>${euro(t.inkomen_pm)}</b></div>
    <div class="geld-rij"><span><b>Blijft over voor de rest</b></span><b style="color:${t.over_pm >= 0 ? 'var(--accent)' : 'var(--rood)'}">${euro(t.over_pm)}</b></div>` : ''}
    <div class="geld-balk">${cats.map(([c, n], i) => `<i style="width:${(n / catTot * 100).toFixed(1)}%;background:${kleuren[i % kleuren.length]}" title="${esc(c)}"></i>`).join('')}</div>
    ${cats.map(([c, n], i) => `<div class="geld-rij"><span><i class="init" style="background:${kleuren[i % kleuren.length]};width:.7rem;height:.7rem;display:inline-block;border-radius:3px;margin-right:.4rem"></i>${esc(c)}</span><b>${euro(n)}</b></div>`).join('')}
    <div class="geld-rij"><span>Polissen</span><b>${euro(t.polissen_pm)}</b></div>
    <div class="geld-rij"><span>Hypotheek</span><b>${euro(h.maandlast)}</b></div>
    ${t.in_pm ? `<div class="geld-rij"><span>Komt terug (constructies)</span><b style="color:var(--accent)">− ${euro(t.in_pm)}</b></div>
    <div class="geld-rij"><span><b>Netto per maand</b></span><b>${euro(t.netto_pm)}</b></div>` : ''}
    <h4 style="margin-top:.8rem">Grootste posten</h4>
    ${(g.vaste_lasten || []).slice(0, 8).map(l => `<div class="geld-rij"><span>${esc(l.naam)} <small>· ${esc(l.betaald_van)}${l.hoort_bij !== l.betaald_van ? ' → ' + esc(l.hoort_bij) : ''}</small></span><b>${euro2(l.per_maand)}</b></div>`).join('') || '<p class="leeg">nog geen vaste lasten ingevuld</p>'}
    ${geldUitlegKnop(g, 'vaste lasten', 'Leg in gewone taal uit waar ons geld elke maand naartoe gaat en wat het grootste aandeel is.')}
  </div>`;
  // 2. hypotheek
  const rentePct = h.maandlast ? Math.round(h.rente / h.maandlast * 100) : 0;
  html += `<div class="panel"><div class="kaartkop"><span class="ico">🏠</span><h2>Hypotheek</h2></div>
    ${h.delen.length ? `<div class="geld-groot">${euro(h.maandlast)}<small>per maand</small></div>
    <div class="geld-balk"><i style="width:${rentePct}%;background:var(--rood)" title="rente"></i><i style="width:${100 - rentePct}%;background:var(--accent)" title="aflossing"></i></div>
    <div class="geld-rij"><span><i class="init" style="background:var(--rood);width:.7rem;height:.7rem;display:inline-block;border-radius:3px;margin-right:.4rem"></i>Rente (kost geld)</span><b>${euro(h.rente)}</b></div>
    <div class="geld-rij"><span><i class="init" style="background:var(--accent);width:.7rem;height:.7rem;display:inline-block;border-radius:3px;margin-right:.4rem"></i>Aflossing (bouwt bezit op)</span><b>${euro(h.aflossing)}</b></div>
    <div class="geld-rij"><span>Nog te betalen (restschuld)</span><b>${euro(h.restschuld)}</b></div>
    <div class="geld-rij"><span>Al afgelost</span><b>${euro(h.hoofdsom - h.restschuld)} <small>(${h.hoofdsom ? Math.round((h.hoofdsom - h.restschuld) / h.hoofdsom * 100) : 0}%)</small></b></div>
    ${h.delen.map(d => `<div class="geld-rij"><span>${esc(d.deel)} <small>· ${esc(d.vorm)} · ${d.rente_pct}%</small></span><small>${d.rentevast_dagen !== null ? 'rente vast tot ' + esc(d.rentevast_tot) : ''}</small></div>`).join('')}`
    : '<p class="leeg">nog geen hypotheek ingevuld</p>'}
    ${geldUitlegKnop(g, 'hypotheek', 'Leg in gewone taal uit hoe onze hypotheek werkt: wat rente en aflossing zijn, wat er van ons maandbedrag waarheen gaat, en wat er gebeurt als de rentevaste periode afloopt.')}
  </div>`;
  // 3. polissen
  html += `<div class="panel"><div class="kaartkop"><span class="ico">🛡️</span><h2>Polissen</h2><b>${(g.polissen || []).length || ''}</b></div>
    ${(g.polissen || []).map(p => `<div class="geld-rij"><span>${esc(p.naam)} <small>· ${esc(p.verzekeraar)}${p.eigen_risico ? ' · eigen risico ' + euro(p.eigen_risico) : ''}</small></span>
      ${p.dagen !== null ? `<small class="${p.dagen < 45 ? 'laat' : ''}" style="${p.dagen < 45 ? 'color:var(--amber)' : ''}">${p.dagen < 0 ? 'verlopen' : 'tot ' + esc(p.einddatum)}</small>` : ''}<b>${euro2(p.per_maand)}</b></div>`).join('') || '<p class="leeg">nog geen polissen ingevuld</p>'}
    ${geldUitlegKnop(g, 'polissen', 'Leg in gewone taal uit welke verzekeringen we hebben, waar ze voor zijn, wat eigen risico betekent en waar we op moeten letten bij opzeggen.')}
  </div>`;
  // 4. constructies + verrekenen (pot-model: structureel uit het register + losse posten)
  const vr = VERREKEN || { posten: [], afrekeningen: [] };
  const open = vr.posten.filter(p => !p.verrekend);
  const saldoLos = {};
  open.forEach(p => { saldoLos[p.wie] = Math.round(((saldoLos[p.wie] || 0) + (p.richting === 'voor_pot' ? p.bedrag : -p.bedrag)) * 100) / 100; });
  const personen = [...new Set([...(v.personen || []), ...PERSONEN.slice(0, 2)])].filter(Boolean);
  html += `<div class="panel"><div class="kaartkop"><span class="ico">🔁</span><h2>Constructies</h2></div>
    ${(g.constructies || []).map(c => `<div class="geld-rij"><span>${esc(c.naam)}<br><small>uit ${euro(c.uit_pm)} · terug ${euro(c.in_pm)} per maand</small></span><b style="color:${c.netto_pm >= 0 ? 'var(--accent)' : 'var(--rood)'}">${c.netto_pm >= 0 ? '+' : '−'} ${euro(Math.abs(c.netto_pm))}</b></div>
      ${c.uitleg ? `<div class="geld-uitleg">${esc(c.uitleg)}</div>` : ''}`).join('') || '<p class="leeg">geen constructies</p>'}
    ${(g.inleg || []).length ? '<h4 style="margin-top:.6rem">Afspraak: inleg op de pot</h4>' + g.inleg.map(c => `<div class="geld-rij"><span>${esc(c.naam)}</span><b>${euro(c.uit_pm)}<small> /mnd</small></b></div>`).join('') : ''}
    ${geldUitlegKnop(g, 'verrekenen', 'Leg in gewone taal uit hoe onze geldstromen en constructies werken, en wat de inleg-afspraak inhoudt.')}
  </div>`;
  html += `<div class="panel"><div class="kaartkop"><span class="ico">⚖️</span><h2>Verrekenen</h2><b>${open.length || ''}</b></div>
    <p class="notitie">Iets privé betaald dat van de pot (gezamenlijk) had moeten komen? Zet het hier; einde maand druk je op Afrekenen.</p>
    <div class="vr-form">
      <select id="vrWie">${personen.map(p => `<option>${esc(p)}</option>`).join('')}</select>
      <input id="vrBedrag" type="number" inputmode="decimal" step="0.01" min="0" placeholder="€ bedrag">
      <input id="vrOms" placeholder="wat was het?" maxlength="120" onkeydown="if(event.key==='Enter')vrToevoegen()">
      <select id="vrRichting"><option value="voor_pot">betaald voor de pot</option><option value="uit_pot">van de pot ontvangen / privé uitgegeven</option></select>
      <button class="aknop" onclick="vrToevoegen()">＋ Toevoegen</button>
    </div>
    ${open.length ? open.slice().reverse().map(p => `<div class="geld-rij"><small>${esc(p.datum.slice(5))}</small><span>${esc(p.omschrijving || '(geen omschrijving)')} <small>· ${esc(p.wie)} ${p.richting === 'voor_pot' ? 'voor de pot' : 'uit de pot'}</small></span><b>${p.richting === 'voor_pot' ? '+' : '−'} ${euro2(p.bedrag)}</b><button class="herstelknop" title="verwijderen" onclick="vrVerwijder('${p.id}')">✕</button></div>`).join('')
      : '<p class="leeg">geen open posten</p>'}
    ${open.length ? `<div class="geld-saldo">${Object.entries(saldoLos).map(([w, b]) => b >= 0 ? `pot → ${esc(w)}: ${euro2(b)}` : `${esc(w)} → pot: ${euro2(-b)}`).join(' · ')}</div>
      <button class="blikknop" onclick="vrAfrekenen()">✅ Afrekenen (${open.length} ${open.length === 1 ? 'post' : 'posten'})</button>` : ''}
    <h4 style="margin-top:.8rem">Structureel, per maand</h4>
    <div class="geld-saldo" style="font-weight:500">${esc(v.tekst || 'in balans')}</div>
    ${(v.regels || []).slice(0, 8).map(r => `<div class="geld-rij"><span>${esc(r.wat)} <small>· ${esc(r.tekst)}</small></span><b>${r.richting === 'van_pot' ? '+' : '−'} ${euro2(r.bedrag)}</b></div>`).join('')}
    ${(vr.afrekeningen || []).length ? '<h4 style="margin-top:.8rem">Eerdere afrekeningen</h4>' + vr.afrekeningen.slice(0, 4).map(a => `<div class="geld-rij"><small>${esc(a.datum.slice(5))}</small><span>${esc(a.tekst)} <small>· ${a.aantal} posten</small></span></div>`).join('') : ''}
  </div>`;
  html += '</div>' + geldWoordenlijst(g);
  el.innerHTML = html;
}
function geldUitlegKnop(g, onderwerp, vraag){
  const u = (g.uitleg || {})[onderwerp];
  return (u && u.tekst ? `<div class="geld-uitleg">🐦 ${esc(u.tekst)}${u.bijgewerkt ? `<br><small class="notitie">Birdy · ${esc(u.bijgewerkt)}</small>` : ''}</div>` : '') +
    `<button class="aknop" onclick="stuur(${JSON.stringify(vraag + ' Gebruik onze eigen cijfers (financien.py toon).')})">🐦 ${u && u.tekst ? 'Opnieuw uitleggen' : 'Leg uit met onze cijfers'}</button>`;
}
function geldWoordenlijst(g){
  return `<div class="panel geld-wl" style="margin-top:.9rem"><div class="kaartkop"><span class="ico">📖</span><h2>Woordenlijst</h2></div>` +
    (g.woordenlijst || []).map(([w, u]) => `<details><summary>${esc(w)}</summary><p>${esc(u)}</p></details>`).join('') + '</div>';
}
async function geldLaad(ververs){
  try {
    const r = await fetch('/api/geld' + (ververs ? '?ververs=1' : ''), { headers: { 'X-Dashboard-Key': KEY, 'X-Geld-Pin': GELDPIN || '' } });
    const d = await r.json();
    if (r.status === 403){ GELDPIN = null; try { sessionStorage.removeItem('birdy-geld-pin'); } catch(e){} toon(d.error || 'pincode klopt niet'); GELD = null; renderGeld(); return; }
    if (!r.ok) throw new Error(d.error || 'fout');
    GELD = d;
    const rv = await fetch('/api/verreken', { headers: { 'X-Dashboard-Key': KEY, 'X-Geld-Pin': GELDPIN || '' } });
    if (rv.ok) VERREKEN = await rv.json();
  } catch(e){ toon('Financieel overzicht laden lukte even niet.'); }
}
async function vrPost(body){
  const r = await fetch('/api/verreken', { method:'POST', headers:{ 'Content-Type':'application/json', 'X-Dashboard-Key':KEY, 'X-Geld-Pin':GELDPIN || '' }, body: JSON.stringify(body) });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || 'fout');
  return d;
}
async function vrToevoegen(){
  const wie = document.getElementById('vrWie').value, bedrag = document.getElementById('vrBedrag').value,
        omschrijving = document.getElementById('vrOms').value.trim(), richting = document.getElementById('vrRichting').value;
  if (!bedrag || Number(bedrag) <= 0){ toon('Vul een bedrag in.'); return; }
  try { await vrPost({ actie:'toevoegen', wie, bedrag, omschrijving, richting }); toon(`⚖️ Genoteerd: ${wie} ${euro2(Number(bedrag))}`); await geldLaad(); renderGeld(); }
  catch(e){ toon('Toevoegen lukte niet: ' + e.message); }
}
async function vrVerwijder(id){
  try { await vrPost({ actie:'verwijderen', id }); await geldLaad(); renderGeld(); } catch(e){ toon('Verwijderen lukte niet.'); }
}
async function vrAfrekenen(){
  if (!confirm('Alle open posten afrekenen en afsluiten?')) return;
  try { const d = await vrPost({ actie:'afrekenen' }); toon('✅ Afgerekend: ' + d.afrekening.tekst); await geldLaad(); renderGeld(); }
  catch(e){ toon('Afrekenen lukte niet: ' + e.message); }
}
async function geldOntgrendel(){
  const pin = (document.getElementById('geldPin').value || '').trim();
  if (!pin) return;
  GELDPIN = pin; GELD = null;
  await geldLaad();
  if (GELD){ try { sessionStorage.setItem('birdy-geld-pin', pin); } catch(e){} renderGeld(); }
}
