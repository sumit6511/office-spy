'use strict';
/* Office Spy client.
   The server sends a personalised game state; we render it as HTML and *patch* the live DOM
   (a small keyed morph), so only what changed moves. Inputs keep focus, and animations
   play only for elements that are genuinely new. */

const IS_SCREEN = location.pathname.startsWith('/screen');
if (IS_SCREEN) document.body.classList.add('screen');
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

// Secret token that proves who this device is. It is only ever sent to the server when connecting,
// never shown to other players (they see a separate public id). Kept in localStorage so it survives
// closing the tab. `?seat=2` gives a separate identity, for testing several players in one browser.
// crypto.randomUUID needs HTTPS (which a LAN game lacks), but getRandomValues works over plain HTTP.
const TOKEN_KEY = 'token' + (new URLSearchParams(location.search).get('seat') || '').replace(/[^\w-]/g, '');
const newToken = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
};
let token = store.get(TOKEN_KEY);
if (!token) { token = newToken(); store.set(TOKEN_KEY, token); }

let ws, S = null, recvAt = 0, connected = false, removed = false;
let lastViewKey = '', lastPhase = null, lastTickSec = null;
const ui = {folderOpen: false, folderKey: '', slip: false, packSent: false};
const typedClues = new Set();
let clueMark = {view: '', count: 0};

// ------------------------------------------------------------------ helpers

