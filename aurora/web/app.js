const SB_KEY = window.AURORA_SB_KEY || 'aurora_web_sidebar';
const $ = id => document.getElementById(id);
new ResizeObserver(() => document.documentElement.style.setProperty('--hh', document.querySelector('header').offsetHeight + 'px')).observe(document.querySelector('header'));
const log = $('log');
let lastId = 0, cur = null, curThink = null, tools = [], pendingApproval = null;   // tools: calls still waiting for a result
let thinkT0 = 0, thinkSeen = 0, thinkTimer = 0;   // server time of the first think chunk / local arrival (live counter)
const secs = s => s < 60 ? Math.max(1, Math.round(s)) + 's' : Math.floor(s / 60) + 'm ' + Math.round(s % 60) + 's';
// What Aurora is doing right now, shown under the log and in the status tag, so a hidden tool call or a long think never looks like a hang.
let liveLabel = '', liveSince = 0, liveTimer = 0, liveOpen = false, thinkText = '', turnTools = [];   // this turn's thinking and tool lines, for the tap-to-expand panel
function paintThink() {
  const pre = $('live-think'); pre.hidden = !liveOpen; $('live-line').setAttribute('aria-expanded', String(liveOpen));
  if (!liveOpen) return;
  const parts = [];
  if (thinkText) parts.push('THINKING\n' + thinkText);
  if (turnTools.length) parts.push('COMMANDS\n' + turnTools.join('\n'));
  pre.textContent = parts.join('\n\n') || 'Nothing yet.'; pre.scrollTop = pre.scrollHeight;
}
function toggleLive() { if ($('live').hidden) return; liveOpen = !liveOpen; paintThink(); if (liveOpen) $('live').scrollIntoView({ block: 'nearest' }); }
const statusTap = () => { if ($('live').hidden) post('/api/wake', {}); else toggleLive(); };   // R426: "idle" wakes a sleeping model
$('live-line').onclick = toggleLive; $('status').onclick = statusTap;
for (const [e, f] of [[$('live-line'), toggleLive], [$('status'), statusTap]]) e.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); f(); } };
$('status').setAttribute('role', 'button'); $('status').tabIndex = 0;
function paintLive() {
  const t = liveLabel + ' ' + secs((Date.now() - liveSince) / 1000);   // the counter runs for every state, so a long silent wait (a big prompt being read) never looks frozen
  $('live-line').textContent = t; $('status').textContent = t;
}
function live(txt) {
  if (!txt) { clearInterval(liveTimer); liveLabel = ''; $('live').hidden = true; return; }
  $('live').hidden = false; $('status').classList.add('busy');
  if (txt !== liveLabel) { liveLabel = txt; liveSince = Date.now(); }
  clearInterval(liveTimer); liveTimer = setInterval(paintLive, 1000); paintLive();
}
function endThink(t) {
  if (!curThink) return;
  clearInterval(thinkTimer);
  curThink.querySelector('summary').textContent = 'thought for ' + secs(Math.max(0, t - thinkT0));
  curThink = null;
}

// Token problems must never be silent: any 401 from the API shows a banner at the top of the page (not just a note in the log).
(function () {
  const _fetch = window.fetch.bind(window);
  function banner(msg) {
    let b = document.getElementById('token-banner');
    if (!b) {
      // Fixed, not a body child in the flow: the / page's body is a flex ROW, so a prepended block became a
      // full-height red column beside the sidebar (the whole chat pane on a phone). A one/two-line strip
      // across the top instead, under the notch (safe-area), with a close button.
      b = document.createElement('div'); b.id = 'token-banner'; b.setAttribute('role', 'alert');
      b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;display:flex;align-items:flex-start;gap:8px;'
        + 'padding:calc(env(safe-area-inset-top,0px) + 8px) 12px 8px;background:#b3261e;color:#fff;'
        + 'font:600 14px/1.3 system-ui,sans-serif;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.35);'
        + 'max-height:30vh;overflow:auto';
      const text = document.createElement('span'); text.id = 'token-banner-text'; text.style.flex = '1 1 auto';
      const close = document.createElement('button'); close.type = 'button'; close.textContent = '×';
      close.setAttribute('aria-label', 'Dismiss');
      close.style.cssText = 'flex:none;background:none;border:0;color:#fff;font:600 22px/1 system-ui,sans-serif;padding:0 4px;cursor:pointer';
      close.onclick = e => { e.stopPropagation(); b.remove(); };
      b.append(text, close);
      b.onclick = () => { if (window.openSettings) window.openSettings('token'); else { const t = document.getElementById('token'); if (t) t.focus(); } };
      document.body.append(b);
    }
    b.querySelector('#token-banner-text').textContent = msg + ' Tap to enter it.';
  }
  window.fetch = async (...a) => {
    const r = await _fetch(...a);
    if (r.status === 401 && String(a[0]).includes('/api/')) {
      const has = !!localStorage.getItem('aurora_web_token');
      banner(has ? 'The server REJECTED the saved access token — paste the correct one in Settings and press Save.'
                 : 'No access token is saved in this browser — paste it in Settings and press Save. Nothing was loaded or sent.');
    }
    return r;
  };
})();
const getToken = () => localStorage.getItem('aurora_web_token') || '';
$('token').value = getToken();
// R450 (S11): chats and notes cached for offline reading are dropped when the token changes or is refused
async function clearOfflineData() {
  try {
    if (!window.caches) return;
    const c = await caches.open('aurora-web-v1');
    for (const k of await c.keys()) if (new URL(k.url).pathname.startsWith('/api/')) await c.delete(k);
  } catch (e) {}
}
$('token-save').onclick = async () => {
  const v = $('token').value.trim();
  if (v !== getToken()) await clearOfflineData();
  localStorage.setItem('aurora_web_token', v); location.reload();
};
const hdr = () => ({ 'Authorization': 'Bearer ' + getToken(), 'Content-Type': 'application/json' });
async function post(path, body) {
  let r;
  try { r = await fetch(path, { method: 'POST', headers: hdr(), body: JSON.stringify(body || {}) }); }
  catch (e) { netState('offline'); note('offline: not sent (it will work again when the connection is back)', true); return false; }   // R396: was an uncaught rejection, nothing on screen
  netState('ok');
  const d = await r.json().catch(() => ({}));
  if (!r.ok) note((d.error || 'HTTP ' + r.status), true);
  return r.ok;
}

function note(text, err) { add('msg note' + (err ? ' err' : ''), text); }
function add(cls, text) {
  const d = document.createElement('div'); d.className = cls; d.textContent = text;
  log.append(d); scroll(); return d;
}
// v1 scrolls the page; /v2 scrolls the log box (AURORA_LOG_SCROLLS) and keeps following the end only while the reader is at the end.
let stick = true;
log.addEventListener('scroll', () => { stick = log.scrollHeight - log.scrollTop - log.clientHeight < 80; const j = document.getElementById('jump'); if (j) j.hidden = stick; }, { passive: true });
function scroll() {
  if (!window.AURORA_LOG_SCROLLS) { window.scrollTo({ top: document.body.scrollHeight }); return; }
  if (stick) log.scrollTop = log.scrollHeight;
}

// Fences and `code` only: the rest stays plain text, nothing from the model is ever HTML.
function render(el, raw) {
  el.textContent = '';
  raw.split(/(```[\s\S]*?(?:```|$))/g).forEach(part => {
    if (part.startsWith('```')) {
      const pre = document.createElement('pre'), code = document.createElement('code');
      code.textContent = part.replace(/^```[^\n]*\n?/, '').replace(/```$/, '');
      pre.append(code); el.append(pre);
    } else part.split(/(`[^`\n]+`)/g).forEach(p => {
      if (/^`[^`\n]+`$/.test(p)) { const c = document.createElement('code'); c.textContent = p.slice(1, -1); el.append(c); }
      else el.append(document.createTextNode(p));
    });
  });
}
// R439: Markdown, a CommonMark/GFM subset built with DOM nodes only (textContent), so nothing from the model or a
// note can become HTML or script; links only http(s) and mailto. Blocks: ATX and === headings, ``` and ~~~ fences,
// indented code, quotes holding any block, nested/loose/ordered(start)/task lists, GFM tables (alignment, \|, code
// with |), rules. Inline: code spans of any backtick run, ***strong em***, ** __ * _ ~~, links with titles, images
// as links, <autolinks>, bare URLs, <br>, backslash escapes, entities. A failure shows the raw text instead.
const MD_DEPTH = 16, MD_URL = /^(https?:\/\/|mailto:)/i;
const MD_ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' };
function mdEnt(s) {
  return s.indexOf('&') < 0 ? s : s.replace(/&(#x[0-9a-f]{1,6}|#[0-9]{1,7}|[a-z]{2,6});/gi, (m, n) => {
    const k = n.toLowerCase();
    if (k[0] !== '#') return MD_ENT[k] || m;
    const c = k[1] === 'x' ? parseInt(k.slice(2), 16) : parseInt(k.slice(1), 10);
    return c > 0 && c < 0x110000 ? String.fromCodePoint(c) : m;
  });
}
function inline(parent, t, depth, noLink) {
  depth = depth || 0;
  if (depth > MD_DEPTH) { parent.append(t); return; }
  let buf = '', i = 0;
  const flush = () => { if (buf) { parent.append(mdEnt(buf)); buf = ''; } };
  const at = (re, k) => { re.lastIndex = k; return re.exec(t); };
  const add = el => { flush(); parent.append(el); return el; };
  while (i < t.length) {
    const c = t[i], prev = i ? t[i - 1] : ' ';
    if (c === '\\' && i + 1 < t.length) {
      if (t[i + 1] === '\n') { add(document.createElement('br')); i += 2; continue; }
      if (/[!-\/:-@\[-`{-~]/.test(t[i + 1])) { buf += t[i + 1]; i += 2; continue; }
    }
    if (c === '`') {
      const run = at(/`+/y, i)[0];
      let j = i + run.length, end = -1;
      while ((j = t.indexOf(run, j)) >= 0) { if (t[j + run.length] !== '`' && t[j - 1] !== '`') { end = j; break; } j += run.length; while (t[j] === '`') j++; }
      if (end < 0) { buf += run; i += run.length; continue; }
      let code = t.slice(i + run.length, end).replace(/\n/g, ' ');
      if (/^ .*[^ ].* $/.test(code)) code = code.slice(1, -1);
      const el = document.createElement('code'); el.textContent = code; add(el); i = end + run.length; continue;
    }
    if (c === '<') {
      const al = at(/<((?:https?:\/\/|mailto:)[^\s<>]+)>/iy, i);
      if (al && !noLink) { const a = document.createElement('a'); a.href = al[1]; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.textContent = al[1]; add(a); i += al[0].length; continue; }
      const br = at(/<br\s*\/?>/iy, i);
      if (br) { add(document.createElement('br')); i += br[0].length; continue; }
    }
    if ((c === '[' || (c === '!' && t[i + 1] === '[')) && !noLink) {
      const img = c === '!', s0 = i + (img ? 1 : 0);
      let k = s0 + 1, lvl = 1;
      for (; k < t.length && lvl; k++) { if (t[k] === '\\') { k++; continue; } if (t[k] === '[') lvl++; else if (t[k] === ']') lvl--; }
      const close = k - 1;
      if (!lvl && t[k] === '(') {
        let m = k + 1, pl = 1;
        for (; m < t.length && pl; m++) { if (t[m] === '\\') { m++; continue; } if (t[m] === '(') pl++; else if (t[m] === ')') pl--; if (t[m] === '\n' && t[m - 1] === '\n') break; }
        if (!pl) {
          const label = t.slice(s0 + 1, close), inner = t.slice(k + 1, m - 1).trim();
          const um = /^(?:<([^>]*)>|(\S+))(?:\s+(?:"([^"]*)"|'([^']*)'|\(([^)]*)\)))?$/.exec(inner) || [];
          const url = (um[1] != null ? um[1] : um[2] || '').replace(/\\(.)/g, '$1'), title = um[3] || um[4] || um[5] || '';
          if (MD_URL.test(url)) {
            const a = document.createElement('a'); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; if (title) a.title = title;
            if (img) { a.className = 'md-img'; a.textContent = '🖼 ' + (label || 'image'); } else inline(a, label, depth + 1, true);
            add(a);
          } else { flush(); inline(parent, label, depth + 1, true); }   // an unsafe or empty link keeps its words, not its target
          i = m; continue;
        }
      }
    }
    if (c === '*' || c === '_' || c === '~') {
      const run = at(c === '*' ? /\*+/y : c === '_' ? /_+/y : /~+/y, i)[0], next = t[i + run.length] || ' ';
      const n = c === '~' ? (run.length === 2 ? 2 : 0) : Math.min(run.length, 3);
      const canOpen = n && !/\s/.test(next) && (c !== '_' || !/[\p{L}\p{N}]/u.test(prev));
      if (canOpen && run.length === n) {
        const d = c.repeat(n);
        let j = i + n, end = -1;
        while ((j = t.indexOf(d, j)) >= 0) {
          const after = t[j + n] || ' ', before = t[j - 1];
          if (j > i + n && !/\s/.test(before) && after !== c && t[j - 1] !== c && (c !== '_' || !/[\p{L}\p{N}]/u.test(after))) { end = j; break; }
          j += 1;
        }
        if (end > 0) {
          const inner = t.slice(i + n, end);
          let el, host;
          if (c === '~') el = host = document.createElement('del');
          else if (n === 1) el = host = document.createElement('em');
          else if (n === 2) el = host = document.createElement('strong');
          else { el = document.createElement('strong'); host = document.createElement('em'); el.append(host); }
          inline(host, inner, depth + 1, noLink); add(el); i = end + n; continue;
        }
      }
      buf += run; i += run.length; continue;
    }
    if (!noLink && (c === 'h' || c === 'w') && !/[\p{L}\p{N}\/]/u.test(prev)) {
      const u = at(/(?:https?:\/\/|www\.)[^\s<>"'`]+/iy, i);
      if (u) {
        let url = u[0];
        while (/[.,;:!?*_~'"]$/.test(url) || (url.endsWith(')') && (url.match(/\(/g) || []).length < (url.match(/\)/g) || []).length)) url = url.slice(0, -1);
        if (/^www\./i.test(url) ? /^www\.[^.\/]+\.[a-z]{2,}/i.test(url) : url.length > 10) {
          const a = document.createElement('a'); a.href = /^www\./i.test(url) ? 'https://' + url : url; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.textContent = url;
          add(a); i += url.length; continue;
        }
      }
    }
    if (c === ' ' && t.startsWith('  \n', i)) { add(document.createElement('br')); i += 3; while (t[i] === ' ') i++; continue; }   // two spaces: a hard break
    buf += c; i++;
  }
  flush();
}
const mdInd = l => { let w = 0; for (const ch of l) { if (ch === ' ') w++; else if (ch === '\t') w += 4 - (w % 4); else break; } return w; };
function mdStrip(l, n) {   // drop n columns of leading white space
  let w = 0, k = 0;
  while (k < l.length && w < n && (l[k] === ' ' || l[k] === '\t')) { w += l[k] === '\t' ? 4 - (w % 4) : 1; k++; }
  return (w > n ? ' '.repeat(w - n) : '') + l.slice(k);
}
const MD_FENCE = /^ {0,3}(`{3,}|~{3,})[ \t]*([^\s`]*)[^`]*$/, MD_HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/, MD_ATX = /^ {0,3}(#{1,6})(?:[ \t]+|$)(.*?)(?:[ \t]+#+)?[ \t]*$/,
      MD_LI = /^([ \t]*)([-*+]|(\d{1,9})([.)]))([ \t]+|$)(.*)$/, MD_QUOTE = /^ {0,3}>/;
function mdCells(l) {   // split a table row on | outside `code` and not \|
  const out = []; let cell = '', tick = 0;
  let s = l.trim().replace(/^\|/, '');
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);   // no lookbehind: older Safari
  for (let k = 0; k < s.length; k++) {
    const ch = s[k];
    if (ch === '\\' && s[k + 1] === '|') { cell += '|'; k++; continue; }
    if (ch === '`') { let r = 0; while (s[k + r] === '`') r++; tick = tick === r ? 0 : tick || r; cell += s.slice(k, k + r); k += r - 1; continue; }
    if (ch === '|' && !tick) { out.push(cell.trim()); cell = ''; continue; }
    cell += ch;
  }
  out.push(cell.trim()); return out;
}
const mdDelim = l => /\|/.test(l) || /^\s*:?-+:?\s*$/.test(l) ? mdCells(l) : null;
function mdTableAt(lines, k) {
  if (k + 1 >= lines.length || !/\|/.test(lines[k]) || mdInd(lines[k]) > 3) return false;
  const d = mdDelim(lines[k + 1]);
  return !!d && d.length === mdCells(lines[k]).length && d.every(x => /^:?-+:?$/.test(x));   // GFM: one delimiter per header cell
}
function mdStarts(lines, k, inPara) {   // does a new block start on line k (so a paragraph ends there)?
  const l = lines[k];
  if (MD_FENCE.test(l) || MD_ATX.test(l) || MD_HR.test(l) || MD_QUOTE.test(l) || mdTableAt(lines, k)) return true;
  const m = MD_LI.exec(l);
  return !!m && mdInd(l) < 4 && m[6].trim() !== '' && (!inPara || !m[3] || m[3] === '1');
}
function mdBlocks(parent, lines, base, opts, depth) {
  if (depth > MD_DEPTH) { const p = document.createElement('p'); p.textContent = lines.join('\n'); parent.append(p); return; }
  const mk = (tag, text) => { const e = document.createElement(tag); if (text != null) inline(e, text); parent.append(e); return e; };
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^\s*$/.test(l)) { i++; continue; }
    let m;
    if ((m = MD_FENCE.exec(l))) {
      const fi = mdInd(l), mark = m[1], buf = [];
      const closeRe = new RegExp('^ {0,3}' + (mark[0] === '`' ? '`' : '~') + '{' + mark.length + ',}[ \\t]*$');
      i++;
      while (i < lines.length && !closeRe.test(lines[i])) buf.push(mdStrip(lines[i++], fi));
      i++;
      const pre = mk('pre'), code = document.createElement('code'); code.textContent = buf.join('\n');
      const lang = (m[2] || '').replace(/[^\w+#.-]/g, ''); if (lang) { code.className = 'language-' + lang; pre.dataset.lang = lang; }
      pre.append(code); continue;
    }
    if (mdInd(l) >= 4) {   // indented code
      const buf = [];
      while (i < lines.length && (mdInd(lines[i]) >= 4 || /^\s*$/.test(lines[i]))) buf.push(mdStrip(lines[i++], 4));
      while (buf.length && !buf[buf.length - 1].trim()) buf.pop();
      const pre = mk('pre'), code = document.createElement('code'); code.textContent = buf.join('\n'); pre.append(code); continue;
    }
    if ((m = MD_ATX.exec(l))) { mk('h' + m[1].length, m[2]); i++; continue; }
    if (MD_HR.test(l)) { mk('hr'); i++; continue; }
    if (MD_QUOTE.test(l)) {
      const inner = [];
      while (i < lines.length && (MD_QUOTE.test(lines[i]) || (!/^\s*$/.test(lines[i]) && inner.length && inner[inner.length - 1].trim() && !mdStarts(lines, i, true))))
        inner.push(lines[i].replace(/^ {0,3}> ?/, '')), i++;
      mdBlocks(mk('blockquote'), inner, base + i - inner.length, null, depth + 1); continue;   // no task ticks inside a quote
    }
    if (mdTableAt(lines, i)) {
      const head = mdCells(lines[i]), align = mdCells(lines[i + 1]).map(x => /^:-*:$/.test(x) ? 'center' : /-:$/.test(x) ? 'right' : /^:/.test(x) ? 'left' : '');
      const t = mk('table'), th = document.createElement('thead'), tb = document.createElement('tbody'), cols = head.length;
      const row = (cells, tag) => { const tr = document.createElement('tr'); for (let k = 0; k < cols; k++) { const c = document.createElement(tag); inline(c, cells[k] || ''); if (align[k]) c.style.textAlign = align[k]; tr.append(c); } return tr; };
      th.append(row(head, 'th')); t.append(th); i += 2;
      while (i < lines.length && !/^\s*$/.test(lines[i]) && /\|/.test(lines[i]) && !MD_FENCE.test(lines[i])) tb.append(row(mdCells(lines[i++]), 'td'));
      if (tb.children.length) t.append(tb);
      continue;
    }
    if (MD_LI.test(l)) { i = mdList(parent, lines, i, base, opts, depth); continue; }
    // paragraph (=== under it: a heading); it ends at a blank line or where another block starts
    const buf = [l]; i++;
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !mdStarts(lines, i, true)) {
      if (/^ {0,3}=+[ \t]*$/.test(lines[i])) break;
      buf.push(lines[i++]);
    }
    if (i < lines.length && /^ {0,3}=+[ \t]*$/.test(lines[i])) { mk('h1', buf.join(' ').trim()); i++; continue; }
    mk('p', buf.map(x => x.replace(/^[ \t]+/, '')).join('\n').replace(/\\$/, ''));
  }
}
function mdList(parent, lines, i, base, opts, depth) {
  const first = MD_LI.exec(lines[i]), ordered = !!first[3], bullet = first[2], delim = first[4], mi = mdInd(lines[i]);
  const list = document.createElement(ordered ? 'ol' : 'ul');
  if (ordered && first[3] !== '1') list.start = parseInt(first[3], 10);
  parent.append(list);
  let loose = false;
  while (i < lines.length) {
    const m = MD_LI.exec(lines[i]), d = mdInd(lines[i]);
    if (!m || d < Math.max(0, mi - 1) || d > mi + 1) break;           // a sibling item sits at (about) the same indent
    if (!!m[3] !== ordered || (ordered ? m[4] !== delim : m[2] !== bullet) || MD_HR.test(lines[i])) break;
    const ci = mdInd(m[1] + m[2]) + Math.max(1, Math.min(m[5].length, 4)), start = i, body = [m[6]];
    i++;
    let blank = false;
    while (i < lines.length) {
      const l = lines[i];
      if (/^\s*$/.test(l)) { blank = true; body.push(''); i++; continue; }
      if (mdInd(l) >= ci) { body.push(mdStrip(l, ci)); i++; blank = false; continue; }
      if (MD_LI.test(l) && mdInd(l) > mi + 1) { body.push(mdStrip(l, mdInd(l))); i++; blank = false; continue; }   // nested under-indented ("1. a" then "  - b")
      if (blank) break;
      if (MD_LI.test(l) && mdInd(l) < ci) break;                   // the next item, or an outer list's
      if (mdStarts(lines, i, true)) break;
      body.push(l.trim()); i++;                                     // a lazy continuation line
    }
    while (body.length > 1 && body[body.length - 1] === '') body.pop();
    if (blank && i < lines.length && MD_LI.test(lines[i]) && Math.abs(mdInd(lines[i]) - mi) <= 1) loose = true;   // a blank line between items
    if (body.some((x, k) => x === '' && k + 1 < body.length && body[k + 1] !== '' && !MD_LI.test(body[k + 1]))) loose = true;   // or between paragraphs of one item
    const li = document.createElement('li'), tm = /^\[( |x|X)\](?:[ \t]+|$)/.exec(body[0]);
    if (tm) {   // R403: "- [ ] text" / "- [x] text"; a tick in View edits that line of the note
      const cb = document.createElement('input'), sp = document.createElement('span'), at = base + start, on = tm[1] !== ' ';
      cb.type = 'checkbox'; cb.checked = on; li.className = 'task' + (on ? ' done' : '');
      if (opts && opts.task) cb.onchange = () => opts.task(at, cb.checked); else cb.disabled = true;
      body[0] = body[0].slice(tm[0].length);
      const rest = document.createElement('div');
      mdBlocks(rest, body, base + start, opts, depth + 1);
      const p0 = rest.firstChild; if (p0 && p0.tagName === 'P') { sp.append(...p0.childNodes); p0.remove(); }
      li.append(cb, sp, ...rest.childNodes);
    } else mdBlocks(li, body, base + start, opts, depth + 1);
    list.append(li);
  }
  if (!loose) list.querySelectorAll(':scope>li>p').forEach(p => { p.replaceWith(...p.childNodes); });   // a tight list: no paragraph gaps
  else list.classList.add('loose');
  return i;
}
// R440: a note's one-line preview without the Markdown marks ("- [ ] milk **now**" -> "☐ milk now")
function mdPlain(t) {
  return String(t || '').replace(/(^|\s)(?:[-*+]|\d{1,9}[.)])\s+\[( |x|X)\]\s*/g, (m, p, x) => p + (x === ' ' ? '☐ ' : '☑ '))
    .replace(/(^|\s)(?:[-*+]|\d{1,9}[.)]|#{1,6}|>)\s+/g, '$1').replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/(\*\*|__|~~|`+)(?=\S)([^]*?\S)\1/g, '$2').replace(/(^|[^\w*])[*_](?=\S)([^*_]*?\S)[*_](?!\w)/g, '$1$2').replace(/\s+/g, ' ').trim();
}
function mdRender(el, raw, opts) {
  el.textContent = ''; el.classList.add('md');
  const text = String(raw == null ? '' : raw).replace(/\r\n?/g, '\n');
  try { mdBlocks(el, text.split('\n'), 0, opts, 0); }
  catch (e) { el.textContent = text; console.error('markdown', e); }   // R439: never lose the text
}
$('hide-tools').checked = localStorage.getItem('aurora_web_hide_tools') !== '0';
const applyHideTools = () => document.body.classList.toggle('hide-tools', $('hide-tools').checked);
applyHideTools();
$('hide-tools').onchange = () => { localStorage.setItem('aurora_web_hide_tools', $('hide-tools').checked ? '1' : '0'); applyHideTools(); scroll(); };
const mdOn = () => localStorage.getItem('aurora_web_md') !== '0';
function show(el, raw) { el._raw = raw; if (mdOn()) mdRender(el, raw); else { el.classList.remove('md'); render(el, raw); } }
$('md-on').checked = mdOn();
$('md-on').onchange = e => { localStorage.setItem('aurora_web_md', e.target.checked ? '1' : '0'); log.querySelectorAll('.msg.reply').forEach(el => { show(el, el._raw); showWorkspaceFiles(el, el._raw); }); };
let raf = 0;
// A reply's last chunk is painted on the next animation frame; anything that ends the reply (done, a tool call, a new message, a reset) paints it first.
// R392 (P2): while it streams, the finished blocks (up to the last blank line outside a code fence) are drawn once and
// only the open tail is redrawn each frame; the end of the reply draws it whole once, so the result is the same.
function flushCur() { if (cur) { cur.head = null; show(cur.el, cur.raw); showWorkspaceFiles(cur.el, cur.raw); lastReplyRaw = cur.raw; lastReplyEl = cur.el; } }   // R413: once, when the reply is whole
function fenceOpen(text) {   // R439: inside an unclosed ``` or ~~~ fence at the end of `text`?
  let open = null;
  for (const l of text.split('\n')) {
    const m = /^ {0,3}(`{3,}|~{3,})/.exec(l);
    if (!m) continue;
    if (!open) open = m[1];
    else if (m[1][0] === open[0] && m[1].length >= open.length && /^ {0,3}[`~]+[ \t]*$/.test(l)) open = null;
  }
  return !!open;
}
function commitPoint(raw, from) {
  for (let k = raw.lastIndexOf('\n\n'); k > from; k = raw.lastIndexOf('\n\n', k - 1)) {
    const next = raw.slice(k + 2, k + 40);
    if (/^([ \t]|[-*+] |\d{1,9}[.)] )/.test(next)) continue;   // a list or indented block may go on after the blank line
    if (!fenceOpen(raw.slice(0, k))) return k + 2;
  }
  return from;
}
function paintStreaming() {
  if (!mdOn()) { show(cur.el, cur.raw); return; }
  if (!cur.head) {
    cur.el.textContent = ''; cur.el.classList.add('md'); cur.el._raw = cur.raw;
    cur.head = document.createElement('div'); cur.tail = document.createElement('div'); cur.head.className = 'mdpart'; cur.done = 0;
    cur.el.append(cur.head, cur.tail);
  }
  const cut = commitPoint(cur.raw, cur.done);
  if (cut > cur.done) { const d = document.createElement('div'); d.className = 'mdpart'; mdRender(d, cur.raw.slice(cur.done, cut)); cur.head.append(d); cur.done = cut; }
  mdRender(cur.tail, cur.raw.slice(cur.done)); cur.tail.classList.add('mdpart'); cur.el._raw = cur.raw;
}
function paint() { if (!raf) raf = requestAnimationFrame(() => { raf = 0; if (cur) paintStreaming(); scroll(); }); }
{ const st = document.createElement('style'); st.textContent = '.mdpart>*+*,.mdpart+.mdpart,.msg.md>.mdpart+.mdpart{margin-top:12px}.mdpart:empty{display:none}'; document.head.append(st); }

function details(summary, body, cls) {
  const d = document.createElement('details'); if (cls) d.className = cls;
  const s = document.createElement('summary'), p = document.createElement('pre');
  s.textContent = summary; p.textContent = body; d.append(s, p); log.append(d); scroll(); return d;
}
const argLine = a => { const v = Object.values(a || {})[0]; return v ? String(v).split('\n')[0].slice(0, 120) : ''; };

let queued = [];   // messages waiting behind the running turn, oldest first (R373)
function paintQueue() {
  const q = $('queue'); q.hidden = !queued.length; q.replaceChildren();
  if (!queued.length) return;
  const h = document.createElement('span'); h.className = 'cap'; h.textContent = 'Queued (' + queued.length + ')'; q.append(h);
  queued.forEach((t, i) => {
    const r = document.createElement('div'); r.className = 'q-item'; const s = document.createElement('span'); s.className = 'grow'; s.textContent = t; r.append(s);
    const x = iconBtn('qx', 'Remove from the queue', ICO.x); x.onclick = () => post('/api/queue/cancel', { position: i + 1, text: t });   // R418 (I2)
    r.append(x); q.append(r);
  });
}
let replayUntil = 0;   // R392 (P3): events up to this id are a replay of the log; their side effects (requests) are skipped
const live_ = ev => ev.id > replayUntil;
let lastUser = null, lastUserText = '', editMode = false, lastReplyRaw = '', lastReplyEl = null, turnOutputs = [], pendingMark = '';   // R392: edit and resend / regenerate
// R436: the reply actions are house icon buttons (label hidden on a phone); Read aloud sits in the same row
const ICON_REGEN = '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>', ICON_EDIT = '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
      ICON_NOTE = '<path d="M6 3h9l4 4v14H6z"/><path d="M9 11h7M9 15h7"/>', ICON_SPEAK = '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16.5 8.5a5 5 0 0 1 0 7"/>',
      ICON_STOP = '<rect x="7" y="7" width="10" height="10" rx="1.5"/>';
function clearActs() { log.querySelectorAll('.acts').forEach(a => { a.querySelectorAll('.speak').forEach(sp => a.before(sp)); a.remove(); }); }
function addActs() {
  clearActs(); if (!window.AURORA_CHAT || !lastUser) return;
  const a = document.createElement('div'); a.className = 'acts';
  const re = actBtn('Regenerate', 'Ask again for a new reply to your last message', ICON_REGEN);
  re.onclick = () => post('/api/retry', { temperature: pageTemperature() });
  const ed = actBtn('Edit', 'Change your last message and send it again', ICON_EDIT);
  ed.onclick = () => { editMode = true; G('editing').hidden = false; const t = $('text'); t.value = lastUserText; t.dispatchEvent(new Event('input')); t.focus(); };
  const sp = [...log.querySelectorAll('.speak')].pop();
  if (sp && (lastUser.compareDocumentPosition(sp) & Node.DOCUMENT_POSITION_FOLLOWING)) a.append(sp);
  a.prepend(re, ed);
  if (lastReplyRaw) {   // R418 (I3): keep the reply as a note that links back to this chat
    const sv = actBtn('Save as note', 'Save this reply as a note and download the chat as HTML', ICON_NOTE);
    const raw = lastReplyRaw;
    // R426: the reply becomes a note AND the chat downloads as HTML, and both are said in the chat
    sv.onclick = async () => {
      const sid = $('sid').dataset.full || '', title = raw.replace(/[#*`>_]/g, '').trim().split('\n')[0].slice(0, 60);
      sv.disabled = true;
      if (!(await post('/api/notes/create', { title, text: raw, source_session: sid }))) { sv.disabled = false; return; }
      sv.querySelector('span').textContent = 'Saved';
      const row = chatRows.find(r => r.id === sid), got = sid && await downloadChat(sid, 'brief', (row && row.first) || title);
      note('saved as a note (Notes › Personal)' + (got ? ' and downloaded the chat as HTML' : ''));
    };
    a.insertBefore(sv, sp && sp.parentNode === a ? sp : null);
  }
  log.append(a);
}
// R418 (F2): one dim line under a reply: tokens in and out, cost when the provider charges, time taken
function turnLine(t) {
  if (!t.input && !t.output) return;
  const k = n => n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
  const d = document.createElement('div'); d.className = 'turnline';
  d.textContent = [k(t.input) + ' in', k(t.output) + ' out', t.cost ? '$' + (t.cost < 0.01 ? t.cost.toFixed(4) : t.cost.toFixed(3)) : '', t.secs != null ? secs(t.secs) : ''].filter(Boolean).join(' · ');
  log.append(d);
}
// R418 (I1): the words a chat was found by are marked in it, and the first one is scrolled to
function markMatches(q) {
  const needle = q.toLowerCase(), hits = [], w = document.createTreeWalker(log, NodeFilter.SHOW_TEXT);
  for (let n; (n = w.nextNode());) if (n.nodeValue.toLowerCase().includes(needle) && !n.parentNode.closest('mark')) hits.push(n);
  for (const n of hits) {
    const f = document.createDocumentFragment(), v = n.nodeValue, lv = v.toLowerCase(); let at = 0, k;
    while ((k = lv.indexOf(needle, at)) >= 0) { f.append(v.slice(at, k)); const m = document.createElement('mark'); m.textContent = v.slice(k, k + needle.length); f.append(m); at = k + needle.length; }
    f.append(v.slice(at)); n.replaceWith(f);
  }
  const first = log.querySelector('mark'); if (first) { stick = false; first.scrollIntoView({ block: 'center' }); }
}
// R418 (I5): the last messages of the chat are kept on this device, so it can be read again offline
const CHAT_COPY_KEY = 'aurora_web_chat_copy';
function saveChatCopy() {
  if (!window.AURORA_CHAT) return;
  const msgs = [...log.querySelectorAll('.msg.user,.msg.reply')].filter(e => !e.classList.contains('offline-copy')).slice(-50)
    .map(e => ({ r: e.classList.contains('user') ? 'user' : 'reply', t: (e._raw != null ? e._raw : e.textContent).slice(0, 20000) }));
  try { if (msgs.length) localStorage.setItem(CHAT_COPY_KEY, JSON.stringify({ sid: $('sid').dataset.full || '', at: Date.now(), msgs })); } catch (e) {}
}
function showChatCopy() {
  if (!window.AURORA_CHAT || log.childElementCount) return;
  let c; try { c = JSON.parse(localStorage.getItem(CHAT_COPY_KEY) || 'null'); } catch (e) { c = null; }
  if (!c || !Array.isArray(c.msgs) || !c.msgs.length) return;
  const h = add('note offline-copy', 'Offline: your last chat as it was ' + new Date(c.at).toLocaleString() + ' (read only)');
  h.classList.add('offline-copy');
  for (const m of c.msgs) { const el = add('msg ' + m.r + ' offline-copy', ''); if (m.r === 'user') el.textContent = m.t; else show(el, m.t); }
}
addEventListener('pagehide', saveChatCopy);
function endEdit() { editMode = false; const e = G('editing'); if (e) e.hidden = true; }
function dequeue(text) { const k = queued.indexOf(text); if (k >= 0) { queued.splice(k, 1); paintQueue(); } }
function handle(ev) {
  switch (ev.type) {
    case 'queued': queued.push(ev.text); paintQueue(); break;
    case 'queue_cancel': if (queued[ev.position - 1] === ev.text) { queued.splice(ev.position - 1, 1); paintQueue(); } else dequeue(ev.text); break;
    case 'output': if (ev.name) turnOutputs.push('~/.aurora/workspace/outputs/' + ev.name); break;   // R418 (F4)
    case 'queue_cleared': queued = []; paintQueue(); note(ev.count + ' queued message' + (ev.count === 1 ? '' : 's') + ' dropped (Stop)'); break;
    case 'user': dequeue(ev.text); setBusy(true, fromOther(ev.client)); stick = true; flushCur(); endThink(ev.t); thinkText = ''; turnTools = []; cur = null; clearActs(); lastReplyRaw = ''; lastReplyEl = null; turnOutputs = []; lastUserText = ev.text; lastUser = add('msg user', ev.text + (ev.images ? '  [' + ev.images + ' picture' + (ev.images > 1 ? 's' : '') + ']' : '')); break;
    case 'retry': clearActs(); if (lastUser && lastUser.parentNode === log) { while (lastUser.nextSibling) lastUser.nextSibling.remove(); lastUser.remove(); } lastUser = null; cur = null;
      if (live_(ev) && (ev.images || ev.notes)) note('sent again with ' + [ev.images && ev.images + ' picture' + (ev.images > 1 ? 's' : ''), ev.notes && ev.notes + ' note/file' + (ev.notes > 1 ? 's' : '')].filter(Boolean).join(' and '));   // R418 (I4)
      break;
    case 'plan': planShow(ev.on); break;
    case 'state': ctx(ev.context); if (live_(ev)) refreshModel(); break;
    case 'text':
      endThink(ev.t);
      if (!cur) { cur = { el: add('msg reply', ''), raw: '' }; live('writing…'); }
      cur.raw += ev.text; paint(); break;
    case 'think':
      if (!curThink) {
        if (thinkText) thinkText += '\n\n'; curThink = details('thinking…', ''); curThink._txt = curThink.querySelector('pre').appendChild(document.createTextNode('')); thinkT0 = ev.t; thinkSeen = Date.now();
        const sm = curThink.querySelector('summary');
        const tick = () => { sm.textContent = 'thinking… ' + secs((Date.now() - thinkSeen) / 1000); live('thinking…'); };
        thinkTimer = setInterval(tick, 1000); tick();
      }
      curThink._txt.appendData(ev.text); thinkText += ev.text; if (liveOpen) paintThink(); break;
    case 'tool_start': flushCur(); endThink(ev.t); cur = null;
      live('running ' + ev.name + '…');
      turnTools.push('$ ' + ev.name + ' ' + argLine(ev.args)); if (liveOpen) paintThink();
      tools.push({ name: ev.name, el: details(ev.name + ' ' + argLine(ev.args), JSON.stringify(ev.args, null, 1), 'tool') }); break;
    case 'tool_result': {
      // parallel calls: a result belongs to the oldest call of the same name that has none yet
      const k = tools.findIndex(t => t.name === ev.name);
      if (k >= 0) { tools[k].el.querySelector('pre').textContent += '\n\n→ ' + ev.output; tools.splice(k, 1); }
      else details(ev.name + ' result', ev.output, 'tool');
      live('working…'); break; }
    case 'notify': note(ev.text); break;
    case 'approve': showApproval(ev); break;
    case 'approve_done': closeApproval(ev.approval); break;
    case 'reset': queued = []; paintQueue(); clearInterval(thinkTimer); flushCur(); log.textContent = ''; cur = curThink = null; tools = []; thinkText = ''; turnTools = []; lastUser = null; break;
    case 'session': showSession(ev.session, !live_(ev)); break;
    case 'history': (ev.messages || []).forEach(m => { if (m.role === 'user') add('msg user', m.text); else { const el = add('msg reply', ''); show(el, m.text); speakButton(false, el); } });
      if (pendingMark) { markMatches(pendingMark); pendingMark = ''; } break;
    case 'prices': if (window.reopenModelMenu) reopenModelMenu(); break;
    case 'models': refreshModel(); break;   // R423: config.yaml's models changed
    case 'done': if (outbox.length) flushOutbox(); flushCur(); speakButton(live_(ev)); endThink(ev.t); cur = null; tools = []; setBusy(false); ctx(ev.context);
      if (turnOutputs.length) { const el = lastReplyEl || add('msg reply', ''); showWorkspaceFiles(el, (lastReplyRaw || '') + '\n' + turnOutputs.join('\n')); turnOutputs = []; }
      if (ev.turn && window.AURORA_CHAT) turnLine(ev.turn);
      addActs(); if (live_(ev)) saveChatCopy(); if (live_(ev)) { refreshModel(); if (window.AURORA_CHAT) chatList(); } break;
  }
}
// Context meter: amber from 70%, red from 90% (the server clears history after a turn that ends there).
const G = id => document.getElementById(id);
function fmtTok(n) { return n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1000 ? (n / 1000).toFixed(n >= 1e4 ? 0 : 1) + 'k' : String(n); }
function ctx(c) {
  const used = c && c.used || 0, limit = c && c.limit || 0, pct = limit ? Math.min(100, Math.round(100 * used / limit)) : 0;
  const lvl = pct >= 90 ? 'bad' : pct >= 70 ? 'warn' : '';
  const n = c && c.compactions || 0;
  if (G('ctx-txt')) {   // (G: ids only the Chat page has, so the shared-ids test skips them)
    const lim = document.createElement('span'); lim.className = 'lim'; lim.textContent = limit ? ' / ' + fmtTok(limit) : '';
    G('ctx-txt').replaceChildren(fmtTok(used), lim, limit ? ' · ' + pct + '%' : '');
    $('ctx').className = 'tag ' + lvl;
    const fold = G('ctx-fold'); fold.hidden = !n; fold.textContent = '⤵' + n; fold.title = 'Compacted ' + n + (n === 1 ? ' time' : ' times') + ' this session';
    G('hmeter').className = 'meter ' + lvl + (pct < 1 ? ' zero' : ''); G('hmeter').firstElementChild.style.width = pct + '%';   // U15
  } else { $('ctx').textContent = used ? 'ctx ' + used + (limit ? ' / ' + limit + ' (' + pct + '%)' : '') + (n ? ' · ⤵' + n : '') : ''; $('ctx').className = 'tag ' + lvl; }
  $('meter').className = 'meter ' + lvl; $('meter').firstElementChild.style.width = pct + '%';
  $('ctx-detail').textContent = (used ? used + (limit ? ' / ' + limit + ' tokens' : ' tokens') : 'no data yet') + (n ? ' · compacted ' + n + '×' : '');
  $('hint').hidden = !lvl;
  $('hint').textContent = pct >= 90 ? 'Context is full: run Clear (it clears itself after the next turn).' : 'Context is getting full: Clear or Compact soon.';
}
// R458 (#22): one live chat is shared by every device; a turn another device
// started is named as such instead of a bare "working…"
const mySent = new Set();
const fromOther = id => !!id && !mySent.has(id);
function setBusy(b, other) {
  if (b && other) { $('status').title = 'Another device or tab sent the message being answered; yours will queue behind it'; live('busy on another device…'); }
  else if (b) { $('status').title = 'Tap to show what Aurora is doing'; if ($('live').hidden || liveLabel === 'busy on another device…') live('working…'); } else { live(''); $('status').textContent = 'idle'; $('status').title = 'Idle: tap to wake a sleeping model'; $('status').classList.remove('busy'); }
  $('stop').hidden = !b;
  if (window.AURORA_CHAT) { const g = $('go'); g.title = b ? 'Queue (runs after this turn)' : 'Send'; g.setAttribute('aria-label', g.title); g.classList.toggle('queue', b); $('text').placeholder = b ? 'Queue a message…' : $('text').dataset.ph; }
  else $('go').textContent = b ? 'Queue' : 'Send';   // a message sent while a turn runs waits its turn (R373)
  document.querySelectorAll('.cmd').forEach(x => x.disabled = b);
}
// Session id: short form, full in the tooltip, click copies it.
// R461: the address bar names the open chat (`/?chat=<session id>`), so it can be bookmarked or sent to someone
// who also has access; opening such a link opens that chat
var LINK_CHAT = (new URLSearchParams(location.search).get('chat') || '').trim();
if (!/^[A-Za-z0-9_-]{1,64}$/.test(LINK_CHAT)) LINK_CHAT = '';
function chatInAddress(id) {
  const q = new URLSearchParams(location.search);
  if (id) q.set('chat', id); else q.delete('chat');
  const url = location.pathname + (q.toString() ? '?' + q : '') + location.hash;
  if (url !== location.pathname + location.search + location.hash) history.replaceState(null, '', url);
}
function showSession(id, replaying) {
  if (!LINK_CHAT) chatInAddress(id);   // R461: not while a ?chat= link is still being opened
  const e = $('sid'); e.hidden = !id; e.textContent = id ? 'session ' + id.slice(0, 6) : ''; e.dataset.full = id || ''; e.title = id + ' (click to copy)';
  if (!replaying) loadSessions();
}
$('sid').onclick = () => { const f = $('sid').dataset.full; if (f && navigator.clipboard) navigator.clipboard.writeText(f).then(() => note('session id copied: ' + f)); };
$('sid').onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('sid').click(); } };
async function openSession(id) {
  const body = $('view-body'); $('view-title').textContent = 'Session ' + id.slice(0, 6) + ' (read only)';
  const rb = $('view-resume'); rb.hidden = id === $('sid').dataset.full; rb.dataset.id = id;
  body.textContent = 'loading…'; if (!$('view').open) $('view').showModal();
  try {
    const r = await fetch('/api/sessions/' + encodeURIComponent(id), { headers: hdr() });
    const d = await r.json();
    if (r.ok) mdRender(body, d.markdown); else body.textContent = d.error || 'HTTP ' + r.status;
  } catch (e) { body.textContent = 'could not load: ' + e; }
  body.classList.add('md'); body.scrollTop = 0;
}
$('view-close').onclick = () => $('view').close();
// R376: Resume makes a past session the live one; the current chat is replaced (it stays in its own log).
$('view-resume').onclick = async () => {
  const id = $('view-resume').dataset.id;
  if (!await askConfirm('Resume session ' + id.slice(0, 6) + '?', 'The current chat is replaced by this session; new messages continue it.', 'Resume')) return;
  if (await post('/api/resume', { id })) { $('view').close(); sidebar(false); stick = true; }
};
function showText(title, text) {
  const body = $('view-body'); $('view-title').textContent = title; $('view-resume').hidden = true;
  body.classList.remove('md'); body.replaceChildren(); const pre = document.createElement('pre'); pre.textContent = text; body.append(pre);
  if (!$('view').open) $('view').showModal(); body.scrollTop = 0;
}
async function getJSON(path) { const r = await fetch(path, { headers: hdr() }); const d = await r.json().catch(() => ({})); if (!r.ok) { note(d.error || 'HTTP ' + r.status, true); return null; } return d; }
$('diff-btn').onclick = async () => {
  const d = await getJSON('/api/diff'); if (!d) return;
  showText('Last turn: what changed', d.diff ? d.diff + (d.cut ? '\n[… diff cut]' : '') : d.note);
};
$('undo-btn').onclick = async () => {
  const p = await getJSON('/api/undo'); if (!p) return;
  if (p.kind === 'none') return note('nothing to undo: no checkpoint for this workspace yet');
  if (p.kind === 'error') return note('undo: ' + (p.paths[0] || 'error'), true);
  const what = p.kind === 'file' ? 'Revert the last change to ' + p.paths[0] + '?' : 'Revert the last change (' + p.paths.length + ' file' + (p.paths.length === 1 ? '' : 's') + ')?';
  if (!await askConfirm('Undo', what, 'Undo', p.diff || p.paths.join('\n'))) return;
  await post('/api/undo');
};
$('sessions').addEventListener('click', e => { const r = e.target.closest('[data-id]'); if (r) openSession(r.dataset.id); });
$('sessions').addEventListener('keydown', e => { const r = e.target.closest('[data-id]'); if (r && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openSession(r.dataset.id); } });
let sessionsSeq = 0;
async function loadSessions() {
  if (window.AURORA_CHAT) return chatList();
  const seq = ++sessionsSeq;
  try {
    const r = await fetch('/api/sessions', { headers: hdr() }); if (!r.ok) return;
    const list = (await r.json()).sessions;
    if (seq !== sessionsSeq) return;   // a newer call is in flight: only its result is drawn
    const box = $('sessions'), rows = [];
    if (!list.length) { const e = document.createElement('span'); e.className = 'sec'; e.textContent = 'none yet'; box.replaceChildren(e); return; }
    for (const x of list) {
      const row = document.createElement('div'), c = document.createElement('code'), f = document.createElement('span');
      if (x.current) row.className = 'cur';
      c.textContent = x.short; row.title = x.id + '  ' + x.when; f.className = 'first'; f.textContent = x.first || '(empty)';
      row.setAttribute('role', 'button'); row.tabIndex = 0; row.dataset.id = x.id; row.title += '  (tap to read)';
      row.append(c, f); rows.push(row);
    }
    box.replaceChildren(...rows);
  } catch (e) {}
}

function showApproval(ev) {
  pendingApproval = ev.approval;
  $('ap-title').textContent = ev.tool;
  const a = ev.args || {}, args = Object.entries(a).map(([k, v]) => k + ': ' + v).join('\n');
  if (G('ap-args')) {   // R392 U9: what runs comes first; the diff is folded under "Show diff"
    G('ap-args').textContent = args || '(no arguments)';
    G('ap-diff').hidden = !ev.diff; G('ap-diff').open = false; $('ap-body').textContent = ev.diff || '';
  } else $('ap-body').textContent = (ev.diff ? ev.diff + '\n\n' : '') + args;
  clearInterval(apTimer); const left = G('ap-left');
  if (left && ev.timeout) {   // R418 (F3): the server denies on its own when nobody answers; say when
    const end = (ev.t + ev.timeout) * 1000;
    const tick = () => { const s = Math.max(0, Math.round((end - Date.now()) / 1000)); left.textContent = 'No answer = denied in ' + Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
    tick(); apTimer = setInterval(tick, 1000); left.hidden = false;
  }
  if (!$('approve').open) $('approve').showModal();
  if (G('ap-deny')) G('ap-deny').focus();
}
let apTimer = 0;
function closeApproval(id) { if (id === pendingApproval) { pendingApproval = null; clearInterval(apTimer); if ($('approve').open) $('approve').close(); } }
$('approve').addEventListener('cancel', e => e.preventDefault());   // Esc must not leave it half-answered
$('approve').querySelectorAll('[data-a]').forEach(b => b.onclick = () => post('/api/approve', { id: pendingApproval, answer: b.dataset.a }));

$('send').onsubmit = async e => {
  e.preventDefault();
  const t = $('text').value.trim(); if (!t && !attached.length && !noteChips.length && !fileChips.length) return;
  if (editMode) { if (await post('/api/retry', { text: t, temperature: pageTemperature() })) { $('text').value = ''; $('text').style.height = ''; endEdit(); } return; }
  const sentFiles = fileChips.slice();
  const item = { id: newId(), text: t, images: attached.map(f => f.data_url), image_paths: attached.map(f => f.path).filter(Boolean), note_ids: noteChips.map(n => n.id), files: fileChips.map(f => (f.path ? { name: f.name, text: f.text, path: f.path } : { name: f.name, text: f.text })) };
  const res = outbox.length ? 'net' : await sendNow(item);   // R396: never jump ahead of messages already waiting
  if (res === 'busy') return;                                // the queue is full: the text stays, as before
  if (res === 'net' && !toOutbox(item)) return;              // too big to keep: the text stays in the box
  if (res !== 'refused') {   // R396: clear only what was sent; text typed while the request was out stays
    if ($('text').value.trim() === t) { $('text').value = ''; $('text').style.height = ''; }
    const sent = new Set(item.images); attached = attached.filter(f => !sent.has(f.data_url));
    const sentN = new Set(item.note_ids); noteChips = noteChips.filter(n => !sentN.has(n.id));
    fileChips = fileChips.filter(f => !sentFiles.includes(f)); paintChips(); saveDraft();
  }
  if (res === 'net') flushOutbox();   // busy comes from the user/queued event: a turn that fails at once has sent done before this answer
};
// Attachments (R381): the paperclip, a file picker and drops on the message bar upload each file to /api/attach; the server returns
// text (appended to the draft as a fenced block) or a picture (kept as a chip and sent with the message, vision models only).
const ATT_MAX_IMAGES = 4;
const ATT_ACCEPT = '.pdf,.docx,.xlsx,.pptx,.odt,.ods,.odp,.rtf,.html,.htm,.txt,.md,.csv,.tsv,.json,.xml,.yaml,.yml,.log,.py,.js,.ts,.swift,.sh,.c,.h,.cpp,.java,.go,.rs,.sql,.toml,.ini,.png,.jpg,.jpeg,.gif,.webp,.mp3,.m4a,.aac,.wav,.flac,.ogg,.oga,.opus,.wma,.aiff,.aif,.mp4,.m4v,.mov,.webm,.mkv,.avi';   // R420: audio/video are stored, not read
let attached = [];   // pictures waiting to be sent: {name, data_url}
let noteChips = [];  // R401: notes sent as context with the next message only: {id, title}
const NOTE_MAX = 5;
let fileChips = [];  // R405: files whose text goes to the model raw with the next message: {name, text}
const FILE_MAX = 10;
const dropbar = $('send');
const chips = document.createElement('div'); chips.id = 'chips'; chips.hidden = true; dropbar.prepend(chips);
const picker = document.createElement('input'); picker.type = 'file'; picker.multiple = true; picker.accept = ATT_ACCEPT; picker.hidden = true; dropbar.append(picker);
const clip = document.createElement('button'); clip.type = 'button'; clip.id = 'attach'; clip.className = 'ib';   // the composer's round 36 px icon buttons clip.title = 'Attach files (or drop them here)'; clip.setAttribute('aria-label', 'Attach files');
clip.append(new DOMParser().parseFromString('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5l-8.6 8.6a5 5 0 01-7.1-7.1l8.9-8.9a3.3 3.3 0 014.7 4.7l-8.9 8.9a1.7 1.7 0 01-2.4-2.4l8-8"/></svg>', 'image/svg+xml').documentElement);
$('go').before(clip);
const css = document.createElement('style');
css.textContent = 'form#send.dropping{outline:2px dashed currentColor;outline-offset:-2px}#chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 0 6px}#chips .chip{display:inline-flex;align-items:center;gap:6px;max-width:100%;border:1px solid var(--line,#8884);border-radius:999px;padding:2px 4px 2px 8px;font-size:12px}#chips .chip img{width:22px;height:22px;object-fit:cover;border-radius:4px}#chips .chip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:160px}#chips .chip.open{cursor:pointer}#chips .chip.open:hover{background:var(--hover,#8882)}#chips .chip b{font-weight:normal}#chips .chip button{border:0;background:none;color:inherit;cursor:pointer;font-size:14px;line-height:1;padding:2px 6px}#attach{cursor:pointer}';
document.head.append(css);
function paintChips() {
  chips.hidden = !attached.length && !noteChips.length && !fileChips.length; chips.replaceChildren();
  fileChips.forEach((f, k) => {
    const c = document.createElement('div'); c.className = 'chip file';
    c.title = f.media ? 'Preview ' + f.name + ' — stored unchanged; Aurora gets its path, not its content' : 'Preview ' + f.name + ' — ' + f.text.length.toLocaleString('en') + ' characters, sent to Aurora as is' + (f.cut ? ' (only the first part: too long)' : '');
    const ic = document.createElement('b'); ic.textContent = fileType(f.name).icon; ic.setAttribute('aria-hidden', 'true');   // R414
    const nm = document.createElement('span'); nm.textContent = f.name;
    if (f.path) { c.classList.add('open'); c.onclick = e => { if (e.target !== x) previewFile('input', f.path.split('/').pop()); }; }
    const x = document.createElement('button'); x.type = 'button'; x.textContent = '×'; x.title = 'Remove'; x.setAttribute('aria-label', 'Remove file ' + f.name);
    x.onclick = () => { fileChips.splice(k, 1); paintChips(); };
    c.append(ic, nm, x); chips.append(c);
  });
  noteChips.forEach((n, k) => {
    const c = document.createElement('div'); c.className = 'chip note'; c.title = 'Sent to Aurora as context with this message';
    const ic = document.createElement('b'); ic.textContent = '📝'; ic.setAttribute('aria-hidden', 'true');
    const nm = document.createElement('span'); nm.textContent = n.title || 'Untitled';
    const x = document.createElement('button'); x.type = 'button'; x.textContent = '×'; x.title = 'Remove'; x.setAttribute('aria-label', 'Remove note ' + (n.title || 'Untitled'));
    x.onclick = () => { noteChips.splice(k, 1); paintChips(); };
    c.append(ic, nm, x); chips.append(c);
  });
  attached.forEach((f, k) => {
    const c = document.createElement('div'); c.className = 'chip';
    const im = document.createElement('img'); im.src = f.data_url; im.alt = '';
    const nm = document.createElement('span'); nm.textContent = f.name;
    if (f.path) { c.classList.add('open'); c.title = 'Preview ' + f.name; c.onclick = e => { if (e.target !== x) previewFile('input', f.path.split('/').pop()); }; }
    const x = document.createElement('button'); x.type = 'button'; x.textContent = '×'; x.title = 'Remove'; x.onclick = () => { attached.splice(k, 1); paintChips(); };
    c.append(im, nm, x); chips.append(c);
  });
}
function attachNote(id, title) {   // R401
  if (noteChips.some(n => n.id === id)) return true;
  if (noteChips.length >= NOTE_MAX) { note('at most ' + NOTE_MAX + ' notes per message', true); return false; }
  noteChips.push({ id, title }); paintChips(); return true;
}
async function attachFiles(files) {
  const t = $('text');
  for (const f of files) {
    const reading = add('msg note', 'reading ' + f.name + ' …');
    const r = await fetch('/api/attach', { method: 'POST', headers: { 'Authorization': 'Bearer ' + getToken(), 'X-Filename': encodeURIComponent(f.name) }, body: f }).catch(() => ({ ok: false, status: 0, json: async () => ({}) }));
    const d = await r.json().catch(() => ({}));
    reading.remove();   // R392 (bug 6): exactly this note, never a message that arrived meanwhile
    if (!r.ok) { note(d.error || (f.name + ': HTTP ' + r.status), true); continue; }
    if (d.kind === 'image') {
      if (attached.length >= ATT_MAX_IMAGES) { note('at most ' + ATT_MAX_IMAGES + ' pictures per message', true); continue; }
      attached.push({ name: d.name, data_url: d.data_url, path: d.path }); paintChips();
    } else {
      // R405: kept as a chip; its text goes to the model raw in front of the message, never into the box
      if (fileChips.length >= FILE_MAX) { note('at most ' + FILE_MAX + ' files per message', true); continue; }
      fileChips.push({ name: d.name, text: d.text, cut: !!d.cut, path: d.path, media: d.media }); paintChips();
      if (d.cut) note(d.name + ': only the first part was read (too long)');
    }
  }
  t.dispatchEvent(new Event('input')); t.focus();
}
clip.onclick = () => picker.click();
picker.onchange = () => { const fs = [...picker.files]; picker.value = ''; attachFiles(fs); };
['dragenter', 'dragover'].forEach(n => dropbar.addEventListener(n, e => { if (e.dataTransfer && [...e.dataTransfer.types].includes('Files')) { e.preventDefault(); dropbar.classList.add('dropping'); } }));
['dragleave', 'drop'].forEach(n => dropbar.addEventListener(n, () => dropbar.classList.remove('dropping')));
dropbar.addEventListener('drop', e => { e.preventDefault(); e.stopPropagation(); attachFiles([...e.dataTransfer.files]); });
// R405/R407: files dropped on the chat column (not the sidebar or a dialog) attach, with an overlay while they are over it;
// a drop anywhere else does nothing (the browser must not open the file either)
{ const zone = document.querySelector('.main') || document.body;
  const ov = document.createElement('div'); ov.id = 'dropall'; ov.hidden = true; ov.textContent = 'Drop files to attach';
  zone.append(ov); let depth = 0;
  const has = e => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
  const off = () => { depth = 0; ov.hidden = true; };
  zone.addEventListener('dragenter', e => { if (!has(e) || document.querySelector('dialog[open]')) return; e.preventDefault(); depth++; ov.hidden = false; });
  zone.addEventListener('dragover', e => { if (has(e) && !ov.hidden) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
  zone.addEventListener('dragleave', e => { if (has(e) && --depth <= 0) off(); });
  zone.addEventListener('drop', () => setTimeout(off), true);   // R409: capture, so a drop the message bar handles still hides the overlay
  zone.addEventListener('drop', e => { if (!has(e) || ov.hidden) return; e.preventDefault(); e.stopPropagation(); off(); attachFiles([...e.dataTransfer.files]); });
  addEventListener('dragover', e => { if (has(e) && !zone.contains(e.target)) { e.preventDefault(); e.dataTransfer.dropEffect = 'none'; } });
  addEventListener('drop', e => { if (has(e)) { e.preventDefault(); off(); } });
  if (getComputedStyle(zone).position === 'static') zone.style.position = 'relative';
  const st = document.createElement('style');
  st.textContent = '#dropall{position:absolute;inset:8px;z-index:50;display:flex;align-items:center;justify-content:center;border:2px dashed var(--accent,var(--link,#36c));border-radius:16px;background:color-mix(in srgb,var(--bg,#fff) 85%,transparent);font-size:18px;font-weight:600;color:var(--fg);pointer-events:none}#dropall[hidden]{display:none}';
  document.head.append(st); }
// Enter: v1 sends on Ctrl/Cmd+Enter. v2 (AURORA_ENTER_SENDS) works like the terminal: Enter sends, Shift+Enter or Alt/Option+Enter
// is a new line; on a touch-only device Enter stays a new line (no Shift key there, the Send button sends).
$('text').addEventListener('keydown', e => {
  if (e.key !== 'Enter' || e.isComposing) return;
  if (!window.AURORA_ENTER_SENDS) { if (e.ctrlKey || e.metaKey) $('send').requestSubmit(); return; }
  if (e.shiftKey) return;
  if (e.altKey) {
    e.preventDefault(); const t = e.target; t.setRangeText('\n', t.selectionStart, t.selectionEnd, 'end'); t.dispatchEvent(new Event('input')); return;
  }
  if (matchMedia('(pointer:coarse)').matches && !matchMedia('(pointer:fine)').matches) return;
  e.preventDefault(); if (!$('go').disabled) $('send').requestSubmit();
});
$('text').addEventListener('input', () => { const t = $('text'); t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, innerHeight * .4) + 'px'; });
$('stop').onclick = () => post('/api/stop');
const askClear = () => askConfirm('Clear the history?', 'The conversation is wiped and the next message starts fresh.', 'Clear');
function askConfirm(title, text, yes, pre) { return new Promise(res => {
  const d = $('confirm'), done = v => { d.close(); res(v); };
  $('cf-title').textContent = title; $('cf-text').textContent = text; $('cf-yes').textContent = yes;
  $('cf-pre').hidden = !pre; $('cf-pre').textContent = pre || '';
  $('cf-yes').onclick = () => done(true); $('cf-no').onclick = () => done(false);
  d.oncancel = () => res(false);
  d.showModal();
}); }
document.querySelectorAll('.cmd[data-cmd]').forEach(b => b.onclick = async () => {
  if (b.dataset.cmd === 'clear' && !await askClear()) return;
  if (b.dataset.cmd === 'compact') setBusy(true);
  const ok = await post('/api/command', { name: b.dataset.cmd });
  if (!ok && b.dataset.cmd === 'compact') setBusy(false);
  refreshModel();
});

// Sidebar sections fold on a tap; the state is remembered per section (the token box starts folded).
const folds = (() => { try { return JSON.parse(localStorage.getItem('aurora_web_folds') || '{}'); } catch { return {}; } })();
function fold(sec, open) {
  sec.classList.toggle('folded', !open); sec.querySelector('h2.cap').setAttribute('aria-expanded', String(open));
}
document.querySelectorAll('aside section[data-fold]').forEach(sec => {
  const k = sec.dataset.fold; fold(sec, k in folds ? folds[k] : !sec.hasAttribute('data-folded-by-default'));
  const h = sec.querySelector('h2.cap'), flip = () => { const o = sec.classList.contains('folded'); fold(sec, o); folds[k] = o; try { localStorage.setItem('aurora_web_folds', JSON.stringify(folds)); } catch {} };
  h.onclick = flip; h.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); } };
});
// Sidebar: open on a desktop, closed on a phone until asked; the choice is remembered.
function sidebar(open) {
  document.body.classList.toggle('sb-closed', !open);
  $('sb-toggle').setAttribute('aria-expanded', String(open));
  localStorage.setItem(SB_KEY, open ? '1' : '0');
}
// R451: the remembered state is applied with the slide switched off, so a phone's closed sidebar is closed from the first paint
// (it used to start open and slide shut on every load, and a click during that slide missed its target)
document.body.classList.add('sb-init');
{ const v = localStorage.getItem(SB_KEY); sidebar(v === null ? (window.AURORA_SB_OPEN ?? innerWidth > 768) : v === '1'); }
requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.remove('sb-init')));
$('sb-toggle').onclick = () => sidebar(document.body.classList.contains('sb-closed'));