const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const fmt = s => { s = Math.ceil(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const player = id => S.players.find(p => p.id === id);
const nameOf = id => esc((player(id) || {name: '?'}).name);
const isHost = () => !!(S.you && S.you.is_host);
const buzz = p => { try { navigator.vibrate && navigator.vibrate(p); } catch (e) {} };
const UC = () => S.mode === 'undercover';
const WORDS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve'];
const num = n => WORDS[n] || String(n);
const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : many}`;
const fileNo = () => `File ${String(S.round).padStart(3, '0')}${S.mode === 'undercover' && S.discussion ? `, round ${num(S.discussion)}` : ''}`;

const CHIP_COLORS = ['#b8322a', '#2c5aa0', '#2b2b2b', '#6a3d9a', '#d9822b', '#1f7f86', '#b84a78', '#7a5230', '#23395d', '#5a7d1e'];
function chip(id, size = '') {
  const p = player(id) || {name: '?'};
  let h = 0;
  for (const c of p.name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return `<span class="chip ${size}" style="--c:${CHIP_COLORS[h % CHIP_COLORS.length]}" aria-hidden="true">${esc(p.name[0].toUpperCase())}</span>`;
}

function send(type, data = {}) {
  Sound.unlock();
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({type, ...data}));
  else toast('Not connected yet. Trying again…');
}

function toast(msg, ok = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = `paper show${ok ? ' ok' : ''}`;
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = 'paper'), 2800);
}

const ICON = {
  sound: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16.5 8.5a5 5 0 0 1 0 7"/><path d="M19 6a8.5 8.5 0 0 1 0 12"/></svg>',
  mute: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9v6h4l5 4V5L8 9z"/><path d="m17 9 5 6M22 9l-5 6"/></svg>',
};

// ------------------------------------------------------------------ DOM morph (keyed patching)

const keyOf = n => (n.nodeType === 1 ? n.getAttribute('data-key') : null);
function syncAttrs(a, b) {
  for (const {name} of [...a.attributes]) if (!b.hasAttribute(name) && name !== 'open') a.removeAttribute(name);
  for (const {name, value} of [...b.attributes]) if (a.getAttribute(name) !== value) a.setAttribute(name, value);
}
function patch(a, b) {
  if (a.nodeType !== 1) { if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue; return; }
  syncAttrs(a, b);
  const tag = a.tagName;
  if (tag === 'INPUT') { if (a.type === 'checkbox') a.checked = b.hasAttribute('checked'); return; } // typed text belongs to the user
  if (tag === 'TEXTAREA') return;
  morphChildren(a, b);
  if (tag === 'SELECT' && document.activeElement !== a) {
    const sel = b.querySelector('option[selected]');
    if (sel) a.value = sel.value;
  }
}
function morphChildren(from, to) {
  const wanted = new Set();
  for (const n of to.childNodes) { const k = keyOf(n); if (k) wanted.add(k); }
  for (const n of [...from.childNodes]) { const k = keyOf(n); if (k && !wanted.has(k)) from.removeChild(n); }
  const keyed = new Map();
  for (const n of from.childNodes) { const k = keyOf(n); if (k) keyed.set(k, n); }
  let i = 0;
  for (const t of [...to.childNodes]) {
    const cur = from.childNodes[i], k = keyOf(t);
    let match = null;
    if (k) { match = keyed.get(k); if (match && match.nodeName !== t.nodeName) match = null; }
    else if (cur && !keyOf(cur) && cur.nodeType === t.nodeType && cur.nodeName === t.nodeName) match = cur;
    if (match) {
      if (k) keyed.delete(k);
      if (match !== cur) from.insertBefore(match, cur || null);
      patch(match, t);
    } else {
      from.insertBefore(t, cur || null);
    }
    i++;
  }
  while (from.childNodes.length > i) from.removeChild(from.lastChild);
}

// ------------------------------------------------------------------ sound (synthesised in the browser)

const Sound = (() => {
  let ctx = null, muted = localStorage.getItem('muted') === '1';
  function unlock() {
    try {
      if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === 'suspended') ctx.resume();
    } catch (e) {}
  }
  function tone(freq, dur = .12, {type = 'sine', gain = .14, delay = 0, slide = 0} = {}) {
    if (muted || !ctx) return;
    const t = ctx.currentTime + delay, o = ctx.createOscillator(), g = ctx.createGain();
    o.type = type;
    o.frequency.setValueAtTime(freq, t);
    if (slide) o.frequency.exponentialRampToValueAtTime(freq * slide, t + dur);
    g.gain.setValueAtTime(.0001, t);
    g.gain.exponentialRampToValueAtTime(gain, t + .012);
    g.gain.exponentialRampToValueAtTime(.0001, t + dur);
    o.connect(g).connect(ctx.destination);
    o.start(t);
    o.stop(t + dur + .03);
  }
  function noise(dur = .12, gain = .3, delay = 0) { // short burst: paper rustle / stamp thump
    if (muted || !ctx) return;
    const len = ctx.sampleRate * dur, buf = ctx.createBuffer(1, len, ctx.sampleRate), d = buf.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3);
    const src = ctx.createBufferSource(), g = ctx.createGain(), f = ctx.createBiquadFilter();
    f.type = 'lowpass'; f.frequency.value = 900;
    src.buffer = buf; g.gain.value = gain;
    src.connect(f).connect(g).connect(ctx.destination);
    src.start(ctx.currentTime + delay);
  }
  const fx = {
    join() { tone(660, .08); tone(990, .12, {delay: .07}); },
    deal() { noise(.18, .25); tone(240, .25, {type: 'triangle', slide: 2.2, gain: .08}); },
    flip() { noise(.14, .22); },
    stamp() { noise(.09, .9, .42); tone(70, .2, {type: 'sine', gain: .35, delay: .42}); },
    turn() { tone(784, .12); tone(1175, .22, {delay: .12}); },
    key() { noise(.03, .25); },
    tick() { tone(1300, .03, {type: 'square', gain: .035}); },
    vote() { tone(500, .06, {type: 'triangle', gain: .1}); },
    phase() { [392, 523, 659].forEach((f, i) => tone(f, .14, {delay: i * .09})); },
    suspense() { for (let i = 0; i < 10; i++) tone(98 + i * 11, .08, {type: 'sawtooth', gain: .04, delay: i * .1}); },
    win() { [523, 659, 784, 1047, 1319].forEach((f, i) => tone(f, .24, {delay: i * .1, type: 'triangle'})); },
    lose() { [392, 330, 262, 196].forEach((f, i) => tone(f, .28, {delay: i * .15, type: 'sawtooth', gain: .06})); },
    error() { tone(160, .2, {type: 'square', gain: .05}); },
  };
  return {unlock, fx, get muted() { return muted; }, toggle() { muted = !muted; localStorage.setItem('muted', muted ? '1' : '0'); }};
})();
document.addEventListener('pointerdown', Sound.unlock);

// ------------------------------------------------------------------ paper-shred confetti

function shreds() {
  if (REDUCED) return;
  const c = document.createElement('canvas');
  c.className = 'shreds';
  const dpr = Math.min(2, devicePixelRatio || 1);
  c.width = innerWidth * dpr; c.height = innerHeight * dpr;
  document.body.appendChild(c);
  const x = c.getContext('2d');
  x.scale(dpr, dpr);
  const colors = ['#e8d3a2', '#d2b673', '#f3eedd', '#b3261e', '#24468f', '#e2b455'];
  const parts = Array.from({length: 140}, () => ({
    x: innerWidth * (.2 + Math.random() * .6), y: -20 - Math.random() * innerHeight * .4, vx: (Math.random() - .5) * 2, vy: 2 + Math.random() * 3,
    w: 5 + Math.random() * 5, h: 14 + Math.random() * 16, r: Math.random() * 6, vr: (Math.random() - .5) * .2, sway: Math.random() * 6,
    col: colors[Math.random() * colors.length | 0],
  }));
  const t0 = performance.now();
  (function frame(t) {
    const k = (t - t0) / 1000;
    x.clearRect(0, 0, innerWidth, innerHeight);
    for (const p of parts) {
      p.vy = Math.min(p.vy + .06, 5); p.y += p.vy; p.x += p.vx + Math.sin(k * 3 + p.sway) * .8; p.r += p.vr;
      x.save(); x.translate(p.x, p.y); x.rotate(p.r); x.scale(Math.cos(k * 6 + p.sway), 1);
      x.fillStyle = p.col; x.fillRect(-p.w / 2, -p.h / 2, p.w, p.h); x.restore();
    }
    if (k < 4.2) requestAnimationFrame(frame); else c.remove();
  })(t0);
}

// ------------------------------------------------------------------ connection

function connect() {
  if (removed || (ws && ws.readyState <= 1)) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws?token=${token}&screen=${IS_SCREEN ? 1 : 0}`);
  ws.onopen = () => { connected = true; render(); };
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.type === 'state') { const prev = S; S = m; recvAt = Date.now(); render(); cues(prev, S); }
    else if (m.type === 'error') { toast(m.message); Sound.fx.error(); buzz(120); }
    else if (m.type === 'removed') { removed = true; token = newToken(); store.set(TOKEN_KEY, token); ws.close(); render(); }
  };
  ws.onclose = () => { connected = false; if (S) render(); setTimeout(connect, 1000); };
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) connect(); });

// Sound and haptic cues when something changes.
function cues(a, b) {
  if (!a) return;
  const me = b.you && b.you.id;
  if (a.phase !== b.phase) {
    if (b.phase === 'playing' && a.phase !== 'reveal') { Sound.fx.deal(); buzz([50, 40, 50]); }
    else if (b.phase === 'playing' || b.phase === 'voting') { Sound.fx.phase(); buzz(70); }
    else if (b.phase === 'guess') Sound.fx.suspense();
    else if (b.phase === 'reveal') Sound.fx.stamp();
    else if (b.phase === 'results') {
      const mine = b.you && b.you.in_round;
      const won = mine ? (b.winner === 'spies') === b.spies.includes(me) : true;
      Sound.fx.stamp();
      setTimeout(() => (won ? Sound.fx.win() : Sound.fx.lose()), 650);
      if (won) setTimeout(shreds, 500);
      buzz(won ? [60, 50, 60, 50, 160] : 400);
    }
  }
  if (b.current_turn && b.current_turn !== a.current_turn && b.current_turn === me) { Sound.fx.turn(); buzz([90, 60, 90]); }
  if ((b.clues || []).length > (a.clues || []).length) {
    const n = Math.min(12, (b.clues[b.clues.length - 1].text || '').length);
    for (let i = 0; i < n; i++) setTimeout(Sound.fx.key, i * 55);
  }
  if (b.players.length > a.players.length) Sound.fx.join();
  if (b.phase === 'voting' && a.phase === 'voting' && (b.voted || []).length > (a.voted || []).length) Sound.fx.vote();
}