// "New chat on open" (on by default): a fresh page starts a fresh conversation; off, it continues the last chat (R388). Skipped while a turn is running.
$('clear-open').checked = localStorage.getItem('aurora_web_clear_open') !== '0';
$('clear-open').onchange = e => localStorage.setItem('aurora_web_clear_open', e.target.checked ? '1' : '0');

// R388: "New chat on open" off → open on the most recent chat. A chat already in progress on the server (a page reload) is kept as it is.
async function continueLast() {
  try {
    const r = await fetch('/api/sessions?limit=30', { headers: hdr() });
    if (!r.ok) return;
    const rows = (await r.json()).sessions || [];
    if (rows.some(x => x.current && x.prompts > 0)) return;
    const last = rows.find(x => !x.current && x.prompts > 0);
    if (last) await post('/api/resume', { id: last.id });
  } catch (e) {}
}

async function stream() {
  for (let wait = 1000; ; wait = Math.min(wait * 2, 15000)) {
    try {
      const r = await fetch('/api/stream?after=' + lastId + (bootId ? '&boot=' + encodeURIComponent(bootId) : ''), { headers: hdr() });
      if (r.status === 401) { clearOfflineData(); if (window.openSettings) { note('this server needs the token — paste it in Settings and press Save', true); window.openSettings('token'); } else { note('this server needs the token — paste it in the sidebar and press Save', true); sidebar(true); fold(document.querySelector('[data-fold=token]'), true); } return; }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const rd = r.body.getReader(), dec = new TextDecoder(); let buf = '';
      wait = 1000; netState('ok'); flushOutbox(); log.querySelectorAll('.offline-copy').forEach(e => e.remove());
      for (;;) {
        // R396: the server sends a heartbeat every 15 s; silence for 40 s is a dead link (a phone that slept), not a quiet one
        const { done, value } = await Promise.race([rd.read(), new Promise((_, no) => { stallT = setTimeout(() => no(new Error('stalled')), 40000); })])
          .finally(() => clearTimeout(stallT));
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
          if (chunk.startsWith('data: ')) { const ev = JSON.parse(chunk.slice(6)); if (ev.type === 'restart') { log.textContent = ''; queued = []; paintQueue(); bootId = ev.boot || bootId; lastUser = null; } lastId = ev.id; handle(ev); }
        }
      }
    } catch (e) { /* reconnect with the last id: nothing is lost, nothing repeats */ }
    netState(navigator.onLine === false ? 'offline' : 'reconnecting');
    if (!lastId) showChatCopy();
    await nap(wait);   // R396: cut short when the device comes back online or the page is shown again
  }
}
// R460: a page opened as `/?note=<id>` (Notes › Copy link) loads that note into a chat at once, before any message;
// R462: `/?note=<id>,<id>,…` loads several (at most 5), in that order
var LINK_NOTE = (new URLSearchParams(location.search).get('note') || '').trim().slice(0, 80);
function askChoice(title, text, yes, no) { return new Promise(res => {   // like askConfirm, with two answers; Esc = neither
  const d = $('confirm'), nb = $('cf-no'), was = nb.textContent, done = v => { nb.textContent = was; d.close(); res(v); };
  $('cf-title').textContent = title; $('cf-text').textContent = text; $('cf-yes').textContent = yes; nb.textContent = no;
  $('cf-pre').hidden = true;
  $('cf-yes').onclick = () => done(true); nb.onclick = () => done(false);
  d.oncancel = () => { nb.textContent = was; res(null); };
  d.showModal();
}); }
async function openLinkNote(empty, busy) {
  const id = LINK_NOTE; LINK_NOTE = '';
  const q = new URLSearchParams(location.search); q.delete('note');   // a reload must not load it again
  history.replaceState(null, '', location.pathname + (q.toString() ? '?' + q : '') + location.hash);
  const ids = [...new Set(id.split(',').map(x => x.trim()).filter(Boolean))].slice(0, 5);
  if (!ids.length || !ids.every(x => /^[0-9a-f]{12}$/.test(x))) { note('this link names no note: use Notes › Copy link for a note\'s link', true); return; }
  if (busy) { note('Aurora is busy: open the note link again when the reply has finished', true); return; }
  const found = [];
  for (const x of ids) { try { const r = await fetch('/api/notes/' + x, { headers: hdr() }); if (r.ok) found.push(await r.json()); else note('a note in this link no longer exists', true); } catch (e) {} }
  if (!found.length) return;
  let fresh = false;
  if (!empty) {
    const what = found.length === 1 ? '\u201c' + (found[0].title || 'Untitled') + '\u201d' : found.length + ' notes';
    const c = await askChoice('Load ' + what, 'Start a new chat with ' + (found.length === 1 ? 'this note' : 'these notes') + ' as context, or add ' + (found.length === 1 ? 'it' : 'them') + ' to the chat that is open now?', 'New chat', 'Add to this chat');
    if (c === null) return;
    fresh = c;
  }
  for (let k = 0; k < found.length; k++) if (!(await post('/api/load-note', { id: found[k].id, new_chat: fresh && k === 0 }))) break;
}
let ctxFile = '', bootId = '';
$('load-ctx').onclick = () => { const t = $('text'); t.value = 'load ' + ctxFile; t.focus(); t.dispatchEvent(new Event('input')); };
function autoShow(on) { $('auto-on').checked = on; $('auto-tag').classList.toggle('on', on); }
$('auto-on').onchange = async e => { if (!await post('/api/auto', { on: e.target.checked })) refreshModel(); autoShow(e.target.checked); };
// The server caches "what is loaded" for up to 2 minutes, so this is a recent view, not a live probe.
async function refreshModel(first) {
  try {
    const r = await fetch('/api/state', { headers: hdr() });
    if (!r.ok) return;
    const s = await r.json(), m = $('model'), live = s.context && s.context.live_model;
    const where = s.provider && s.provider !== 'local' ? s.provider + ' · ' : '';   // R387: which machine's server, when it is not this one
    // R392 U8: a dot carries the state, the name gets the room (● loaded, ○ asleep: it wakes on send)
    if (s.model && s.model !== 'local') { m.textContent = '● ' + s.model; m.title = 'model: ' + s.model + ' (tap to choose)'; m.className = 'tag ok'; }
    else {
      m.textContent = s.loaded ? '● ' + where + live.split('/').pop() : '○ ' + where + 'asleep';
      m.title = (s.loaded ? 'loaded: ' + live : 'no model loaded: it wakes on send') + ' (tap to choose)';
      m.className = 'tag ' + (s.loaded ? 'ok' : 'bad');
    }
    planShow(!!s.plan_mode);
    if (G('mic')) G('mic').hidden = !(s.stt && window.MediaRecorder && navigator.mediaDevices);
    const vc = document.getElementById('ver-code'); if (vc) vc.textContent = s.version || '?';
    ctx(s.context); autoShow(s.auto_approve); ctxFile = s.context_file || ''; $('load-ctx').disabled = !ctxFile;
    if (first) {
      bootId = s.boot || ''; replayUntil = s.last_event || 0;
      // R392 (bug 9): "New chat on open" never wipes a chat another device has open or used in the last 10 minutes
      const inUse = s.streams > 0 || (s.idle_s != null && s.idle_s < 600);
      let cleared = false;
      if (LINK_CHAT) {   // R461: a chat link opens that chat, instead of New chat on open / continuing the last one
        const want = LINK_CHAT; let at = s.session_id;
        if (s.session_id !== want && !s.busy) { if (await post('/api/resume', { id: want })) { at = want; stick = true; } else note('could not open the chat in this link', true); }
        else if (s.session_id !== want) note('Aurora is busy: open the chat link again when the reply has finished', true);
        LINK_CHAT = ''; chatInAddress(at);
      }
      else if (!s.busy && $('clear-open').checked && !inUse) cleared = !!(await post('/api/command', { name: 'clear' }));
      else if (!s.busy && $('clear-open').checked) note('continuing the chat in use on another device (New chat on open skipped)');
      else if (!s.busy) await continueLast();
      setBusy(s.busy, s.busy && fromOther(s.busy_client));
      if (LINK_NOTE) await openLinkNote(cleared || !!s.chat_empty, s.busy);   // R460
    }
  } catch (e) {}
}
setInterval(() => { if (!document.hidden) refreshModel(); }, 10000);   // R392 (P6): a hidden tab does not poll
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshModel(); });
function planShow(on) { const c = G('plan-on'), t = G('plan-tag'); if (c) c.checked = on; if (t) t.hidden = !on; const x = $('text'); if (window.AURORA_CHAT && x) { x.dataset.ph = on ? 'Plan mode: describe the task' : 'Send a message'; if (!$('go').classList.contains('queue')) x.placeholder = x.dataset.ph; } }
if (G('plan-on')) G('plan-on').onchange = async e => { if (!await post('/api/plan', { on: e.target.checked })) refreshModel(); };
// R392: cost and usage for this chat, and the budget caps (the terminal's /cost and /budget). Also what a tap on the context pill opens.
async function openUsage() {
  const u = await getJSON('/api/usage'); if (!u) return;
  const body = $('view-body'); $('view-title').textContent = 'Context, cost and usage'; $('view-resume').hidden = true;
  body.classList.remove('md'); body.replaceChildren();
  const p = document.createElement('p'); p.className = 'sec'; p.textContent = 'Context: ' + $('ctx-detail').textContent; body.append(p);
  const money = v => v == null ? '–' : '$' + v.toFixed(v < 1 ? 4 : 2);
  const t = document.createElement('table'); t.className = 'usage';
  const hr = document.createElement('tr'); ['Model', 'Turns', 'In', 'Out', 'Cost'].forEach(h => { const th = document.createElement('th'); th.textContent = h; hr.append(th); }); t.append(hr);
  for (const r of u.models) { const tr = document.createElement('tr'); [r.model, r.turns, fmtTok(r.input), fmtTok(r.output), money(r.cost)].forEach(v => { const td = document.createElement('td'); td.textContent = v; tr.append(td); }); t.append(tr); }
  if (!u.models.length) { const tr = document.createElement('tr'), td = document.createElement('td'); td.colSpan = 5; td.textContent = 'No turns in this chat yet.'; tr.append(td); t.append(tr); }
  body.append(t);
  const tot = document.createElement('p'); tot.className = 'sec'; tot.style.marginTop = '8px'; tot.textContent = 'This chat: ' + money(u.session_cost) + (u.today != null ? ' · today: ' + money(u.today) : ''); body.append(tot);
  const b = document.createElement('div'); b.className = 'budget';
  for (const [which, label, val] of [['session', 'Chat budget $', u.session_budget], ['daily', 'Daily budget $', u.daily_budget]]) {
    const l = document.createElement('label'); l.textContent = label; const i = document.createElement('input'); i.type = 'number'; i.min = '0'; i.step = '0.5'; i.inputMode = 'decimal'; i.value = val || ''; i.placeholder = 'no limit'; i.id = 'budget-' + which; l.htmlFor = i.id;
    const s = document.createElement('button'); s.type = 'button'; s.className = 'btn'; s.textContent = 'Set'; s.onclick = () => post('/api/budget', { which, usd: Number(i.value || 0) }).then(ok => ok && openUsage());
    b.append(l, i, s);
  }
  body.append(b);
  if (u.models.length) {   // R397: the same refresh as the model menu
    const rb = document.createElement('button'); rb.type = 'button'; rb.className = 'btn'; rb.id = 'usage-prices'; rb.textContent = 'Refresh OpenRouter prices';
    rb.style.marginTop = '16px'; rb.onclick = async () => { rb.disabled = true; rb.textContent = 'Refreshing…'; await refreshPrices(); openUsage(); };
    body.append(rb);
  }
  if (!$('view').open) $('view').showModal();
}
if (G('usage-btn')) G('usage-btn').onclick = openUsage;
$('ctx').onclick = openUsage; $('ctx').onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openUsage(); } };   // R392 bug 18 / U16
if (G('edit-cancel')) G('edit-cancel').onclick = () => { endEdit(); $('text').value = ''; $('text').dispatchEvent(new Event('input')); };
// R392 voice input: record in the browser, the server turns it into text (SpeechToText's whisper), the text lands in the message box
if (G('mic')) (function () {
  const mic = G('mic'); let rec = null, chunks = [];
  mic.onclick = async () => {
    if (rec) { rec.stop(); return; }
    let stream; try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch (e) { note('microphone not available: ' + e.message, true); return; }
    chunks = []; rec = new MediaRecorder(stream);
    rec.ondataavailable = e => e.data.size && chunks.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach(t => t.stop()); mic.classList.remove('rec'); mic.setAttribute('aria-pressed', 'false'); rec = null;
      const blob = new Blob(chunks, { type: chunks[0] ? chunks[0].type : 'audio/webm' }); if (!blob.size) return;
      const busy = add('msg note', 'transcribing …');
      try {
        const r = await fetch('/api/transcribe?lang=' + encodeURIComponent(voiceLang().split('-')[0]), { method: 'POST', headers: { 'Authorization': 'Bearer ' + getToken() }, body: blob });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) note(d.error || 'HTTP ' + r.status, true);
        else if (d.text) { const t = $('text'); t.value += (t.value && !/\s$/.test(t.value) ? ' ' : '') + d.text; t.dispatchEvent(new Event('input')); t.focus(); }
      } finally { busy.remove(); }
    };
    rec.start(); mic.classList.add('rec'); mic.setAttribute('aria-pressed', 'true');
  };
})();
refreshModel(true).then(stream);

// ── R377: the Chat page (Design Language flavour 3). Everything below runs only there. ──
// R402: the sidebar Chats list folds like the Menu sections (remembered in the same store); a search unfolds it.
if ($('chats-cap')) {
  const cap = $('chats-cap');
  const setChats = open => { document.body.classList.toggle('chats-folded', !open); cap.setAttribute('aria-expanded', String(open)); };
  const saveChats = open => { folds.chats = open; try { localStorage.setItem('aurora_web_folds', JSON.stringify(folds)); } catch {} };
  setChats(folds.chats !== false);
  const flip = () => { const open = document.body.classList.contains('chats-folded'); setChats(open); saveChats(open); };
  cap.onclick = flip; cap.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); } };
  if ($('search')) $('search').addEventListener('input', () => { if ($('search').value.trim()) setChats(true); });
}
let chatSearch = '', chatSeq = 0, chatTimer = 0;
const DAY = 86400000;
function ago(ms) { const m = Math.max(0, Date.now() - ms) / 60000; return m < 60 ? Math.max(1, Math.round(m)) + 'm' : m < 1440 ? Math.round(m / 60) + 'h' : m < 10080 ? Math.round(m / 1440) + 'd' : Math.round(m / 10080) + 'w'; }
function bucket(ms) {
  const t0 = new Date(); t0.setHours(0, 0, 0, 0); const d = t0.getTime();
  if (ms >= d) return 'Today'; if (ms >= d - DAY) return 'Yesterday'; if (ms >= d - 7 * DAY) return 'Previous 7 days'; if (ms >= d - 30 * DAY) return 'Previous 30 days';
  return new Date(ms).toLocaleString(undefined, { month: 'long', year: new Date(ms).getFullYear() === t0.getFullYear() ? undefined : 'numeric' });
}
const FINE = () => matchMedia('(hover:hover) and (pointer:fine)').matches;   // R383: drag and drop only where there is a mouse
const ICO = { del: 'M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12', edit: 'M4 20h4L19 9l-4-4L4 16z', chev: 'M9 6l6 6-6 6', more: 'M5 12h.01M12 12h.01M19 12h.01', x: 'M6 6l12 12M18 6L6 18', share: 'M4 13v6h16v-6M12 3v12M8 7l4-4 4 4', dl: 'M12 4v11M7 10l5 5 5-5M5 19h14' };
function icon(d) {
  const NS = 'http://www.w3.org/2000/svg', v = document.createElementNS(NS, 'svg'), p = document.createElementNS(NS, 'path');
  for (const [k, x] of Object.entries({ viewBox: '0 0 24 24', width: 16, height: 16, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.8, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) v.setAttribute(k, x);
  p.setAttribute('d', d); v.append(p); return v;
}
function iconBtn(cls, label, d) { const b = document.createElement('button'); b.type = 'button'; b.className = 'mini ' + cls; b.title = label; b.setAttribute('aria-label', label); b.append(icon(d)); return b; }
let chatRows = [];
function chatRow(x, q, inFolder) {
  const ms = x.ts ? x.ts * 1000 : new Date(x.when.replace(' ', 'T')).getTime();   // R392: epoch from the server, no timezone guess
  const b = document.createElement('div'); b.setAttribute('role', 'button'); b.tabIndex = 0;
  b.className = 'chat' + (x.current ? ' cur' : '') + (inFolder ? ' in' : ''); b.dataset.chat = x.id;
  if (x.current) b.setAttribute('aria-current', 'true');
  if (!q && FINE()) b.draggable = true;
  const t = document.createElement('span'); t.className = 'ct'; t.textContent = x.first || 'New Chat';
  const a = document.createElement('span'); a.className = 'ca'; a.textContent = ago(ms);
  const n = document.createElement('span'); n.className = 'cn'; n.textContent = x.prompts || ''; n.title = (x.prompts || 0) + ' message' + (x.prompts === 1 ? '' : 's') + ' sent';
  b.title = (x.first || 'New Chat') + '\n' + x.when + ' · ' + x.short + ' · ' + (x.prompts || 0) + ' sent';
  if (q && x.hits) { n.textContent = x.hits; n.title = x.hits + ' matching message' + (x.hits === 1 ? '' : 's') + (x.capped ? ' (only the newest part of this long chat was searched)' : ''); }   // R418 (I1)
  b.append(t, n, a, iconBtn('cm', 'More', ICO.more), iconBtn('cs', 'Share link', ICO.share), iconBtn('cd', 'Download chat', ICO.dl), iconBtn('cr', 'Rename chat', ICO.edit), iconBtn('cx', 'Delete chat', ICO.del)); return b;
}
async function chatList() {
  const seq = ++chatSeq, q = chatSearch;
  if ($('sessions').querySelector('form.nm')) return;   // a name is being typed: don't redraw under it
  try {
    const r = await fetch(q ? '/api/search?q=' + encodeURIComponent(q) : '/api/sessions?limit=60', { headers: hdr() }); if (!r.ok) return;
    const body = await r.json(), list = body.sessions, dirs = q ? [] : (body.folders || []); if (seq !== chatSeq) return;
    chatRows = list;
    const box = $('sessions'), out = []; let last = '';
    const cur = list.find(x => x.current);
    const title = $('chat-title'); if (title) { const ct = (cur && cur.first) || 'New Chat'; if (panelDlg) title.dataset.chat = ct; else title.textContent = ct; document.title = (cur && cur.first ? cur.first.slice(0, 40) + ' · ' : '') + 'Aurora'; }
    const rows = list.filter(x => x.first || x.current);
    for (const f of dirs) {
      const mine = rows.filter(x => x.folder === f.id);
      const w = document.createElement('div'); w.className = 'fold' + (f.open ? ' open' : ''); w.dataset.folder = f.id;
      const h = document.createElement('div'); h.className = 'fh'; h.setAttribute('role', 'button'); h.tabIndex = 0; h.setAttribute('aria-expanded', String(f.open));
      const c = document.createElement('span'); c.className = 'chev'; c.append(icon(ICO.chev));
      const n = document.createElement('span'); n.className = 'fn'; n.textContent = f.name;
      const k = document.createElement('span'); k.className = 'ca'; k.textContent = mine.length || '';
      h.append(c, n, k, iconBtn('fr', 'Rename folder', ICO.edit), iconBtn('fx', 'Delete folder', ICO.del)); w.append(h);
      if (f.open) for (const x of mine) w.append(chatRow(x, q, true));
      out.push(w);
    }
    const loose = rows.filter(x => !x.folder);
    if (!loose.length && !dirs.length) { const e = document.createElement('p'); e.className = 'empty'; e.textContent = q ? 'No chat contains “' + q + '”.' : 'No chats yet.'; box.replaceChildren(e); return; }
    for (const x of loose) {
      const g = q ? 'Results' : bucket(x.ts ? x.ts * 1000 : new Date(x.when.replace(' ', 'T')).getTime());
      if (g !== last) { const h = document.createElement('h3'); h.className = 'grp'; h.textContent = g; out.push(h); last = g; }
      out.push(chatRow(x, q, false));
    }
    box.replaceChildren(...out);
  } catch (e) {}
}
// R383: a name typed in place (new folder, rename); Enter keeps it, Esc or leaving cancels.
function nameInput(initial, done, label = 'Folder name', allowEmpty = false) {
  const f = document.createElement('form'), i = document.createElement('input');
  f.className = 'nm'; i.value = initial; i.maxLength = 60; i.setAttribute('aria-label', label); i.setAttribute('enterkeyhint', 'done'); i.autocomplete = 'off'; f.append(i); f.input = i;
  let fin = false;
  const end = async ok => { if (fin) return; fin = true; const v = i.value.trim(); if (ok && (v || allowEmpty) && v !== initial) await done(v); f.remove(); chatList(); };
  f.onsubmit = e => { e.preventDefault(); end(true); };   // a form: phone keyboards send Enter as a submit, not always a keydown
  i.onkeydown = e => { e.stopPropagation(); if (e.key === 'Escape') end(false); };
  i.onblur = () => end(true);   // tapping away keeps a typed name; an empty or unchanged one is dropped
  f.onclick = e => e.stopPropagation(); return f;
}
async function deleteChat(id) {
  const x = chatRows.find(r => r.id === id);
  if (!await askConfirm('Delete this chat?', 'It leaves this list. The terminal version keeps it.', 'Delete', x && x.first)) return;
  if (x && x.current) await post('/api/command', { name: 'clear' });
  await post('/api/chat/delete', { id }); chatList();
}
async function deleteFolder(id) {
  const mine = chatRows.filter(r => r.folder === id);
  if (!await askConfirm('Delete this folder?', 'It and its ' + mine.length + ' chat' + (mine.length === 1 ? '' : 's') + ' leave this list. The terminal version keeps them.', 'Delete')) return;
  if (mine.some(r => r.current)) await post('/api/command', { name: 'clear' });
  await post('/api/folder/delete', { id }); chatList();
}
function isPhone() { return matchMedia('(max-width:768px)').matches; }
if (window.AURORA_CHAT) {
  const side = $('sessions');
  side.addEventListener('click', async e => {
    if (e.target.closest('form.nm')) return;   // typing a name inside a row must not open the chat
    const cm = e.target.closest('.cm');
    if (cm) { e.stopPropagation(); const row = cm.closest('[data-chat]'); side.querySelectorAll('.chat.open').forEach(r => r !== row && r.classList.remove('open')); row.classList.toggle('open'); return; }   // R392 U11
    const cr = e.target.closest('.cr'), cx = e.target.closest('.cx'), fx = e.target.closest('.fx'), fr = e.target.closest('.fr'), fh = e.target.closest('.fh'), b = e.target.closest('[data-chat]');
    if (cr) {   // R385: rename in place; emptying the name brings the first message back
      e.stopPropagation(); const row = cr.closest('[data-chat]'), t = row.querySelector('.ct'), id = row.dataset.chat, x = chatRows.find(r => r.id === id);
      const inp = nameInput(x && x.renamed ? x.first : '', v => post('/api/chat/rename', { id, name: v }), 'Chat name', true);
      inp.input.placeholder = (x && x.first) || 'New Chat'; row.draggable = false; t.replaceWith(inp); inp.input.focus(); inp.input.select(); return;
    }
    if (cx) { e.stopPropagation(); return deleteChat(cx.closest('[data-chat]').dataset.chat); }
    const cs = e.target.closest('.cs'); if (cs) { e.stopPropagation(); return shareChat(cs.closest('[data-chat]').dataset.chat); }
    const cd = e.target.closest('.cd'); if (cd) { e.stopPropagation(); return exportChat(cd.closest('[data-chat]').dataset.chat); }   // R421
    if (fx) { e.stopPropagation(); return deleteFolder(fx.closest('[data-folder]').dataset.folder); }
    if (fr) {
      e.stopPropagation(); const w = fr.closest('[data-folder]'), n = w.querySelector('.fn');
      const inp = nameInput(n.textContent, v => post('/api/folder/rename', { id: w.dataset.folder, name: v })); n.replaceWith(inp); inp.input.focus(); inp.input.select(); return;
    }
    if (fh) { e.stopPropagation(); if (e.target.closest('input')) return; await post('/api/folder/open', { id: fh.parentNode.dataset.folder, open: !fh.parentNode.classList.contains('open') }); return chatList(); }
    if (!b) return;
    e.stopPropagation();
    if (isPhone()) sidebar(false);
    if (panelDlg && !(await closePanel())) return;   // R418: a chat tapped while Notes/Files fill the page goes back to the chat
    if (b.classList.contains('cur')) { if (chatSearch) markMatches(chatSearch); return; }
    if (await post('/api/resume', { id: b.dataset.chat })) { stick = true; pendingMark = chatSearch; }
  }, true);
  side.addEventListener('keydown', e => { const t = e.target; if ((e.key === 'Enter' || e.key === ' ') && (t.matches('[data-chat]') || t.matches('.fh'))) { e.preventDefault(); t.click(); } });
  // R383: drag a chat onto a folder to file it, onto anything else in the list to take it out (mouse only).
  let dragId = '';
  side.addEventListener('dragstart', e => { const b = e.target.closest('[data-chat]'); if (!b) return; dragId = b.dataset.chat; e.dataTransfer.setData('text/plain', dragId); e.dataTransfer.effectAllowed = 'move'; });
  side.addEventListener('dragend', () => { dragId = ''; side.querySelectorAll('.over').forEach(n => n.classList.remove('over')); });
  side.addEventListener('dragover', e => {
    if (!dragId) return; e.preventDefault();
    side.querySelectorAll('.over').forEach(n => n.classList.remove('over'));
    const w = e.target.closest('[data-folder]'); (w || side).classList.add('over');
  });
  side.addEventListener('drop', async e => {
    if (!dragId) return; e.preventDefault(); const id = dragId, w = e.target.closest('[data-folder]'); dragId = '';
    side.querySelectorAll('.over').forEach(n => n.classList.remove('over'));
    if (await post('/api/chat/move', { chat: id, folder: w ? w.dataset.folder : '' })) chatList();
  });
  $('new-folder').onclick = () => {
    if (document.body.classList.contains('chats-folded') && $('chats-cap')) $('chats-cap').click();   // R411: it makes a chat folder, so show the chats
    if (side.querySelector('form.nm')) return;
    const row = document.createElement('div'); row.className = 'fh newf';
    const inp = nameInput('', async v => { await post('/api/folder/create', { name: v }); });
    inp.input.placeholder = 'Folder name'; row.append(inp); side.prepend(row); inp.input.focus();
  };
  $('new-chat').onclick = async () => {
    if (isPhone()) sidebar(false);
    if (panelDlg) { if (await closePanel()) $('text').focus(); return; }   // R418: it reads Back to chat while Notes/Files are open
    await post('/api/command', { name: 'clear' }); $('text').focus();
  };
  $('search').addEventListener('input', e => { clearTimeout(chatTimer); chatTimer = setTimeout(() => { chatSearch = e.target.value.trim().length >= 2 ? e.target.value.trim() : ''; chatList(); }, 250); });
  $('search').addEventListener('keydown', e => { if (e.key === 'Escape') { e.target.value = ''; chatSearch = ''; chatList(); } });
  if (G('settings-btn')) {   // R431: Settings (bottom of the sidebar) fill the right side like Notes and Files; the ••• menu is gone
    const dlg = $('settings');
    window.openSettings = async focus => {
      if (isPhone()) sidebar(false);
      if (panelDlg !== dlg && !(await openPanel(dlg, 'Settings'))) return;
      if (focus && G(focus)) G(focus).focus();
    };
    $('settings-btn').onclick = () => window.openSettings();
    $('set-close').onclick = () => dlg.close();
    dlg.addEventListener('close', () => panelGone(dlg));
  }
  $('jump').onclick = () => { stick = true; log.scrollTo({ top: log.scrollHeight, behavior: 'smooth' }); };
  $('scrim').onclick = () => sidebar(false);
  $('sb-close').onclick = () => sidebar(false);
  chatList();   // don't wait for the stream's first event: a slow phone link shows the list at once
}

// R392 U3: a popup opens above its button if it fits, else below, else pinned inside the visible area (keyboard up), scrolling inside.
function place(menu, anchor) {
  const vh = window.visualViewport ? visualViewport.height : innerHeight, t = anchor.getBoundingClientRect();
  menu.style.maxHeight = (vh - 8) + 'px';
  const h = Math.min(menu.scrollHeight, vh - 8), w = menu.offsetWidth;
  menu.style.left = Math.max(4, Math.min(t.right - w, innerWidth - w - 4)) + 'px';
  menu.style.top = (t.top - h - 6 >= 4 ? t.top - h - 6 : t.bottom + 6 + h <= vh ? t.bottom + 6 : Math.max(4, vh - h - 4)) + 'px';
}
// R387: the model name is a button that opens the list of configured models, like the TUI's /model.
(function () {
  const tag = $('model'); if (!tag) return;
  tag.setAttribute('role', 'button'); tag.tabIndex = 0; tag.style.cursor = 'pointer'; tag.title = 'Choose the model';
  const css = document.createElement('style');
  css.textContent = '#model-menu{position:fixed;z-index:50;min-width:200px;max-width:min(92vw,360px);max-height:60vh;overflow:auto;background:var(--panel,#fff);color:var(--fg,#000);border:1px solid var(--line,#ccc);border-radius:var(--r-md,8px);padding:4px;box-shadow:0 6px 24px rgba(0,0,0,.25)}'
    + '#model-menu button{display:flex;justify-content:space-between;gap:12px;width:100%;text-align:left;background:none;border:0;border-radius:var(--r-sm,6px);padding:8px 10px;color:inherit;font:inherit;cursor:pointer}'
    + '#model-menu button:hover:not(:disabled){background:var(--hover,#eee)}#model-menu button:disabled{opacity:.5;cursor:not-allowed}'
    + '#model-menu small{color:var(--dim,#666)}#model-menu .cur{font-weight:600}#model-menu .prices{border-top:1px solid var(--line,#ccc);border-radius:0;margin-top:4px}#model-menu .prices+.prices{border-top:0;margin-top:0}'
    + '@media (pointer:coarse){#model-menu button{min-height:44px;align-items:center}}';
  document.head.appendChild(css);
  let menu = null;
  const close = () => { if (menu) { menu.remove(); menu = null; } };
  async function open() {
    if (menu) return close();
    const r = await fetch('/api/models', { headers: hdr() });
    if (!r.ok) { note('could not load the model list', true); return; }
    const list = (await r.json()).models || [];
    menu = document.createElement('div'); menu.id = 'model-menu'; menu.setAttribute('role', 'menu');
    for (const m of list) {
      const b = document.createElement('button'); b.type = 'button'; b.setAttribute('role', 'menuitem');
      if (m.current) b.className = 'cur';
      const name = document.createElement('span'); name.textContent = (m.current ? '✓ ' : '') + (m.model === 'local' ? m.provider : m.model);
      const prov = document.createElement('small'); prov.textContent = m.model === 'local' ? (m.ready ? 'whatever is loaded' : 'no key') : (m.ready ? m.provider : m.provider + ' · no key');
      if (m.price) { prov.textContent += ' · ' + priceText(m.price); prov.title = priceTitle(m.price); }   // R397
      b.append(name, prov); b.disabled = !m.ready;
      b.onclick = async () => { close(); if (await post('/api/model', { i: m.i })) { refreshModel(); post('/api/wake', {}); } };   // R426: picking a sleeping model wakes it
      menu.appendChild(b);
    }
    if (list.some(m => m.provider === 'openrouter')) {   // R397: refresh the OpenRouter prices from the catalog now
      const at = Math.max(0, ...list.filter(m => m.price && m.price.at).map(m => m.price.at));
      const rb = document.createElement('button'); rb.type = 'button'; rb.id = 'prices-refresh'; rb.className = 'prices'; rb.setAttribute('role', 'menuitem');
      const l = document.createElement('span'); l.textContent = 'Refresh prices';
      const w = document.createElement('small'); w.textContent = at ? 'updated ' + ago(at) : 'never updated';
      rb.append(l, w);
      rb.onclick = async e => { e.stopPropagation(); rb.disabled = true; l.textContent = 'Refreshing…'; await refreshPrices(); close(); open(); };
      menu.appendChild(rb);
    }
    if (openModels) {   // R430: Edit models lives here, beside Refresh prices (it left the menu)
      const eb = document.createElement('button'); eb.type = 'button'; eb.id = 'models-edit'; eb.className = 'prices'; eb.setAttribute('role', 'menuitem');
      const l = document.createElement('span'); l.textContent = 'Edit models';
      const w = document.createElement('small'); w.textContent = 'config.yaml';
      eb.append(l, w); eb.onclick = e => { e.stopPropagation(); close(); openModels(); };
      menu.appendChild(eb);
    }
    document.body.appendChild(menu);
    place(menu, tag);
  }
  window.reopenModelMenu = () => { if (menu) { close(); open(); } };
  tag.onclick = open;
  tag.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } else if (e.key === 'Escape') close(); };
  document.addEventListener('click', e => { if (menu && !menu.contains(e.target) && e.target !== tag) close(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
})();

// R390: Commands live in a popup beside Send.
if ($('cmd-btn')) (function () {
  const btn = $('cmd-btn'), menu = $('cmd-menu');
  const close = () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
  btn.onclick = () => {
    if (!menu.hidden) return close();
    menu.hidden = false; btn.setAttribute('aria-expanded', 'true');
    place(menu, btn);
  };
  menu.addEventListener('click', e => { if (e.target.closest('button') && !e.target.closest('button').disabled) close(); });
  document.addEventListener('click', e => { if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) close(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
})();

// R389: Notes — plain-text notes kept on the server (shared by every device). Autosaves as you type; a phone shows the list, then the note.
if ($('notes-btn')) (function () {
  const dlg = $('notes'), shell = $('nshell'), list = $('note-list'), edit = $('note-edit'), empty = $('note-empty'),
        title = $('note-title'), text = $('note-text'), state = $('note-state'), meta = $('note-meta'), search = $('note-search');
  let cur = null, curUpdated = null, timer = 0, saving = null, pending = false, failed = false, searchT = 0;
  // R422: Personal / Work / Skills tabs; the tab shown is remembered on this device, a new note takes its type
  const NTYPES = { personal: 'Personal', work: 'Work', skills: 'Skills' }, NTYPE_KEY = 'aurora_web_note_type';
  let ntype = (() => { try { const t = localStorage.getItem(NTYPE_KEY); return NTYPES[t] ? t : 'personal'; } catch (e) { return 'personal'; } })();
  const paintTabs = () => document.querySelectorAll('#note-tabs .tab').forEach(t => t.setAttribute('aria-selected', String(t.dataset.type === ntype)));
  const setType = async t => { ntype = t; try { localStorage.setItem(NTYPE_KEY, t); } catch (e) {} paintTabs(); await load(); };
  paintTabs();
  // R433: changing tab closes the open note and opens the new tab's first note, or a new one when the tab has none;
  // a note left empty (no title, no text) when you change tab is deleted, so tab-hopping leaves no blank notes behind
  let autoMade = null;   // the id of a note this page created because a tab was empty
  const isEmpty = () => !title.value.trim() && !text.value.trim();
  async function dropIfEmpty(all) {
    if (!cur || !isEmpty() || (!all && cur !== autoMade)) return;
    const id = cur; clearTimeout(timer); timer = 0; pending = false; if (saving) await saving;
    try { await api('/api/notes/delete', { id }); } catch (e) { return; }
    cur = null; if (autoMade === id) autoMade = null;
  }
  // R437: opening Notes and each tab change run one after another, so a quick tab click never lands while the first
  // note is still loading (its text went into the new tab's editor) and two quick clicks never make two blank notes
  let chain = Promise.resolve();
  const serial = f => (chain = chain.then(f, f));
  const switchTab = t => serial(() => t === ntype ? null : doSwitch(t));
  async function doSwitch(t) {
    await dropIfEmpty(true);
    if (cur && !(await flush())) return;
    cur = null; search.value = ''; await setType(t);
    const first = list.querySelector('.n');
    if (first) { await open(first.dataset.id); return; }
    try { const d = await api('/api/notes/create', { title: '', text: '', type: t }); autoMade = d.id; await load(); await open(d.id); }
    catch (e) { say(e.message, true); }
  }
  document.querySelectorAll('#note-tabs .tab').forEach(t => t.onclick = () => switchTab(t.dataset.type));
  if (G('note-type')) G('note-type').onchange = async e => {   // R422: move the open note; the list follows it to its new tab
    const to = e.target.value; if (!cur) return;
    if (!(await flush())) { e.target.value = ntype; return; }   // the typed text is saved before the move
    try { await api('/api/notes/type', { id: cur, type: to }); flash('Moved to ' + NTYPES[to]); await setType(to); list.querySelectorAll('.n').forEach(b => b.toggleAttribute('aria-current', b.dataset.id === cur)); }
    catch (err) { say(err.message, true); e.target.value = ntype; }
  };
  const phone = () => matchMedia('(max-width:768px)').matches;
  const api = async (path, body, keep) => {
    const json = body === undefined ? '' : JSON.stringify(body);
    // R392 (bug 5): browsers refuse keepalive bodies over 64 KiB, so only a small save on page exit uses it
    const r = await fetch(path, body === undefined ? { headers: hdr() } : { method: 'POST', headers: hdr(), body: json, keepalive: !!keep && json.length < 60000 });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(d.error || 'HTTP ' + r.status); e.status = r.status; throw e; }
    return d;
  };
  const when = t => {
    const d = new Date(t * 1000), now = new Date(), days = (new Date(now.getFullYear(), now.getMonth(), now.getDate()) - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000;
    if (days === 0) return d.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' });   // R392 U14: English, like the page
    if (days === 1) return 'Yesterday';
    if (days < 7) return d.toLocaleDateString('en', { weekday: 'short' });
    return d.toLocaleDateString('en', { day: 'numeric', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' });
  };
  const counts = () => { const w = text.value.trim() ? text.value.trim().split(/\s+/).length : 0; meta.textContent = w + (w === 1 ? ' word' : ' words') + ' · ' + text.value.length + ' characters'; };
  const say = (t, err) => { state.textContent = t; state.style.color = err ? 'var(--bad)' : ''; };
  // R468: a one-off confirmation (copied, moved) is a toast under the toolbar for 2.5 s, not the footer's save status
  let flashT = 0;
  const flash = t => { const el = $('note-toast'); if (!el) return say(t); const h = edit.querySelector('.nhead'); el.style.top = (h ? h.offsetTop + h.offsetHeight + 4 : 56) + 'px'; el.textContent = t; el.hidden = false; clearTimeout(flashT); flashT = setTimeout(() => { el.hidden = true; }, 2500); };

  async function load() {
    const q = search.value.trim();   // R422: a search looks in every type, and each hit says which
    let d; try { d = await api('/api/notes?q=' + encodeURIComponent(q) + (q ? '' : '&type=' + ntype)); } catch (e) { say(e.message, true); return; }
    list.replaceChildren();
    if (!d.notes.length) {
      const p = document.createElement('p'); p.className = 'none';
      p.textContent = search.value.trim() ? 'No note matches.' : 'No ' + NTYPES[ntype] + ' notes yet. Use “New note” to write one.'; list.appendChild(p);
    }
    for (const n of d.notes) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'n'; b.dataset.id = n.id;
      if (n.id === cur) b.setAttribute('aria-current', 'true');
      const t = document.createElement('b'); t.textContent = n.title || 'Untitled'; if (!n.title) t.className = 'un';
      const s = document.createElement('small'), a = document.createElement('span'), c = document.createElement('span');
      a.textContent = when(n.updated) + (q && NTYPES[n.type] ? ' · ' + NTYPES[n.type] : ''); c.textContent = mdPlain(n.preview); s.append(a, c); b.append(t, s);
      const g = document.createElement('span'); g.className = 'grip'; g.title = 'Drag to reorder (or Alt+↑/↓)'; g.setAttribute('aria-hidden', 'true');
      g.textContent = '⠿'; g.onpointerdown = e => dragStart(e, b); b.append(g);
      b.onclick = () => { if (!dragged) open(n.id); };
      b.onkeydown = e => { if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) { e.preventDefault(); nudge(b, e.key === 'ArrowUp' ? -1 : 1); } };
      list.appendChild(b);
    }
  }
  // R404: drag a note by its grip (mouse or touch) or Alt+↑/↓ to reorder; the order is kept on the server for every device.
  let dragged = false;
  const rows = () => [...list.querySelectorAll('.n')];
  async function saveOrder(before) {
    const ids = rows().map(r => r.dataset.id);
    if (ids.join() === before.join()) return;
    try { await api('/api/notes/order', { ids }); } catch (e) { say('Order not saved: ' + e.message, true); await load(); }
  }
  function nudge(row, dir) {
    const before = rows().map(r => r.dataset.id), sib = dir < 0 ? row.previousElementSibling : row.nextElementSibling;
    if (!sib || !sib.classList.contains('n')) return;
    if (dir < 0) sib.before(row); else sib.after(row);
    row.focus(); saveOrder(before);
  }
  function dragStart(e, row) {
    if (e.button !== 0 && e.pointerType === 'mouse') return;
    e.preventDefault(); e.stopPropagation();
    const before = rows().map(r => r.dataset.id), pid = e.pointerId;
    let moved = false, scrollT = 0, lastY = e.clientY;
    row.classList.add('dragging');   // listened on the document: moving the row in the DOM would drop a pointer capture
    const place = y => {
      for (const r of rows()) {
        if (r === row) continue;
        const b = r.getBoundingClientRect(), mid = b.top + b.height / 2;
        if (y < mid && r.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING) { r.before(row); moved = true; return; }
        if (y > mid && r.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_PRECEDING) { r.after(row); moved = true; }
      }
    };
    const edge = () => {   // near the top or bottom of the list: scroll it, keep placing
      const b = list.getBoundingClientRect(), d = lastY < b.top + 40 ? -8 : lastY > b.bottom - 40 ? 8 : 0;
      if (d) { list.scrollTop += d; place(lastY); }
      scrollT = requestAnimationFrame(edge);
    };
    scrollT = requestAnimationFrame(edge);
    const move = ev => { if (ev.pointerId !== pid) return; ev.preventDefault(); lastY = ev.clientY; place(lastY); };
    const end = ev => {
      if (ev.pointerId !== pid) return;
      cancelAnimationFrame(scrollT); row.classList.remove('dragging');
      document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', end); document.removeEventListener('pointercancel', end);
      if (moved) { dragged = true; setTimeout(() => { dragged = false; }, 0); saveOrder(before); }
    };
    document.addEventListener('pointermove', move, { passive: false }); document.addEventListener('pointerup', end); document.addEventListener('pointercancel', end);
  }
  const show = on => { edit.hidden = !on; empty.hidden = on; shell.classList.toggle('editing', on && phone()); };
  // R403: notes are Markdown. Edit shows the source, View the formatted note; a note with text opens in View, an empty one in Edit.
  const view = $('note-view'), modeBtn = $('note-mode');
  let viewing = false;
  function setMode(v) {
    viewing = !!v; text.hidden = viewing; view.hidden = !viewing;
    modeBtn.textContent = viewing ? 'Edit' : 'View'; modeBtn.setAttribute('aria-pressed', String(viewing));
    modeBtn.title = viewing ? 'Edit the Markdown source (Ctrl+E)' : 'Show the formatted note (Ctrl+E)';
    if (viewing) paintView();
  }
  function paintView() {
    const top = view.scrollTop;
    mdRender(view, text.value, { task: (line, on) => {   // a tick in View edits that line of the source, then autosaves
      const ls = text.value.split('\n');
      ls[line] = ls[line].replace(/^(\s*(?:[-*+]|\d+[.)])\s+)\[( |x|X)\]/, (m, p) => p + (on ? '[x]' : '[ ]'));
      text.value = ls.join('\n'); touch(); paintView();
    } });
    view.scrollTop = top;
  }
  modeBtn.onclick = () => { setMode(!viewing); (viewing ? view : text).focus(); };
  dlg.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && (e.key === 'e' || e.key === 'E') && !edit.hidden) { e.preventDefault(); modeBtn.click(); } });

  // autosave: a short pause after typing; one request at a time, and one more if typing went on meanwhile
  const dirty = () => timer !== 0 || pending || saving !== null;
  function touch() { counts(); say('Editing…'); failed = false; clearTimeout(timer); timer = setTimeout(flush, 700); }
  async function flush(keep) {
    clearTimeout(timer); timer = 0;
    if (!cur) return true;
    if (saving) { pending = true; await saving; return !failed; }
    const id = cur;
    saving = (async () => {
      try { const d = await api('/api/notes/save', { id, title: title.value, text: text.value, base: curUpdated }, keep); curUpdated = d.updated; failed = false; say('Saved'); }
      catch (e) {
        if (e.status === 404 && id === cur) {   // R392 (bug 14): deleted on another device: keep the text as a new note
          try { const d = await api('/api/notes/create', { title: title.value, text: text.value, type: (G('note-type') || {}).value || ntype }); cur = d.id; curUpdated = null; failed = false; say('This note was deleted on another device: your text was saved as a new note'); return; } catch (e2) { e = e2; }
        }
        if (e.status === 409 && keep) {   // R415 (bug 2): leaving the page, no one to ask: keep the text as a new note
          try { await api('/api/notes/create', { title: (title.value || 'Untitled') + ' (conflict copy)', text: text.value, type: (G('note-type') || {}).value || ntype }, true); failed = false; return; } catch (e5) { e = e5; }
        }
        if (e.status === 409 && id === cur && !keep) {   // R392 (bug 13): changed on another device since it was opened here
          const over = await askConfirm('Changed on another device', 'This note was edited elsewhere since you opened it. Overwrite it with your version, or cancel to load theirs (your last change here is lost).', 'Overwrite');
          if (over) { try { const d = await api('/api/notes/save', { id, title: title.value, text: text.value }); curUpdated = d.updated; failed = false; say('Saved (overwrote the other version)'); return; } catch (e3) { e = e3; } }
          else { try { const n = await api('/api/notes/' + id); title.value = n.title; text.value = n.text; curUpdated = n.updated; counts(); if (viewing) paintView(); failed = false; say('Loaded the other version'); return; } catch (e4) { e = e4; } }
        }
        failed = true; say('Not saved: ' + e.message + ' — keep typing to retry', true);
      }
    })();
    await saving; saving = null;
    if (pending) { pending = false; return flush(keep); }
    if (!failed) await load();
    return !failed;
  }
  async function open(id) {
    if (id === cur) { if (phone()) show(true); return; }
    if (!(await flush())) return;
    try {
      const n = await api('/api/notes/' + id);
      cur = id; curUpdated = n.updated; title.value = n.title; text.value = n.text; say(''); counts(); show(true); setMode(!!n.text.trim());
      if (G('note-type')) G('note-type').value = n.type || 'personal';   // R422
      const src = G('note-src');   // R418 (I3): a note saved from a reply opens that chat again
      if (src) { src.hidden = !n.source_session; src.onclick = async () => { if (await closePanel() && await post('/api/resume', { id: n.source_session })) stick = true; }; }
      list.querySelectorAll('.n').forEach(b => b.toggleAttribute('aria-current', b.dataset.id === id));
      if (!phone() && !n.text && !n.title) title.focus();
    } catch (e) { say(e.message, true); }
  }
  if (G('note-link')) G('note-link').onclick = async () => {   // R460: a link that opens Aurora with this note loaded
    if (!cur) return;
    const url = location.origin + '/?note=' + cur;
    // R468: a short confirmation (the button's tooltip says what the link does); without clipboard access the
    // link is copied the old way, and only if that fails too is it offered in a box to copy by hand
    let ok = false;
    try { await navigator.clipboard.writeText(url); ok = true; }
    catch (e) { const ta = document.createElement('textarea'); ta.value = url; ta.style.cssText = 'position:fixed;opacity:0'; dlg.append(ta); ta.select(); try { ok = document.execCommand('copy'); } catch (e2) {} ta.remove(); }
    if (ok) flash('Link copied');
    else window.prompt('Copy this link:', url);
  };
  async function leave() { await dropIfEmpty(false); if (await flush()) { cur = null; show(false); await load(); } }   // R433: Back drops a blank note the tab made

  const closeNotes = async () => { await dropIfEmpty(false); if (await flush(true) || await askConfirm('Close without saving?', 'The last change could not be saved.', 'Close')) dlg.close(); };
  $('notes-btn').onclick = async () => {   // R392 U17
    if (isPhone()) sidebar(false);
    if (panelDlg === dlg) return;
    if (!(await openPanel(dlg, 'Notes', async () => { await closeNotes(); return !dlg.open; }))) return;
    await serial(async () => {
      search.value = ''; cur = null; show(false); await load();
      const first = list.querySelector('.n'); if (first && !phone()) await open(first.dataset.id);   // R437: a desktop opens on the first note, as a tab change does
    });
  };
  $('note-new').onclick = async () => {
    if (!(await flush())) return;
    try { const d = await api('/api/notes/create', { title: '', text: '', type: ntype }); search.value = ''; cur = null; await load(); await open(d.id); title.focus(); }
    catch (e) { say(e.message, true); }
  };
  $('note-back').onclick = leave;
  $('note-close').onclick = closeNotes;
  dlg.addEventListener('click', e => { if (e.target === dlg && !dlg.classList.contains('panel')) closeNotes(); });   // R411: a tap outside the frame closes it
  dlg.addEventListener('cancel', e => { if (dirty()) { e.preventDefault(); closeNotes(); } });
  dlg.addEventListener('close', () => { cur = null; panelGone(dlg); });
  addEventListener('pagehide', () => { if (cur && dirty()) flush(true); });
  title.oninput = text.oninput = touch;
  title.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); text.focus(); } };
  text.onkeydown = e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); flush(); return; }
    if (e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
    // R440: Enter continues a list ("- ", "1. " counted on, "- [ ] " unticked); Enter on an empty item ends the list;
    // Tab / Shift+Tab indent or outdent a list line by two spaces (elsewhere Tab still moves the focus)
    const v = text.value, a0 = text.selectionStart, a1 = text.selectionEnd, ls = v.lastIndexOf('\n', a0 - 1) + 1;
    const le = v.indexOf('\n', a0) < 0 ? v.length : v.indexOf('\n', a0), line = v.slice(ls, le);
    const m = /^(\s*)([-*+]|(\d{1,9})([.)]))(\s+)(\[[ xX]\]\s+)?/.exec(line);
    if (!m) return;
    if (e.key === 'Enter' && !e.shiftKey && a0 === a1 && a0 >= ls + m[0].length) {
      e.preventDefault();
      if (!line.slice(m[0].length).trim() && a0 === le) { text.setRangeText('', ls, le, 'end'); }   // an empty item: end the list
      else {
        const mark = m[3] ? (parseInt(m[3], 10) + 1) + m[4] : m[2];
        text.setRangeText('\n' + m[1] + mark + m[5] + (m[6] ? '[ ] ' : ''), a0, a1, 'end');
      }
      touch(); return;
    }
    if (e.key === 'Tab' && a0 === a1) {
      e.preventDefault();
      if (e.shiftKey) { const cut = Math.min(2, m[1].length); if (cut) { text.setRangeText('', ls, ls + cut, 'preserve'); text.setSelectionRange(a0 - cut, a0 - cut); } }
      else { text.setRangeText('  ', ls, ls, 'preserve'); text.setSelectionRange(a0 + 2, a0 + 2); }
      touch();
    }
  };
  search.oninput = () => { clearTimeout(searchT); searchT = setTimeout(load, 200); };
  $('note-copy').onclick = async () => {
    try { await navigator.clipboard.writeText(text.value); flash('Copied'); } catch (e) { text.select(); document.execCommand('copy'); flash('Copied'); }
  };
  if ($('note-send')) $('note-send').onclick = async () => {   // R401: attach this note to the next message
    if (!cur || !(await flush())) return;
    if (attachNote(cur, title.value.trim())) { dlg.close(); $('text').focus(); }
  };
  $('note-del').onclick = async () => {
    if (!cur || !await askConfirm('Delete this note?', 'It is removed from every device.', 'Delete')) return;
    clearTimeout(timer); timer = 0;
    try { await api('/api/notes/delete', { id: cur }); cur = null; show(false); await load(); } catch (e) { say(e.message, true); }
  };
  matchMedia('(max-width:768px)').addEventListener('change', () => show(!edit.hidden));
})();