// ------------------------------------------------------------------ shared pieces

function topBar() {
  const you = S.you;
  const where = S.phase === 'lobby'
    ? `<b>The table</b><small>${cap(plural(S.players.length, 'player', 'players'))} seated</small>`
    : `<b>${esc(S.codename)}</b><small class="typed">${fileNo()}</small>`;
  return `<header class="top" data-key="top">
    <span class="signal ${connected ? '' : 'off'}" title="${connected ? 'Connected' : 'Reconnecting'}"></span>
    <div class="op" style="flex:1">${where}</div>
    <div class="tools">
      ${you ? `<span style="display:flex;align-items:center;gap:8px">${chip(you.id, 'sm')}${you.is_host ? '<span class="crown" title="Host">♛</span>' : ''}</span>` : ''}
      <button class="iconbtn" aria-label="How to play" onclick="openHelp()">?</button>
      <button class="iconbtn" aria-label="${Sound.muted ? 'Turn sound on' : 'Mute'}" onclick="Sound.toggle();render()">${Sound.muted ? ICON.mute : ICON.sound}</button>
    </div></header>`;
}

function clockNow() { return S.seconds_left == null ? null : Math.max(0, S.seconds_left - (Date.now() - recvAt) / 1000); }
function clock(size = '') {
  const left = clockNow();
  if (left == null) return '';
  const pct = S.seconds_total ? (left / S.seconds_total) * 100 : 0;
  const low = left <= (S.seconds_total > 60 ? 30 : 10);
  return `<div class="clock ${size} ${low ? 'low' : ''}" id="clock" data-key="clock-${S.phase}-${S.discussion}-${S.current_turn || ''}" role="timer">
    <div class="n">${fmt(left)}</div><div class="fuse"><i style="width:${pct.toFixed(2)}%"></i></div></div>`;
}

function roster({kick = false} = {}) {
  const host = isHost() && kick;
  return `<ul class="roster" data-key="roster">${S.players.map(p => `
    <li class="enter ${p.online ? '' : 'off'}" data-key="r-${p.id}">
      ${chip(p.id)}
      <span class="nm">${esc(p.name)}${p.is_host ? ' <span class="crown" title="Host">♛</span>' : ''}${S.you && p.id === S.you.id ? ' <small>you</small>' : ''}${p.online ? '' : ' <small>offline</small>'}</span>
      ${host && p.id !== S.you.id ? `<span class="act">
        <button class="iconbtn" aria-label="Make ${esc(p.name)} host" onclick="if(confirm('Hand hosting to this player?'))send('make_host',{target:'${p.id}'})">♛</button>
        <button class="iconbtn" aria-label="Remove ${esc(p.name)}" onclick="if(confirm('Remove this player from the table?'))send('kick',{target:'${p.id}'})">✕</button></span>` : ''}
      <span class="pts">${p.score}</span>
    </li>`).join('')}</ul>`;
}

function invite() {
  return `<section class="rail invite" data-key="invite">
    <img src="/qr.svg" alt="QR code for the join link" onerror="this.remove()">
    <div><h3 style="margin:0 0 6px">Invite the table</h3><p class="hint" style="margin:0 0 6px">Same Wi-Fi, then scan or open:</p>
    <div class="url">${esc(S.join_url)}</div></div></section>`;
}

// The case file: tap to flip the cover open. Spies get a stamp that slams down each time it opens.
function caseFile() {
  const r = S.role;
  if (!r) return '';
  const key = `file-${S.round}`;
  if (ui.folderKey !== key) { ui.folderKey = key; ui.folderOpen = false; ui.slip = false; }
  const open = ui.folderOpen;
  let body, stamp = '';
  if (UC()) {
    body = `<p class="meta">Your word</p><div class="word">${esc(r.word)}</div>
      <p class="note">Most of the table shares this word. One of you holds a similar one, and it might be you.</p>`;
    stamp = '<span class="stamp blue small slam">Eyes only</span>';
  } else if (r.spy) {
    body = `<p class="meta">Category: ${r.category ? esc(r.category) : 'classified'}</p>
      <div class="word"><span class="redacted-word">secretword</span></div>
      <p class="note">You don't know the word. Listen, blend in, and work it out before they work you out.</p>`;
    stamp = '<span class="stamp red slam" style="font-size:58px">SPY</span>';
  } else {
    body = `<p class="meta">Category: ${esc(r.category)}</p><div class="word">${esc(r.word)}</div>
      <p class="note">Prove you know it without giving it away. Someone at this table doesn't.</p>`;
    stamp = '<span class="stamp blue small slam">Cleared</span>';
  }
  const reveal = r.spy && r.can_reveal
    ? `<button class="btn btn-stamp reveal-btn" onclick="event.stopPropagation();if(confirm('Reveal yourself and guess the word now? A wrong guess loses the round.'))send('spy_reveal')">I know the word</button>` : '';
  return `<section class="folder ${open ? 'open' : ''}" data-key="${key}" role="button" tabindex="0" aria-expanded="${open}"
      aria-label="${open ? 'Close your case file' : 'Open your case file'}" onclick="toggleFile()" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleFile()}">
    <div class="paper doc ${open ? 'thud' : ''}">
      ${open ? `<div data-key="stamp-${open}" style="margin-bottom:12px">${stamp}</div>` : ''}
      ${body}${reveal}
    </div>
    <div class="cover" data-tab="${fileNo().split(',')[0]}">
      <span class="seal">Top<br>secret</span>
      <div><p class="to">For the eyes of ${esc(S.you.name)} only</p><h2>Classified</h2></div>
      <div class="tap"><span>↑</span>Tap to open. Keep it hidden.</div>
    </div>
  </section>`;
}
function toggleFile() {
  ui.folderOpen = !ui.folderOpen;
  if (ui.folderOpen) { Sound.fx.flip(); if (S.role && S.role.spy) Sound.fx.stamp(); buzz(30); }
  render();
}