// R401: typing # in the message box opens a note picker; the pick becomes a note chip and the #word is removed.
if ($('notes-btn')) (function () {
  const t = $('text'), box = document.createElement('div'); box.id = 'npick'; box.setAttribute('role', 'listbox'); box.hidden = true;
  $('send').prepend(box);
  let items = [], sel = 0, at = -1, reqN = 0, qT = 0;
  const st = document.createElement('style');
  st.textContent = '#npick{display:flex;flex-direction:column;max-height:40vh;overflow:auto;border:1px solid var(--line,#8884);border-radius:8px;margin:0 0 8px;background:var(--view,inherit)}#npick button{display:block;width:100%;text-align:left;border:0;background:none;color:inherit;padding:8px 16px;font:inherit;cursor:pointer;min-height:40px}#npick button small{display:block;opacity:.7;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}#npick button[aria-selected=true]{background:var(--sel,#8882)}#npick p{margin:0;padding:8px 16px;opacity:.7}#chips .chip.note b,#chips .chip.file b{font-weight:normal}';
  document.head.append(st);
  const hide = () => { box.hidden = true; at = -1; items = []; };
  const token = () => {   // the #word right before the cursor, or null
    const pos = t.selectionStart; if (pos !== t.selectionEnd) return null;
    const m = /(^|\s)#([^\s#]{0,40})$/.exec(t.value.slice(0, pos));
    return m ? { start: pos - m[2].length - 1, q: m[2] } : null;
  };
  function paint() {
    box.replaceChildren();
    if (!items.length) { const p = document.createElement('p'); p.textContent = 'No note matches.'; box.append(p); }
    items.forEach((n, k) => {
      const b = document.createElement('button'); b.type = 'button'; b.setAttribute('role', 'option'); b.setAttribute('aria-selected', String(k === sel));
      b.textContent = n.title || 'Untitled'; const sm = document.createElement('small'); sm.textContent = n.preview; b.append(sm);
      b.onmousedown = e => e.preventDefault();   // keep the focus (and the keyboard) in the message box
      b.onclick = () => pick(k); box.append(b);
    });
    box.hidden = false;
    const cur = box.querySelector('[aria-selected=true]'); if (cur) cur.scrollIntoView({ block: 'nearest' });
  }
  async function update() {
    const tk = token(); if (!tk) { hide(); return; }
    at = tk.start; const n = ++reqN;
    let d; try { const r = await fetch('/api/notes?q=' + encodeURIComponent(tk.q), { headers: hdr() }); d = await r.json(); if (!r.ok) throw 0; } catch (e) { hide(); return; }
    if (n !== reqN || !token()) return;   // a newer keystroke already asked
    items = (d.notes || []).slice(0, 8); sel = 0; paint();
  }
  function pick(k) {
    const n = items[k], tk = token(); if (!n || !tk) { hide(); return; }
    if (attachNote(n.id, n.title)) {
      t.setRangeText('', tk.start, t.selectionStart, 'end');
      t.dispatchEvent(new Event('input'));
    }
    hide(); t.focus();
  }
  t.addEventListener('input', () => { clearTimeout(qT); if (!token()) { hide(); return; } qT = setTimeout(update, 120); });
  t.addEventListener('click', () => { if (!box.hidden && !token()) hide(); });
  t.addEventListener('blur', () => setTimeout(() => { if (document.activeElement !== t) hide(); }, 150));
  document.addEventListener('keydown', e => {
    if (box.hidden || e.target !== t || e.isComposing) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); e.stopImmediatePropagation(); if (items.length) { sel = (sel + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length; paint(); } }
    else if ((e.key === 'Enter' || e.key === 'Tab') && items.length) { e.preventDefault(); e.stopImmediatePropagation(); pick(sel); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); hide(); }
  }, true);
})();

// ── R396: offline support ──────────────────────────────────────────────
// A header tag says when the link is down; messages sent meanwhile wait in an outbox (localStorage) and go out,
// in order, when it is back. Each carries an id, so a resend after a lost answer never runs a message twice.
// The draft survives a reload. The service worker (sw.js) keeps the page and the last chats readable offline.
var stallT = 0, wakeNap = null, outbox = [];
const OUTBOX_KEY = 'aurora_web_outbox', DRAFT_KEY = 'aurora_web_draft', OUTBOX_MAX_BYTES = 2_000_000;
try { outbox = JSON.parse(localStorage.getItem(OUTBOX_KEY) || '[]'); if (!Array.isArray(outbox)) outbox = []; } catch (e) { outbox = []; }
const netTag = document.createElement('span'); netTag.className = 'tag'; netTag.id = 'net'; netTag.setAttribute('role', 'status'); netTag.hidden = true;
$('model').after(netTag);
let netNow = 'ok';
function netState(st) {
  netNow = st || netNow;
  const parts = [];
  if (netNow === 'offline') parts.push('offline'); else if (netNow === 'reconnecting') parts.push('reconnecting…');
  if (outbox.length) parts.push(outbox.length + ' waiting to send');
  netTag.textContent = parts.join(' · '); netTag.hidden = !parts.length; netTag.dataset.state = netNow;
  document.documentElement.classList.toggle('offline', netNow !== 'ok');
}
function nap(ms) { return new Promise(r => { const t = setTimeout(done, ms); function done() { clearTimeout(t); wakeNap = null; r(); } wakeNap = done; }); }
const wake = () => { if (wakeNap) wakeNap(); };
addEventListener('online', () => { wake(); flushOutbox(); });
addEventListener('offline', () => netState('offline'));
document.addEventListener('visibilitychange', () => { if (!document.hidden) { wake(); flushOutbox(); } });
function newId() {
  const r = (self.crypto && crypto.randomUUID) ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2);
  return r.replace(/[^A-Za-z0-9_-]/g, '').slice(0, 64);
}
function saveOutbox() {
  try { localStorage.setItem(OUTBOX_KEY, JSON.stringify(outbox)); return true; } catch (e) { return false; }
}
function toOutbox(item) {
  if (JSON.stringify(item).length > OUTBOX_MAX_BYTES) { note('offline: this message (with its pictures) is too big to keep until the connection is back; it stays in the box', true); return false; }
  outbox.push(item);
  if (!saveOutbox()) { outbox.pop(); note('offline: no room to keep this message; it stays in the box', true); return false; }
  paintOutbox(); netState(); return true;
}
function paintOutbox() {
  log.querySelectorAll('.msg.pending').forEach(e => { if (!outbox.some(o => o.id === e.dataset.oid)) e.remove(); });
  for (const o of outbox) if (!log.querySelector('.msg.pending[data-oid="' + o.id + '"]')) {
    const d = add('msg user pending', o.text + (o.images && o.images.length ? '  [' + o.images.length + ' picture' + (o.images.length > 1 ? 's' : '') + ']' : '') + (o.note_ids && o.note_ids.length ? '  [' + o.note_ids.length + ' note' + (o.note_ids.length > 1 ? 's' : '') + ']' : '') + (o.files && o.files.length ? '  [' + o.files.length + ' file' + (o.files.length > 1 ? 's' : '') + ']' : ''));
    d.dataset.oid = o.id; d.title = 'waiting for the connection';
  }
}
async function sendNow(item) {   // 'ok' | 'net' (no answer: keep it) | 'busy' (queue full: keep it) | 'refused' (said why)
  let r;
  mySent.add(item.id);
  try { r = await fetch('/api/send', { method: 'POST', headers: hdr(), body: JSON.stringify({ text: item.text, images: item.images || [], image_paths: item.image_paths || [], note_ids: item.note_ids || [], files: item.files || [], client_id: item.id, temperature: pageTemperature() }) }); }
  catch (e) { netState('offline'); return 'net'; }
  netState('ok');                 // any answer proves the link is up, even while the old stream has not noticed yet
  if (r.ok) return 'ok';
  const d = await r.json().catch(() => ({}));
  if (r.status >= 500) return 'net';
  note(d.error || 'HTTP ' + r.status, true);
  if (r.status === 409) return 'busy';
  return 'refused';
}
let flushing = false;
async function flushOutbox() {
  if (flushing || !outbox.length) return;
  flushing = true;
  try {
    while (outbox.length) {
      const res = await sendNow(outbox[0]);
      if (res === 'net' || res === 'busy') break;   // busy: a full queue frees up when a turn ends ('done' flushes again)
      outbox.shift(); saveOutbox(); paintOutbox();   // sent, or refused for good (the reason is on screen)
    }
  } finally { flushing = false; netState(); }
}
function saveDraft() {
  try { const v = $('text').value; if (v && !editMode) localStorage.setItem(DRAFT_KEY, v); else localStorage.removeItem(DRAFT_KEY); } catch (e) {}
}
$('text').addEventListener('input', saveDraft);
{ const d = (() => { try { return localStorage.getItem(DRAFT_KEY); } catch (e) { return null; } })();
  if (d && !$('text').value) { $('text').value = d; $('text').dispatchEvent(new Event('input')); } }