// Small version for later phases: the word sits under a redaction bar you tap to lift.
function slip() {
  const r = S.role;
  if (!r) return '';
  const w = UC() || !r.spy ? esc(r.word) : 'You are the spy';
  return `<div class="paper slip ${ui.slip ? 'show' : ''}" data-key="slip-${S.round}" role="button" tabindex="0"
      onclick="ui.slip=!ui.slip;render()" aria-label="${ui.slip ? 'Hide your word' : 'Show your word'}">
    <span class="meta">${ui.slip ? 'Tap to hide' : 'Your file, tap to peek'}</span><span class="w">${w}</span></div>`;
}

function clueLog(viewKey, title = 'Transmission log') {
  if (!S.clues || !S.clues.length) return '';
  const groups = [];
  S.clues.forEach((c, i) => {
    const g = UC() && S.discussion > 1 ? `Round ${c.d}${S.clue_rounds > 1 ? `, lap ${c.r}` : ''}` : (S.clue_rounds > 1 ? `Lap ${c.r}` : '');
    if (!groups.length || groups[groups.length - 1].g !== g) groups.push({g, items: []});
    groups[groups.length - 1].items.push([c, i]);
  });
  const line = ([c, i]) => {
    const k = `${viewKey}:${i}`;
    if (!typedClues.has(k) && clueMark.view === viewKey && i >= clueMark.count && c.text) typedClues.add(k);
    const typing = typedClues.has(k);
    return `<li class="${typing ? 'typing' : ''}" data-key="c-${i}" style="--n:${(c.text || '').length}">
      <span class="who">${nameOf(c.pid)}</span>${c.text == null ? '<span class="txt skip">no clue given</span>' : `<span class="txt">${esc(c.text)}</span>`}</li>`;
  };
  return `<section class="paper log" data-key="log">
    <h3>${title}<small>${S.clues.length} ${S.clues.length === 1 ? 'clue' : 'clues'}</small></h3>
    ${groups.map(({g, items}) => `${g ? `<div class="lap" data-key="g-${esc(g)}">${g}</div>` : ''}<ol data-key="o-${esc(g)}">${items.map(line).join('')}</ol>`).join('')}
  </section>`;
}

function spyCount() {
  const n = S.spy_count;
  return `<p class="center dim" data-key="spycount">${cap(UC() ? `${plural(n, 'player has', 'players have')} a different word.` : `${plural(n, 'spy is', 'spies are')} at this table.`)}</p>`;
}

function hostRow(btns, key = 'hostrow') {
  if (!isHost()) return '';
  const list = btns.filter(Boolean);
  return `<div class="btns" data-key="${key}">${list.map(([label, type, cls = 'btn-chalk', ask]) =>
    `<button class="btn ${cls}" onclick="${ask ? `if(confirm('${ask}'))` : ''}send('${type}')">${label}</button>`).join('')}</div>`;
}
const END_ROUND = ['End round', 'lobby', 'btn-quiet', 'End this round and return to the lobby?'];

// ------------------------------------------------------------------ views

function viewJoin() {
  const saved = localStorage.getItem('name') || '';
  return `<div data-key="join">
    <div class="masthead"><h1>Office<span>Spy</span></h1>
      <p>One of you doesn't know the <span class="bar"></span>.</p></div>
    <form class="paper signup drop" onsubmit="event.preventDefault();doJoin()">
      <label for="name">Name for the table</label>
      <input id="name" maxlength="20" autocomplete="off" autocapitalize="words" enterkeyhint="go" placeholder="Your name" value="${esc(saved)}">
      <button class="btn btn-brass">Take a seat</button>
    </form>
    ${S.phase !== 'lobby' ? '<p class="center dim enter-2">A round is underway. You\'ll be dealt in next round.</p>' : ''}
    <button class="btn btn-quiet enter-3" onclick="openHelp()">How to play</button>
  </div>`;
}
// A new device asked to take an offline player's seat mid-round; it waits for the host.
function viewRejoinWait() {
  return `<div data-key="rejoin-wait">
    <div class="masthead"><h1>Office<span>Spy</span></h1></div>
    <section class="paper verdict drop" data-key="rw">
      <span class="stamp blue slam">Checking ID</span>
      <p>Asking the host to let you back in as <b>${esc(S.rejoin_pending)}</b>.
        Your seat holds a secret word, so they have to confirm it's really you.</p>
      <p class="center dim" style="margin:6px 0 0">Waiting for the host<span class="dots"><i></i><i></i><i></i></span></p>
    </section>
    <p class="center dim enter-2">If the round ends first, you'll get your seat back automatically.</p>
    <button class="btn btn-quiet enter-3" onclick="send('cancel_rejoin')">Cancel</button>
  </div>`;
}

// Host only: devices waiting to take back an offline player's seat.
function rejoinRequests() {
  if (!isHost() || !S.rejoin_requests) return '';
  return S.rejoin_requests.map(r => `<section class="rail enter" data-key="rq-${r.id}">
    <h3>${esc(r.name)} wants back in</h3>
    <p class="hint">A new device is asking for ${esc(r.name)}'s seat. Only let it in if ${esc(r.name)} tells you it's
      them, since the seat holds their secret word.</p>
    <div class="btns"><button class="btn btn-brass" data-n="${esc(r.name)}"
        onclick="if(confirm('Is '+this.dataset.n+' really on this new device? They will see their word.'))send('approve_rejoin',{target:'${r.id}'})">Let them in</button>
      <button class="btn btn-chalk" onclick="send('deny_rejoin',{target:'${r.id}'})">Deny</button></div>
  </section>`).join('');
}