paintOutbox(); netState();
{ const st = document.createElement('style'); st.textContent = '.msg.pending{opacity:.55;border-style:dashed}#net[data-state=offline]{color:var(--danger,#c0392b)}'; document.head.append(st); }
if ('serviceWorker' in navigator && isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => {});

// ── R397: OpenRouter prices ────────────────────────────────────────────
const usd = v => v == null ? '?' : '$' + (v >= 10 ? v.toFixed(0) : v >= 1 ? v.toFixed(2).replace(/\.?0+$/, '') : String(+v.toFixed(3)));
function priceText(p) { return (p.in === 0 && p.out === 0) ? 'free' : usd(p.in) + ' / ' + usd(p.out) + ' per M'; }
function priceTitle(p) { return 'input ' + usd(p.in) + ', output ' + usd(p.out) + (p.cache_read != null ? ', cache read ' + usd(p.cache_read) : '') + ' per million tokens' + (p.at ? '; updated ' + ago(p.at) : ''); }
function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 90 ? 'just now' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 129600 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' days ago';
}
async function refreshPrices() { return post('/api/prices/refresh', {}); }   // the server says what changed (a notify), or why not

// ── R400: language for voice, and reading replies aloud ────────────────
// One setting (Settings → Appearance → Language, R431; English by default, per browser) drives both the microphone (whisper is told the
// language instead of guessing) and the 🔊 button under each reply, which uses the device's own voices (speechSynthesis).
const LANGS = [['en-GB', 'English (UK)'], ['en-US', 'English (US)'], ['pt-PT', 'Português (Portugal)'], ['pt-BR', 'Português (Brasil)'],
               ['es-ES', 'Español'], ['fr-FR', 'Français'], ['de-DE', 'Deutsch'], ['it-IT', 'Italiano']];