function doJoin() {
  const n = $('#name').value.trim();
  if (!n) return toast('Enter a name first.');
  localStorage.setItem('name', n);
  send('join', {name: n});
}

function tray(key, options) {
  const s = S.settings, i = Math.max(0, options.findIndex(([v]) => v === s[key]));
  return `<div class="tray" style="--n:${options.length};--i:${i}" role="group">${options.map(([v, label]) =>
    `<button type="button" aria-pressed="${s[key] === v}" onclick="setS('${key}','${v}')">${label}</button>`).join('')}</div>`;
}
function setS(key, value) {
  if (['spies', 'minutes', 'clue_rounds'].includes(key)) value = +value;
  if (key === 'spy_sees_category') value = !!value;
  send('settings', {settings: {[key]: value}});
}

function setup() {
  const s = S.settings, uc = s.mode === 'undercover';
  const online = S.players.filter(p => p.online).length, need = S.min_players - online;
  if (!isHost()) {
    const bits = [uc ? 'Undercover' : 'Classic', s.clue_mode === 'typed' ? 'typed clues' : 'talking out loud', s.category === 'Random' ? 'random category' : s.category,
      plural(s.spies, uc ? 'undercover player' : 'spy', uc ? 'undercover players' : 'spies'),
      s.clue_mode === 'talk' ? `${s.minutes} minutes` : plural(s.clue_rounds, 'lap', 'laps')];
    return `<section class="rail" data-key="setup-guest"><h3>Tonight's game</h3>
      <p class="hint" style="margin-bottom:0">${esc(bits.join(', '))}.</p>
      <p class="center" style="margin:14px 0 2px">Waiting for the host to deal<span class="dots"><i></i><i></i><i></i></span></p></section>`;
  }
  const cats = S.categories.filter(c => !uc || c.pairs);
  const opt = (vals, cur) => vals.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(l)}</option>`).join('');
  return `<section class="rail" data-key="setup-host"><h3>Deal a game</h3>
    <label class="f">Mode</label>
    ${tray('mode', [['classic', 'Classic'], ['undercover', 'Undercover']])}
    <p class="hint">${uc ? "Some players get a similar word and don't know it. Vote one player out each round."
                         : 'The spy knows they\'re the spy, but not the word. One vote decides it.'}</p>
    <label class="f">Clues</label>
    ${tray('clue_mode', [['talk', 'Talk out loud'], ['typed', 'Type on phones']])}
    <label class="f" for="cat">Category</label>
    <select id="cat" onchange="setS('category', this.value)">${opt([['Random', 'Random'], ...cats.map(c => [c.name, c.custom ? `${c.name} (custom)` : c.name])],
      cats.some(c => c.name === s.category) ? s.category : 'Random')}</select>
    <div class="grid2">
      <div><label class="f" for="spies">${uc ? 'Undercover' : 'Spies'}</label>
        <select id="spies" onchange="setS('spies', this.value)">${opt([[1, '1'], [2, '2'], [3, '3']], s.spies)}</select></div>
      ${s.clue_mode === 'talk'
        ? `<div><label class="f" for="mins">Discussion</label><select id="mins" onchange="setS('minutes', this.value)">${opt([2, 3, 4, 5, 6, 8, 10].map(m => [m, `${m} min`]), s.minutes)}</select></div>`
        : `<div><label class="f" for="laps">Clue laps</label><select id="laps" onchange="setS('clue_rounds', this.value)">${opt([[1, '1 lap'], [2, '2 laps'], [3, '3 laps']], s.clue_rounds)}</select></div>`}
    </div>
    ${uc ? '' : `<label class="switch">Spy sees the category<input type="checkbox" ${s.spy_sees_category ? 'checked' : ''} onchange="setS('spy_sees_category', this.checked)"></label>`}
    <button class="btn btn-brass" style="margin-top:18px" ${need > 0 ? 'disabled' : ''} onclick="send('start')">
      ${need > 0 ? `Waiting for ${plural(need, 'more player', 'more players')}` : 'Deal the cards'}</button>
  </section>`;
}

function hostTools() {
  if (!isHost()) return '';
  const custom = S.categories.filter(c => c.custom);
  return `<details class="rail" id="tools" data-key="tools"><summary>Host tools</summary><div class="inner">
    <h3 style="margin-top:16px">Your word packs</h3>
    ${custom.length ? custom.map(c => `<div class="pack" data-key="pk-${esc(c.name)}"><span>${esc(c.name)} <span class="badge">${c.words} words</span></span>
        <button class="iconbtn" aria-label="Delete ${esc(c.name)}" data-n="${esc(c.name)}" onclick="if(confirm('Delete this word pack?'))send('delete_pack',{name:this.dataset.n})">✕</button></div>`).join('')
      : '<p class="hint">Make a pack from office in-jokes, team names or places near the office.</p>'}
    <input class="field" id="packname" maxlength="30" placeholder="Pack name, like Our Office">
    <textarea class="field" id="packwords" placeholder="At least 6 words, separated by commas or new lines"></textarea>
    <button class="btn btn-chalk" onclick="addPack()">Add word pack</button>
    <p class="hint">Packs are saved on the host's computer and work in Classic mode.</p>
    <h3 style="margin-top:18px">Scoreboard</h3>
    <button class="btn btn-chalk" onclick="if(confirm('Reset every score to zero?'))send('reset_scores')">Reset scores</button>
  </div></details>`;
}
function addPack() {
  send('add_pack', {name: $('#packname').value, words: $('#packwords').value});
  ui.packSent = $('#packname').value.trim();
}

function viewLobby() {
  return `<div data-key="v-lobby">${setup()}
    <section class="rail" data-key="players"><h3>At the table <small>${plural(S.players.length, 'player', 'players')}</small></h3>${roster({kick: true})}</section>
    ${invite()}${hostTools()}
    <button class="btn btn-quiet" onclick="if(confirm('Leave the table?'))send('leave')">Leave the table</button></div>`;
}

function watching(title, note, viewKey) {
  const typed = S.clue_mode === 'typed' && S.phase === 'playing';
  return `<div class="headline enter" data-key="watch"><h2>${title}</h2><p>${note}</p></div>
    ${typed && S.current_turn ? `<div class="paper waiting enter-2" data-key="wait-${S.current_turn}">${chip(S.current_turn)}<span class="typed" style="color:var(--ink)"><b>${nameOf(S.current_turn)}</b> is writing<span class="dots"><i></i><i></i><i></i></span></span></div>` : ''}
    ${clock()}${clueLog(viewKey)}`;
}

function viewPlaying(vk) {
  const me = S.you;
  if (!me.in_round) return watching('Round underway', "You'll be dealt in next round.", vk);
  if (!me.alive) return watching("You're out", 'Watch the rest quietly. No hints.', vk) + slip();

  if (S.clue_mode === 'talk') {
    return `${caseFile()}${clock()}${spyCount()}
      <section class="rail" data-key="order"><h3>Speaking order</h3>
        <p class="hint">${nameOf(S.order[0])} starts. Ask someone a question about the word, or give a one-word clue, then pass it on.</p>
        <ul class="roster">${S.order.map((id, i) => `<li data-key="o-${id}">${chip(id, 'sm')}<span class="nm">${nameOf(id)}</span><span class="badge">${i + 1}</span></li>`).join('')}</ul></section>
      ${hostRow([['Start the vote', 'start_vote', 'btn-brass']])}${hostRow([END_ROUND], 'endrow')}`;
  }
  const mine = S.current_turn === me.id;
  const lap = S.clue_rounds > 1 ? `Lap ${num(S.clue_round)} of ${num(S.clue_rounds)}. ` : '';
  const turn = mine
    ? `<section class="paper turn mine" data-key="turn-me-${S.clues.length}">
        <h2>Your turn</h2><p class="meta">${lap}Give a clue about your word. Don't use the word itself.</p>
        <form class="cluefield" onsubmit="event.preventDefault();sendClue()">
          <input id="clue" maxlength="30" autocomplete="off" enterkeyhint="send" placeholder="Your clue" aria-label="Your clue">
          <button class="btn btn-brass">Send</button></form>
        ${clock('sm')}</section>`
    : `<section class="paper waiting" data-key="wait-${S.current_turn}">${chip(S.current_turn)}
        <span class="typed" style="color:var(--ink);flex:1">${lap}<b>${nameOf(S.current_turn)}</b> is writing<span class="dots"><i></i><i></i><i></i></span></span>${clock('sm')}</section>`;
  return `${caseFile()}${turn}${clueLog(vk)}${spyCount()}
    ${hostRow([!mine && ['Skip turn', 'skip_turn'], ['Vote now', 'start_vote']])}${hostRow([END_ROUND], 'endrow')}`;
}
function sendClue() {
  const el = $('#clue');
  if (!el.value.trim()) return toast('Type a clue first.');
  send('clue', {text: el.value});
}

function viewVoting(vk) {
  const me = S.you;
  const tally = `<div class="tally" data-key="tally" aria-label="${S.voted.length} of ${S.alive.length} voted">${S.alive.map((id, i) => `<i class="${i < S.voted.length ? 'on' : ''}"></i>`).join('')}</div>
    <p class="center dim" style="font-size:16px">${cap(num(S.voted.length))} of ${num(S.alive.length)} voted${S.voted.length < S.alive.length ? `, waiting for ${S.alive.filter(id => !S.voted.includes(id)).map(nameOf).join(', ')}` : ''}</p>`;
  if (!me.in_round || !me.alive) return `<div class="headline" data-key="vh"><h2>The table is voting</h2></div>${clock('sm')}${tally}${clueLog(vk, 'Clues given')}`;
  return `<div class="headline" data-key="vh"><h2>${UC() ? 'Who has a different word?' : 'Who is the spy?'}</h2>
      <p>${S.my_vote ? 'Vote cast. You can change it until everyone has voted.' : 'Tap a player to cast your vote.'}</p></div>
    ${clock('sm')}
    <div class="ballot" data-key="ballot">${S.alive.filter(id => id !== me.id).map(id =>
      `<button data-key="b-${id}" aria-pressed="${S.my_vote === id}" onclick="send('vote',{target:'${id}'})">${chip(id, 'lg')}${nameOf(id)}</button>`).join('')}</div>
    ${tally}${slip()}${clueLog(vk, 'Clues given')}
    ${hostRow([['End the vote', 'end_vote']])}${hostRow([END_ROUND], 'endrow')}`;
}

function votesCard(votes) {
  const t = {};
  Object.entries(votes || {}).forEach(([v, target]) => (t[target] = t[target] || []).push(v));
  const rows = Object.entries(t).sort((a, b) => b[1].length - a[1].length);
  if (!rows.length) return '';
  return `<section class="rail" data-key="votes"><h3>How the table voted</h3><ul class="roster">${rows.map(([target, vs], i) =>
    `<li class="enter" style="animation-delay:${i * 60}ms" data-key="vt-${target}">${chip(target)}<span class="nm">${nameOf(target)}<small>${vs.map(nameOf).join(', ')}</small></span><span class="pts">${vs.length}</span></li>`).join('')}</ul></section>`;
}

function viewReveal(vk) {
  let stamp, line;
  if (S.tie || !S.last_out) {
    stamp = '<span class="stamp blue slam">No majority</span>';
    line = 'The vote tied, so nobody leaves. Another round of clues.';
  } else {
    const was = S.eliminated.find(e => e.pid === S.last_out);
    stamp = was.undercover ? '<span class="stamp red slam">Undercover</span>' : '<span class="stamp blue slam">Civilian</span>';
    line = was.undercover ? `${nameOf(S.last_out)} had the different word. Is anyone else still hiding?`
                          : `${nameOf(S.last_out)} was innocent. The undercover player is still at the table.`;
  }
  const who = S.last_out && !S.tie ? `<div class="agents" style="margin:0 0 14px">${`<span class="agent">${chip(S.last_out, 'sm')}${nameOf(S.last_out)} is out</span>`}</div>` : '';
  return `<section class="paper verdict drop" data-key="verdict-${S.discussion}">${who}${stamp}<p>${line}</p></section>
    ${clock('sm')}<p class="center dim" style="font-size:16px">The next round of clues starts automatically.</p>
    ${hostRow([['Continue now', 'continue', 'btn-brass']])}
    ${votesCard(S.votes)}${S.you && S.you.alive ? slip() : ''}${clueLog(vk)}`;
}

function viewGuess() {
  const name = nameOf(S.accused);
  if (S.guess_options) {
    return `<section class="paper verdict drop" data-key="lastchance">
      <span class="stamp red slam">${S.self_reveal ? 'Your guess' : 'Caught'}</span>
      <p>${S.self_reveal ? 'Name the secret word. Get it right and the spy wins.' : 'One chance left. Name the secret word and you still win.'}</p>
      ${clock('sm')}
      <div class="choices">${S.guess_options.map(w =>
        `<button data-w="${esc(w)}" onclick="if(confirm('Lock in “'+this.dataset.w+'”?'))send('guess',{word:this.dataset.w})">${esc(w)}</button>`).join('')}</div></section>`;
  }
  return `<section class="paper verdict drop" data-key="accused">
      <div class="agents" style="margin:0 0 14px"><span class="agent spy">${chip(S.accused, 'sm')}${name}</span></div>
      <span class="stamp red slam">${S.self_reveal ? 'Going for it' : 'Spy found'}</span>
      <p>${S.self_reveal ? `${name} says they know the word and is guessing now.` : `${name} gets one last chance. If they name the word, the spy still wins.`}</p></section>
    ${clock()}`;
}

function viewResults(vk) {
  const uc = UC(), spiesWon = S.winner === 'spies';
  const stampText = uc ? (spiesWon ? 'Undercover wins' : 'Case closed') : (spiesWon ? 'Spy escapes' : 'Spy caught');
  const words = uc
    ? `<div class="pair"><div><p class="meta">Civilians had</p><div class="word">${esc(S.word)}</div></div>
        <div><p class="meta">Undercover had</p><div class="word">${esc(S.spy_word)}</div></div></div>`
    : `<p class="meta">The word was</p><div class="word">${esc(S.word)}</div>${S.guess ? `<p class="meta">The spy guessed “${esc(S.guess)}”</p>` : ''}`;
  const agents = S.spies.map(id => `<span class="agent spy">${chip(id, 'sm')}${nameOf(id)}</span>`).join('');
  const out = uc && S.eliminated.length ? `<section class="rail" data-key="out"><h3>Voted out, in order</h3><ul class="roster">${S.eliminated.map((e, i) =>
    `<li data-key="e-${e.pid}"><span class="medal">${i + 1}</span>${chip(e.pid, 'sm')}<span class="nm">${nameOf(e.pid)}</span><span class="badge">${e.undercover ? 'undercover' : 'civilian'}</span></li>`).join('')}</ul></section>` : '';
  const sorted = [...S.players].sort((a, b) => b.score - a.score || b.wins - a.wins);
  const board = `<section class="rail" data-key="board"><h3>Scoreboard</h3><ul class="roster board">${sorted.map((p, i) =>
    `<li class="enter" style="--i:${i}" data-key="s-${p.id}"><span class="medal">${i + 1}</span>${chip(p.id)}<span class="nm">${esc(p.name)}<small>won ${num(p.wins)} of ${num(p.games)}</small></span><span class="pts">${p.score}</span></li>`).join('')}</ul></section>`;
  const controls = isHost()
    ? `<div data-key="ctl"><button class="btn btn-brass" onclick="send('start')">Deal again</button><button class="btn btn-chalk" onclick="send('lobby')">Change settings</button></div>`
    : (S.you ? '<p class="center dim" data-key="ctl">Waiting for the host to deal again<span class="dots"><i></i><i></i><i></i></span></p>' : '');
  return `<section class="paper verdict drop" data-key="verdict-final">
      <span class="stamp ${spiesWon ? 'red' : 'blue'} slam">${stampText}</span><p>${esc(S.reason)}</p></section>
    <section class="paper declass enter-2" data-key="declass">${words}<p class="meta">${esc(S.category)}</p><div class="agents">${agents}</div></section>
    ${controls}${out}${uc ? '' : votesCard(S.votes)}${board}${clueLog(vk, 'Clues given')}`;
}

// Shared TV / projector: never shows a word or role until the results.
function viewScreen(vk) {
  const ph = S.phase;
  const bar = `<header class="top" data-key="top"><span class="signal ${connected ? '' : 'off'}"></span>
    <div class="op" style="flex:1">${ph === 'lobby' ? '<b>Office Spy</b>' : `<b>${esc(S.codename)}</b><small class="typed">${fileNo()}</small>`}</div>
    <button class="iconbtn" aria-label="Sound" onclick="Sound.toggle();render()">${Sound.muted ? ICON.mute : ICON.sound}</button></header>`;
  if (ph === 'lobby') {
    return `${bar}<div class="screen-lobby" data-key="sl"><img class="qr" src="/qr.svg" alt="" onerror="this.remove()">
      <div><h1>Office<span>Spy</span></h1><p class="typed dim" style="font-size:26px">Join on the same Wi-Fi:</p>
      <p class="typed" style="font-size:34px;color:var(--brass);font-weight:700">${esc(S.join_url)}</p>
      <div class="table-chips">${S.players.map(p => `<span class="agent enter" data-key="tc-${p.id}">${chip(p.id)}${esc(p.name)}</span>`).join('')
        || '<span class="dim typed">The table is empty. Be the first to sit down.</span>'}</div></div></div>`;
  }
  if (ph === 'playing') {
    const left = S.clue_mode === 'typed'
      ? `<div class="paper waiting" data-key="wait-${S.current_turn}" style="font-size:28px">${chip(S.current_turn, 'lg')}<span class="typed" style="color:var(--ink)"><b>${nameOf(S.current_turn)}</b> is writing<span class="dots"><i></i><i></i><i></i></span></span></div>${clock()}`
      : `${clock()}<section class="rail" data-key="order"><h3>Speaking order</h3><div class="table-chips">${S.order.map(id => `<span class="agent" data-key="so-${id}">${chip(id)}${nameOf(id)}</span>`).join('')}</div></section>`;
    return `${bar}${spyCount()}<div class="screen-grid" data-key="sg"><div>${left}</div><div>${clueLog(vk) || ''}</div></div>`;
  }
  if (ph === 'voting') {
    return `${bar}<div class="headline" data-key="vh"><h2 style="font-size:72px">The table is voting</h2></div>
      <div class="screen-grid" data-key="sg"><div>${clock()}<div class="tally" data-key="tally">${S.alive.map((id, i) => `<i class="${i < S.voted.length ? 'on' : ''}" style="width:22px;height:22px"></i>`).join('')}</div></div><div>${clueLog(vk)}</div></div>`;
  }
  if (ph === 'reveal') return bar + viewReveal(vk);
  if (ph === 'guess') return bar + viewGuess();
  return bar + viewResults(vk);
}

// ------------------------------------------------------------------ help sheet

function openHelp() {
  const el = $('#sheet');
  el.innerHTML = `<div class="paper" role="dialog" aria-modal="true" aria-labelledby="help-title" onclick="event.stopPropagation()">
    <div class="grip"></div><h2 id="help-title">How to play</h2>
    <h3>Classic</h3>
    <ol><li>Everyone gets the same secret word, except the spy, who only learns they're the spy.</li>
      <li>Take turns asking questions or giving clues. Show you know the word without giving it away.</li>
      <li>Vote for the player you think is the spy.</li>
      <li>A caught spy gets one guess at the word. If they get it right, the spy still wins.</li>
      <li>The spy can tap "I know the word" at any time to guess early.</li></ol>
    <h3>Undercover</h3>
    <ol><li>Most players share a word. The undercover players get a similar one, like Momo and Samosa, and don't know theirs is different.</li>
      <li>Give clues, then vote someone out. Their role is revealed.</li>
      <li>Civilians win once every undercover player is out. Undercover wins by lasting until it's one on one.</li></ol>
    <h3>Typed clues</h3>
    <p>When it's your turn, type a short clue on your phone. It shows up in the transmission log for everyone. A clue can't contain your own word.</p>
    <h3>Points</h3>
    <p>Winning civilians get 1 point each. Winning spies and undercover players get 2.</p>
    <button class="btn btn-brass" onclick="closeHelp()">Back to the table</button></div>`;
  el.hidden = false;
  requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('show')));
  el.onclick = closeHelp;
}
function closeHelp() {
  const el = $('#sheet');
  el.classList.remove('show');
  setTimeout(() => (el.hidden = true), REDUCED ? 0 : 420);
}
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#sheet').hidden) closeHelp(); });

// ------------------------------------------------------------------ render

function build() {
  if (removed) return {key: 'removed', html: `<div class="masthead" data-key="removed"><h1>Office<span>Spy</span></h1>
    <p>You've left the table.</p></div><button class="btn btn-brass" onclick="location.reload()">Sit back down</button>`};
  if (!S) return {key: 'connecting', html: '<p class="center dim typed" style="margin-top:40vh">Connecting to the table…</p>'};
  if (IS_SCREEN) {
    const key = `screen-${S.phase}-${S.round}`;
    return {key, html: viewScreen(key)};
  }
  if (!S.you) return S.rejoin_pending ? {key: 'rejoin', html: viewRejoinWait()} : {key: 'join', html: viewJoin()};
  const key = `${S.phase}-${S.round}`;
  const views = {lobby: viewLobby, playing: viewPlaying, voting: viewVoting, reveal: viewReveal, guess: viewGuess, results: viewResults};
  return {key, html: topBar() + `<main data-key="${key}">${rejoinRequests()}${views[S.phase](key)}</main>`};
}