const LANG_KEY = 'aurora_web_lang', READ_KEY = 'aurora_web_read_aloud';
function voiceLang() { try { const v = localStorage.getItem(LANG_KEY); if (LANGS.some(l => l[0] === v)) return v; } catch (e) {} return 'en-GB'; }
const canSpeak = 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window;
{ const anchor = $('hide-tools') && $('hide-tools').closest('label');
  if (anchor) {
    const wrap = document.createElement('label'); wrap.className = 'opt lang-opt'; wrap.htmlFor = 'voice-lang'; wrap.textContent = 'Language ';
    const sel = document.createElement('select'); sel.id = 'voice-lang'; sel.className = 'sel'; sel.title = 'Used by the microphone and by Read aloud';
    for (const [v, t] of LANGS) { const o = document.createElement('option'); o.value = v; o.textContent = t; sel.append(o); }
    sel.value = voiceLang();
    sel.onchange = () => { try { localStorage.setItem(LANG_KEY, sel.value); } catch (e) {} stopSpeaking(); };
    wrap.append(sel); anchor.after(wrap);
    if (canSpeak) {
      const ra = document.createElement('label'); ra.className = 'opt'; ra.title = 'Speak each new reply when it finishes';
      const cb = document.createElement('input'); cb.type = 'checkbox'; cb.id = 'read-aloud'; cb.checked = localStorage.getItem(READ_KEY) === '1';
      cb.onchange = () => { try { localStorage.setItem(READ_KEY, cb.checked ? '1' : '0'); } catch (e) {} if (!cb.checked) stopSpeaking(); };
      ra.append(cb, document.createTextNode(' Read replies aloud')); wrap.after(ra);
    }
  }
  const st = document.createElement('style');
  st.textContent = '.lang-opt{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.lang-opt select{max-width:100%}'
    + '.speak{border:0;background:none;color:var(--dim,#888);cursor:pointer;font:inherit;padding:4px 8px;min-height:32px;align-self:flex-start}'
    + '.speak[aria-pressed=true]{color:var(--accent-fg,inherit);background:var(--accent,#333);border-radius:999px}'
    + '@media (pointer:coarse){.speak{min-height:44px;min-width:44px}}';
  document.head.append(st);
}
function speakableText(els) {   // prose only: code blocks, tables and tool output are skipped, the reader would spell them out
  return els.map(el => { const c = el.cloneNode(true); c.querySelectorAll('pre,table,.speak,button,.tool').forEach(x => x.remove()); return c.textContent; })
    .join('\n').replace(/https?:\/\/\S+/g, 'link').replace(/[*_#>`|]+/g, ' ').replace(/[ \t]+/g, ' ').replace(/\n{2,}/g, '\n').trim();
}
function chunks(text, max = 220) {   // long utterances get cut off on some browsers: speak sentence-sized pieces in order
  const out = []; let cur = '';
  for (const part of text.split(/(?<=[.!?;:])\s+|\n+/)) {
    if ((cur + ' ' + part).length > max && cur) { out.push(cur); cur = part; } else cur = cur ? cur + ' ' + part : part;
    while (cur.length > max) { const k = cur.lastIndexOf(' ', max) > 40 ? cur.lastIndexOf(' ', max) : max; out.push(cur.slice(0, k)); cur = cur.slice(k).trim(); }
  }
  if (cur) out.push(cur);
  return out;
}
let speakingBtn = null;
function stopSpeaking() {
  if (canSpeak) speechSynthesis.cancel();
  if (speakingBtn) { speakingBtn.setAttribute('aria-pressed', 'false'); paintSpeak(speakingBtn, false); speakingBtn = null; }
}
function pickVoice(lang) {
  const vs = speechSynthesis.getVoices(), base = lang.split('-')[0];
  return vs.find(v => v.lang === lang) || vs.find(v => v.lang.replace('_', '-').toLowerCase() === lang.toLowerCase())
    || vs.find(v => v.lang.toLowerCase().startsWith(base)) || null;
}
function speak(text, btn) {
  stopSpeaking();
  if (!canSpeak || !text) return;
  const lang = voiceLang(), voice = pickVoice(lang), parts = chunks(text);
  speakingBtn = btn || null;
  if (btn) { btn.setAttribute('aria-pressed', 'true'); paintSpeak(btn, true); }
  parts.forEach((t, k) => {
    const u = new SpeechSynthesisUtterance(t); u.lang = lang; if (voice) u.voice = voice;
    if (k === parts.length - 1) u.onend = u.onerror = () => { if (speakingBtn === btn) stopSpeaking(); };
    speechSynthesis.speak(u);
  });
}
function paintSpeak(b, on) {   // R436: the chat page draws it like the other reply actions; the other pages keep the glyph
  if (!b.classList.contains('act')) { b.textContent = on ? '■' : '🔊'; return; }
  const n = actBtn(on ? 'Stop' : 'Read aloud', '', on ? ICON_STOP : ICON_SPEAK); b.replaceChildren(...n.childNodes);
}
// After a turn: one button under its reply that reads every reply part since the last user message.
function speakButton(isLive, only) {
  if (!canSpeak) return;
  let els = [];
  if (only) els = [only];
  else { for (let n = (lastUser && lastUser.parentNode === log) ? lastUser.nextSibling : null; n; n = n.nextSibling) if (n.classList && n.classList.contains('reply')) els.push(n); }
  if (!els.length) return;
  const last = els[els.length - 1];
  if (last.nextSibling && last.nextSibling.classList && last.nextSibling.classList.contains('speak')) return;
  const b = document.createElement('button'); b.type = 'button'; b.className = window.AURORA_CHAT ? 'btn act speak' : 'speak'; paintSpeak(b, false);
  b.title = 'Read aloud'; b.setAttribute('aria-label', 'Read this reply aloud'); b.setAttribute('aria-pressed', 'false');
  b.onclick = () => { if (speakingBtn === b) stopSpeaking(); else speak(speakableText(els), b); };
  last.after(b);
  if (isLive && localStorage.getItem(READ_KEY) === '1') speak(speakableText(els), b);
}
if (canSpeak) { speechSynthesis.getVoices(); speechSynthesis.onvoiceschanged = () => {}; addEventListener('pagehide', stopSpeaking); }

// R413: a file in workspace/input (R432; was uploads) or workspace/outputs, fetched with the token (an <img src> cannot send it)
async function fileBlob(kind, name, thumb, view) {
  const r = await fetch('/api/files/file?kind=' + kind + '&name=' + encodeURIComponent(name) + (thumb ? '&thumb=1' : view ? '&view=1' : ''), { headers: hdr() }).catch(() => null);
  return r && r.ok ? r.blob() : null;
}
const shortName = n => n.replace(/^\d{8}-\d{6}-[0-9a-f]{8}-/, '');
async function downloadFile(kind, name, as) {
  const b = await fileBlob(kind, name, false); if (!b) return note(shortName(name) + ': not found', true);
  const a = document.createElement('a'); a.href = URL.createObjectURL(b); a.download = as || shortName(name); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}
// R414: a type for every file, by extension: what the row icon says and what the preview shows
const FILE_TYPES = [
  ['image', '🖼️', 'Picture', /\.(png|jpe?g|gif|webp)$/i], ['pdf', '📕', 'PDF', /\.pdf$/i],
  ['markdown', '📝', 'Markdown', /\.(md|markdown)$/i], ['document', '📄', 'Document', /\.(docx|odt|rtf)$/i],
  ['document', '📊', 'Spreadsheet', /\.(xlsx|ods)$/i], ['document', '📽️', 'Slides', /\.(pptx|odp)$/i],
  ['text', '📊', 'Table', /\.(csv|tsv)$/i], ['audio', '🎵', 'Audio', /\.(mp3|wav|ogg|oga|m4a|flac|opus)$/i],
  ['video', '🎬', 'Video', /\.(mp4|m4v|webm|mov)$/i],
  ['text', '💻', 'Code', /\.(py|js|ts|swift|sh|c|h|cpp|java|go|rs|sql|html?|css|svg|xml|json|ya?ml|toml|ini)$/i],
  ['text', '📃', 'Text', /\.(txt|log)$/i],
];
function fileType(name) {
  for (const [kind, icon, label, re] of FILE_TYPES) if (re.test(name)) return { kind, icon, label };
  const ext = (name.match(/\.([A-Za-z0-9]+)$/) || [])[1];
  return { kind: 'text', icon: '📎', label: ext ? ext.toUpperCase() : 'File' };
}
const urls = [];   // blob URLs of the open preview, revoked when it closes
// R419: Prev/Next walk the Files list of the same kind (Input or Outputs), in its order, wrapping at the ends
let pvNav = { kind: null, names: [], i: -1 }, pvSeq = 0, pvListSeq = 0;
async function pvList(kind, name) {
  const my = ++pvListSeq;
  pvNav = { kind, names: [], i: -1 };   // until the list arrives Prev/Next do nothing (never the previous file's list)
  const d = await getJSON('/api/files?kind=' + kind);
  if (my !== pvListSeq) return;         // a newer preview has asked for its own list
  const names = d && d.files ? d.files.map(u => u.name) : [];
  pvNav = { kind, names, i: names.indexOf(name) };
  for (const id of ['pv-prev', 'pv-next']) if ($(id)) $(id).hidden = names.length < 2 || pvNav.i < 0;
}
function pvStep(d) {
  const n = pvNav.names.length; if (n < 2 || pvNav.i < 0 || !$('preview').open) return;
  pvNav.i = (pvNav.i + d + n) % n; previewFile(pvNav.kind, pvNav.names[pvNav.i], true);
}
async function previewFile(kind, name, stepping) {   // R414: picture, PDF, audio, video, markdown, text or an office file's text
  const dlg = $('preview'); if (!dlg) return;
  const seq = ++pvSeq;   // a fast run of Next presses: only the newest file may fill the body
  if (!stepping && $('pv-prev')) { $('pv-prev').hidden = $('pv-next').hidden = true; pvList(kind, name); }
  const body = $('pv-body'), t = fileType(name);
  body.replaceChildren(); urls.splice(0).forEach(URL.revokeObjectURL);
  $('pv-h').textContent = shortName(name); $('pv-dl').onclick = () => downloadFile(kind, name);
  if (!dlg.open) dlg.showModal();
  const wait = document.createElement('p'); wait.className = 'sec'; wait.textContent = 'Loading…'; body.append(wait);
  let el;
  if (['image', 'pdf', 'audio', 'video'].includes(t.kind)) {
    const b = await fileBlob(kind, name, false, true);
    if (b) {
      const u = URL.createObjectURL(b); urls.push(u);
      el = document.createElement(t.kind === 'image' ? 'img' : t.kind === 'pdf' ? 'iframe' : t.kind);
      el.src = u; el.className = 'pv-' + t.kind;
      if (t.kind === 'image') el.alt = shortName(name); else if (t.kind === 'pdf') el.title = shortName(name); else el.controls = true;
    }
  } else {
    const r = await fetch('/api/files/file?kind=' + kind + '&name=' + encodeURIComponent(name) + '&text=1', { headers: hdr() }).catch(() => null);
    const d = r ? await r.json().catch(() => ({})) : {};
    if (r && r.ok) {
      el = document.createElement('div');
      if (d.kind === 'markdown') { el.className = 'msg md pv-md'; mdRender(el, d.text); }
      else { el = document.createElement('pre'); el.className = 'pv-text'; el.textContent = d.text; }
      if (d.cut) { const c = document.createElement('p'); c.className = 'sec'; c.textContent = 'Only the first part is shown. Download for the whole file.'; body.append(c); }
    } else {
      el = document.createElement('p'); el.className = 'empty'; el.textContent = (d.error ? 'No preview: ' + d.error + '.' : 'No preview for this file.') + ' Download it to open it.';
    }
  }
  if (seq !== pvSeq) return;
  wait.remove();
  if (!el) { el = document.createElement('p'); el.className = 'empty'; el.textContent = 'File not found.'; }
  body.prepend(el);
}
if ($('preview')) {
  const dlg = $('preview');
  $('pv-close').onclick = () => dlg.close();
  dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
  dlg.addEventListener('close', () => { pvSeq++; $('pv-body').replaceChildren(); urls.splice(0).forEach(URL.revokeObjectURL); });
  if ($('pv-prev')) {
    $('pv-prev').onclick = () => pvStep(-1); $('pv-next').onclick = () => pvStep(1);
    dlg.addEventListener('keydown', e => {   // R419: ← / → step; not while a player or a text field has the keys
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight' || e.altKey || e.ctrlKey || e.metaKey) return;
      if (e.target.closest('audio, video, input, textarea, select, iframe')) return;
      e.preventDefault(); pvStep(e.key === 'ArrowLeft' ? -1 : 1);
    });
  }
}
function fileIcon(kind, name, u) {   // a real thumbnail for pictures and PDFs, else the type's icon
  const th = document.createElement('span'), t = fileType(name);
  th.className = 'fl-thumb t-' + t.kind; th.textContent = t.icon; th.setAttribute('aria-hidden', 'true');
  if (t.kind === 'image' || t.kind === 'pdf') fileBlob(kind, name, true).then(b => { if (b) { const im = document.createElement('img'); im.alt = ''; im.src = URL.createObjectURL(b); th.replaceChildren(im); } });
  return th;
}
// R413: a reply that names a file in workspace/outputs (or input; an older chat's uploads path is the same folder, R432) shows it under the text: a picture inline,
// anything else as a file card (R414) that opens the preview
const WS_RE = /(?:~|\/[^\s`'"()]*?)\/\.aurora\/workspace\/(input|uploads|outputs)\/([^\s`'"()<>\]\[\/]+)/g;
function showWorkspaceFiles(el, raw) {
  const seen = new Set(), box = document.createElement('div'); box.className = 'out-imgs';
  for (const m of String(raw || '').matchAll(WS_RE)) {
    const kind = m[1] === 'uploads' ? 'input' : m[1], name = m[2].replace(/[.,;:!?]+$/, ''), key = kind + '/' + name;
    if (seen.has(key)) continue; seen.add(key);
    const t = fileType(name);
    if (t.kind === 'image') {
      const cell = document.createElement('div'); cell.className = 'out-img';
      const dl = document.createElement('button'); dl.className = 'btn'; dl.type = 'button'; dl.textContent = 'Download'; dl.onclick = () => downloadFile(kind, name);
      fileBlob(kind, name, false, true).then(b => { if (!b) return; const im = document.createElement('img'); im.alt = shortName(name); im.src = URL.createObjectURL(b); im.onclick = () => previewFile(kind, name); cell.prepend(im); });
      cell.append(dl); box.append(cell);
    } else {
      const card = document.createElement('button'); card.type = 'button'; card.className = 'file-card'; card.title = 'Preview ' + shortName(name);
      const nm = document.createElement('span'); nm.className = 'fc-name'; nm.textContent = shortName(name);
      const ty = document.createElement('span'); ty.className = 'sec'; ty.textContent = t.label;
      const tx = document.createElement('span'); tx.className = 'fc-txt'; tx.append(nm, ty);
      card.append(fileIcon(kind, name), tx); card.onclick = () => previewFile(kind, name); box.append(card);
    }
  }
  el.querySelector(':scope > .out-imgs')?.remove();
  if (box.childElementCount) el.append(box);
}
// R414: row actions are an icon plus a label; a phone shows the icon only, so the name keeps the room
const ICON_DL = '<path d="M12 4v11M7 10l5 5 5-5"/><path d="M5 19h14"/>', ICON_DEL = '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>';
function actBtn(label, aria, paths) {
  const b = document.createElement('button'); b.className = 'btn act'; b.type = 'button'; b.title = aria; b.setAttribute('aria-label', aria);
  b.append(new DOMParser().parseFromString('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + paths + '</svg>', 'image/svg+xml').documentElement);
  const t = document.createElement('span'); t.textContent = label; b.append(t); return b;
}
// R408: the Files dialog (R414; was Uploads; R432: its tabs are Input and Outputs) lists the saved files and deletes them one by one
if ($('files-btn')) (function () {
  const dlg = $('files'), list = $('fl-list');
  let kind = 'input';   // R413: the dialog browses workspace/input (R432) or workspace/outputs
  const DESC = { input: 'Files you attached, saved so Aurora can open them. Deleted automatically after 30 days.',
                 outputs: 'Files Aurora made for you. Kept until you delete them.' };
  const size = n => n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(0) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
  const when = s => { const d = new Date(s * 1000), now = new Date();
    return d.toDateString() === now.toDateString() ? 'today ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : d.toLocaleDateString([], { day: 'numeric', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' }); };
  async function load() {
    const d = await getJSON('/api/files?kind=' + kind); if (!d) return;
    list.textContent = ''; $('fl-desc').textContent = DESC[kind];
    $('fl-all').hidden = !d.files.length;
    if (!d.files.length) { const p = document.createElement('p'); p.className = 'empty'; p.textContent = kind === 'outputs' ? 'No outputs yet.' : 'No input files.'; list.append(p); return; }
    for (const u of d.files) {
      const row = document.createElement('div'); row.className = 'fl-row';
      const open = document.createElement('button'); open.type = 'button'; open.className = 'fl-open'; open.title = 'Preview';
      const nm = document.createElement('span'); nm.className = 'fl-name'; nm.textContent = shortName(u.name); nm.title = u.path;
      const meta = document.createElement('span'); meta.className = 'sec'; meta.textContent = fileType(u.name).label + ' · ' + size(u.size) + ' · ' + when(u.mtime);
      const txt = document.createElement('span'); txt.className = 'fl-txt'; txt.append(nm, meta);   // name over type/size/date, so a phone shows the name
      open.append(fileIcon(kind, u.name), txt); open.onclick = () => previewFile(kind, u.name);
      const k = kind;
      const del = actBtn('Delete', 'Delete ' + nm.textContent, ICON_DEL);
      del.onclick = async () => { if (!await askConfirm(k === 'outputs' ? 'Delete output' : 'Delete input file', 'Delete ' + nm.textContent + '?' + (k === 'input' ? ' The agent can no longer open it.' : ''), 'Delete')) return; if (await post('/api/files/delete', { name: u.name, kind: k })) load(); };
      const dl = actBtn('Download', 'Download ' + nm.textContent, ICON_DL);
      dl.onclick = () => downloadFile(k, u.name, nm.textContent);
      row.append(open, dl, del); list.append(row);
    }
  }
  $('files-btn').onclick = async () => { if (isPhone()) sidebar(false); if (panelDlg === dlg || !(await openPanel(dlg, 'Files'))) return; await load(); };
  $('fl-close').onclick = () => dlg.close();
  dlg.addEventListener('click', e => { if (e.target === dlg && !dlg.classList.contains('panel')) dlg.close(); });   // R411: a tap outside the frame closes it
  dlg.addEventListener('close', () => panelGone(dlg));
  $('fl-all').onclick = async () => { const K = kind === 'input' ? 'Input' : 'Outputs'; if (!await askConfirm('Delete all ' + K + ' files', 'Delete every file in ' + K + '?' + (kind === 'input' ? ' Aurora can no longer open them.' : ''), 'Delete all')) return; if (await post('/api/files/delete_all', { kind })) load(); };
  document.querySelectorAll('#fl-tabs .tab').forEach(t => t.onclick = () => {
    kind = t.dataset.kind; document.querySelectorAll('#fl-tabs .tab').forEach(o => o.setAttribute('aria-selected', String(o === t))); load(); });
})();

// R418: on the chat page Notes and Files take the place of the chat (no overlay) and New Chat reads Back to chat;
// the other pages keep them as dialogs. Leaving Notes saves the open note first.
var panelDlg = null, panelBack = null;
async function openPanel(dlg, name, back) {
  if (!window.AURORA_CHAT) { if (!dlg.open) dlg.showModal(); return true; }
  if (panelDlg && panelDlg !== dlg && !(await closePanel())) return false;
  const main = document.querySelector('.main');
  if (dlg.parentNode !== main) main.querySelector('header').after(dlg);
  dlg.classList.add('panel'); panelDlg = dlg; panelBack = back || (async () => { dlg.close(); return true; });
  if (!dlg.open) dlg.show();
  if (dlg.contains(document.activeElement)) document.activeElement.blur();   // R437: no field focused (and no phone keyboard) on open
  document.body.dataset.panel = name;
  const nc = $('new-chat'); if (!nc._kids) nc._kids = [...nc.childNodes];
  const arrow = icon('M15 6l-6 6 6 6'); arrow.setAttribute('width', 20); arrow.setAttribute('height', 20);
  nc.replaceChildren(arrow, ' Back to chat');
  const t = $('chat-title'); if (t) { if (t.dataset.chat == null) t.dataset.chat = t.textContent; t.textContent = name; }
  return true;
}
// R419: Esc closes the top layer only: a menu or a dialog first (the browser closes a modal one), then Notes/Files go back to chat
let escPopup = false;
addEventListener('keydown', e => { if (e.key === 'Escape') escPopup = !!document.querySelector('#model-menu, #cmd-menu:not([hidden]), dialog[open]:modal'); }, true);
document.addEventListener('keydown', async e => {
  if (e.key !== 'Escape' || e.defaultPrevented || e.isComposing || escPopup || !panelDlg) return;
  if (e.target.closest && e.target.closest('form.nm')) return;   // an inline rename box takes its own Esc
  e.preventDefault(); if (await closePanel()) $('text').focus();
});
async function closePanel() { if (!panelDlg) return true; const ok = await panelBack(); return !!ok; }
function panelGone(dlg) {
  if (dlg !== panelDlg) return;
  panelDlg = panelBack = null; delete document.body.dataset.panel; dlg.classList.remove('panel');
  const nc = $('new-chat'); if (nc._kids) { nc.replaceChildren(...nc._kids); nc._kids = null; }
  const t = $('chat-title'); if (t && t.dataset.chat != null) { t.textContent = t.dataset.chat; delete t.dataset.chat; }
}
// R429: Edit models (in the model menu, R430) edits config.yaml / config.local.yaml as YAML only; the R423 form is gone.
// R427: secrets are shown as «hidden» and put back by the server; the text is validated before it is saved.
var openModels = null;
if (G('models')) (function () {
  const dlg = $('models'), ta = $('cfg-text'), gut = $('cfg-gutter'), msg = $('cfg-msg');
  let view = '', files = {}, loaded = '';
  const say = (t, cls) => { msg.textContent = t; msg.className = 'sec' + (cls ? ' ' + cls : ''); };
  const gutter = () => { const n = ta.value.split('\n').length; let g = ''; for (let i = 1; i <= n; i++) g += i + '\n'; gut.textContent = g; gut.scrollTop = ta.scrollTop; };
  ta.addEventListener('input', () => { gutter(); say(ta.value === loaded ? '' : 'Not saved. Validate checks it; Save checks it, keeps a backup and writes it.'); });
  ta.addEventListener('scroll', () => { gut.scrollTop = ta.scrollTop; });
  ta.addEventListener('keydown', e => { if (e.key === 'Tab' && !e.shiftKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); ta.setRangeText('  ', ta.selectionStart, ta.selectionEnd, 'end'); gutter(); } });
  const dirty = () => !!view && ta.value !== loaded;
  async function leaveRaw() { return !dirty() || await askConfirm('Discard changes?', 'Your edits to ' + view + ' are not saved.', 'Discard'); }
  async function fetchFiles() {
    const d = await getJSON('/api/config/files'); if (!d || !d.files) return false;
    files = {}; d.files.forEach(f => files[f.name] = f);
    document.querySelector('#md-tabs .tab[data-view="config.local.yaml"]').hidden = !files['config.local.yaml'];
    return true;
  }
  function goLine(line, col) {
    const ls = ta.value.split('\n'); let pos = 0; for (let i = 0; i < Math.min(line - 1, ls.length); i++) pos += ls[i].length + 1;
    pos += Math.max(0, (col || 1) - 1); ta.focus(); ta.setSelectionRange(pos, pos);
    ta.scrollTop = Math.max(0, (line - 3) * parseFloat(getComputedStyle(ta).lineHeight));
  }
  async function show(v, force) {
    if (v === view && !force) return;
    if (!force && !(await leaveRaw())) return;
    view = v;
    document.querySelectorAll('#md-tabs .tab').forEach(t => t.setAttribute('aria-selected', String(t.dataset.view === v)));
    if (!(await fetchFiles()) || !files[v]) { ta.value = loaded = ''; gutter(); say('could not read ' + v, 'bad'); return; }
    const f = files[v]; $('cfg-path').textContent = f.path + ' · keys are shown as «hidden» and kept when you save';
    ta.value = loaded = f.text; ta.scrollTop = 0; gutter(); say('');
  }
  document.querySelectorAll('#md-tabs .tab').forEach(t => t.onclick = () => show(t.dataset.view));
  async function check(save) {
    const f = files[view]; if (!f) return;
    say(save ? 'saving…' : 'checking…');
    const r = await fetch('/api/config/' + (save ? 'save' : 'validate'), { method: 'POST', headers: hdr(), body: JSON.stringify({ name: view, text: ta.value, base: f.base }) }).catch(() => null);
    const d = r ? await r.json().catch(() => ({})) : {};
    if (!r || !r.ok) { say((d.errors && d.errors.length > 1 ? d.errors.map(x => '• ' + x).join('\n') : d.error) || 'offline: not checked', 'bad'); if (d.line) goLine(d.line, d.column); return; }
    if (!save) return say('Valid: the YAML parses and every model has a known provider.', 'ok');
    await fetchFiles(); ta.value = loaded = files[view].text; gutter();
    say('Saved' + (d.backup ? ' (backup: ' + d.backup + ')' : ' (no change)') + '. Models reloaded.' + (d.restart ? ' runtime: changes apply after a restart.' : ''), 'ok');
    refreshModel();
  }
  $('cfg-check').onclick = () => check(false);
  $('cfg-save').onclick = () => check(true);
  $('cfg-revert').onclick = async () => { if (ta.value !== loaded && !await askConfirm('Revert?', 'Load ' + view + ' from disk again and drop your edits.', 'Revert')) return; await fetchFiles(); ta.value = loaded = (files[view] || {}).text || ''; gutter(); say(''); };
  const close = async () => { if (await leaveRaw()) { view = ''; dlg.close(); } };
  $('md-close').onclick = close;
  dlg.addEventListener('cancel', e => { if (dirty()) { e.preventDefault(); close(); } else view = ''; });   // Esc asks first when the file was edited
  openModels = async () => { await show('config.yaml', true); if (!dlg.open) dlg.showModal(); ta.focus(); ta.setSelectionRange(0, 0); };
})();
// R421: download one chat as an HTML file; the dialog asks how much of it (brief or everything)
function exportChat(id) {
  const d = $('export'); if (!d) return;
  const x = chatRows.find(r => r.id === id), title = (x && x.first) || '';
  $('ex-no').onclick = () => d.close();
  $('ex-yes').onclick = () => { const level = (d.querySelector('input[name=ex-level]:checked') || {}).value || 'brief'; d.close(); downloadChat(id, level, title); };
  d.showModal();
}
async function downloadChat(id, level, title) {
  const r = await fetch('/api/sessions/' + encodeURIComponent(id) + '/export?level=' + level + '&title=' + encodeURIComponent(title || ''), { headers: hdr() }).catch(() => null);
  if (!r || !r.ok) { note('could not download the chat', true); return false; }
  const slug = (title || 'chat').replace(/[^A-Za-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 50) || 'chat';
  const a = document.createElement('a'); a.href = URL.createObjectURL(await r.blob());
  a.download = 'aurora-' + slug + (level === 'full' ? '-full' : '') + '.html'; document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  return true;
}
// R418 (F1): a read-only link to one chat, for anyone who can reach this server; Stop sharing revokes it
async function shareChat(id) {
  let d; try { const r = await fetch('/api/share', { method: 'POST', headers: hdr(), body: JSON.stringify({ id }) }); d = await r.json(); if (!r.ok) return note(d.error || 'HTTP ' + r.status, true); }
  catch (e) { return note('offline: no link made', true); }
  const url = location.origin + d.path;
  let copied = false; try { await navigator.clipboard.writeText(url); copied = true; } catch (e) {}
  if (await askConfirm('Share link', (copied ? 'Copied. ' : '') + 'Anyone who can open this server and has the link can read this chat (read only, it never runs anything).', 'Stop sharing', url))
    if (await post('/api/share/delete', { token: d.token })) note('link revoked');
}
// R418 (F5): a temperature for this page only, sent with each message; off = the model's own setting
const TEMP_KEY = 'aurora_web_temperature';
function pageTemperature() { const v = (() => { try { return localStorage.getItem(TEMP_KEY); } catch (e) { return null; } })(); return v === null || v === '' ? null : Number(v); }
if (G('temp-on')) {
  const on = G('temp-on'), r = G('temp'), out = G('temp-val'), t = pageTemperature();
  const paint = () => { r.hidden = !on.checked; out.textContent = on.checked ? Number(r.value).toFixed(2) : ''; };
  on.checked = t !== null; if (t !== null) r.value = t; paint();
  on.onchange = () => { try { if (on.checked) localStorage.setItem(TEMP_KEY, r.value); else localStorage.removeItem(TEMP_KEY); } catch (e) {} paint(); };
  r.oninput = () => { try { localStorage.setItem(TEMP_KEY, r.value); } catch (e) {} paint(); };
}