function render() {
  const app = $('#app');
  const {key, html} = build();
  const next = document.createElement('div');
  next.innerHTML = html;
  const apply = () => { morphChildren(app, next); afterRender(key); };
  const phaseChanged = key !== lastViewKey && lastViewKey !== '';
  lastViewKey = key;
  if (phaseChanged && document.startViewTransition && !REDUCED) document.startViewTransition(apply);
  else apply();
}

function afterRender(key) {
  if (S) {
    clueMark = {view: key, count: (S.clues || []).length};
    if (ui.packSent && S.categories.some(c => c.custom && c.name === ui.packSent)) {
      const n = $('#packname'), w = $('#packwords');
      if (n) n.value = '';
      if (w) w.value = '';
      toast(`Added the ${ui.packSent} pack.`, true);
      ui.packSent = false;
    }
    lastPhase = S.phase;
  }
  tick();
}

function tick() {
  const el = $('#clock');
  if (!el || !S) return;
  const left = clockNow();
  if (left == null) return;
  el.querySelector('.n').textContent = fmt(left);
  const low = left <= (S.seconds_total > 60 ? 30 : 10);
  el.classList.toggle('low', low);
  if (S.seconds_total) el.querySelector('.fuse i').style.width = `${(left / S.seconds_total) * 100}%`;
  // Ticking in the last 10 seconds, only on the screen and the phone whose turn it is.
  const sec = Math.ceil(left), mine = S.you && (S.current_turn === S.you.id || (S.phase === 'guess' && S.accused === S.you.id));
  if (sec !== lastTickSec) {
    lastTickSec = sec;
    if (sec > 0 && sec <= 10 && (IS_SCREEN || mine)) Sound.fx.tick();
  }
}
setInterval(tick, 250);

render();
connect();
