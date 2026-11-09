'use strict';
// RecSort のブラウザ画面。画面の構成と言葉はデスクトップ版（organizer.py）に合わせる。
// 処理（調査・候補・保存・ごみ箱への移動）はすべてこのパソコンのサーバー（webapp.py）が行う。

const TOKEN = document.querySelector('meta[name="recsort-token"]').content;
const $ = (sel, root = document) => root.querySelector(sel);
const main = $('#main');

const NAV = [
  ['home', '⌂', 'ホーム'], ['files', '▦', 'ファイル一覧'], ['plan', '✦', '整理のおすすめ'],
  ['chosen', '🗑', '削除候補'], ['settings', '⚙', '設定'],
];
const DECISION_HELP = {
  '未判断': 'まだ判断していません。',
  '残す': 'この録画は残しておきます。',
  '保留': 'あとで確認して決めます。',
  '削除候補': 'ごみ箱に移動する候補に追加します。まだ削除されません。',
  '保護中': '保護を解除するまでごみ箱へ移動できません。',
  'お気に入り': '整理のおすすめから外します（「残す」に近い扱い）。お気に入りを外すと対象に戻ります。',
};

let state = null;            // /api/state の結果
let page = 'home';
let view = {};               // 画面ごとの一時的な状態

// --- 共通 ---------------------------------------------------------------------
const esc = (x) => String(x ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
function size(b) {
  if (b == null) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0; let v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(v < 10 ? 2 : 1)) + ' ' + u[i];
}
function date(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
function duration(sec) {
  if (!sec) return '—';
  const s = Math.round(sec);
  const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60); const r = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}` : `${m}:${String(r).padStart(2, '0')}`;
}
const decisionText = (it) => (it.protected ? '保護中' : it.decision || '未判断');
function badges(it) {
  const d = decisionText(it);
  const cls = { '残す': 'keep', '保留': 'hold', '削除候補': 'cand', '保護中': 'protect' }[d] || '';
  return `<span class="pill ${cls}" title="${esc(DECISION_HELP[d])}">${esc(d)}</span>` + (it.favorite ? '<span class="pill fav" title="' + esc(DECISION_HELP['お気に入り']) + '">★ お気に入り</span>' : '');
}
const mediaUrl = (id) => `/api/media/${id}?t=${encodeURIComponent(TOKEN)}`;
const thumbUrl = (id) => `/api/thumb/${id}?t=${encodeURIComponent(TOKEN)}`;
const thumb = (it) => (it.cloud_only ? '<div class="thumb">オンライン専用</div>' : `<div class="thumb"><img loading="lazy" alt="" src="${thumbUrl(it.id)}" data-fallback="1"></div>`);

async function api(path, body) {
  const opt = { headers: { 'X-RecSort-Token': TOKEN } };
  if (body !== undefined) {
    opt.method = 'POST';
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opt);
  } catch (e) {
    setStatus('RecSort のサーバーに接続できません。起動した黒いウィンドウが閉じていないか確認してください');
    throw e;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    const msg = data.error || `エラー（${res.status}）`;
    await alertModal('うまくいきませんでした', `<p>${esc(msg)}</p>`);
    throw new Error(msg);
  }
  return data;
}

function setStatus(text) { $('#statusbar').textContent = text; }

// --- ダイアログ（確認） -----------------------------------------------------------
function modal(title, html, buttons) {
  return new Promise((resolve) => {
    $('#modal-title').textContent = title;
    $('#modal-body').innerHTML = html;
    const actions = $('#modal-actions');
    actions.innerHTML = '';
    for (const [label, value, cls] of buttons) {
      const b = document.createElement('button');
      b.className = 'btn ' + (cls || '');
      b.textContent = label;
      b.addEventListener('click', () => { $('#modal').hidden = true; resolve(value); });
      actions.appendChild(b);
    }
    $('#modal').hidden = false;
    actions.lastChild.focus();
  });
}
const alertModal = (title, html) => modal(title, html, [['閉じる', true, 'primary']]);

// --- 画面の切り替え ---------------------------------------------------------------
function renderNav() {
  const chosen = state ? state.chosen_count : 0;
  $('#nav').innerHTML = NAV.map(([key, icon, label]) => {
    const active = key === page || (key === 'plan' && page === 'compare') || (key === 'chosen' && ['final', 'result'].includes(page));
    const badge = key === 'chosen' && chosen ? `<span class="badge">${chosen}</span>` : '';
    return `<button data-go="${key}" class="${active ? 'active' : ''}" ${active ? 'aria-current="page"' : ''}><span aria-hidden="true">${icon}</span>${label}${badge}</button>`;
  }).join('');
}

async function go(p, opts = {}) {
  if (state && state.trashing && p !== 'result') return;  // 移動中は画面を移らない
  page = p;
  view = opts;
  location.hash = p;
  renderNav();
  await render();
  main.focus();
  window.scrollTo(0, 0);
}

async function render() {
  const fn = { home, files, plan, compare, chosen, final: finalPage, result, settings }[page] || home;
  try {
    await fn();
  } catch (e) {
    console.error(e);
  }
}

// --- 状態の更新（調査の進み具合） -------------------------------------------------------
let wasScanning = false;
async function refreshState() {
  try {
    state = await api('/api/state');
  } catch (e) {
    return;
  }
  renderNav();
  const p = state.progress || {};
  if (state.trashing) setStatus('ごみ箱へ移動しています…');
  else if (state.scanning) setStatus('調査中: ' + (p.text || '録画を探しています'));
  else if (state.scan_result && state.scan_result.cancelled) setStatus('調査を中断しました。途中までの結果を表示しています。「PCをもう一度調べる」で調べ直せます');
  else setStatus(`録画 ${state.videos} 本・${size(state.video_bytes)} · 最終調査: ${date(state.last_scan)}`);
  if (page === 'home') renderScanBox();
  if (wasScanning && !state.scanning && ['home', 'files', 'plan'].includes(page)) render();  // 調査が終わったら表示を更新
  wasScanning = state.scanning;
}

// --- ホーム ------------------------------------------------------------------------
function banners() {
  let html = '';
  if (state && !state.protection_ok) html += '<div class="note danger"><b>保護の記録を読み込めませんでした。</b> 安全のため、ごみ箱への移動を止めています。設定で記録を確認してください。</div>';
  if (state && !state.ffmpeg) html += '<div class="note warn">FFmpeg が見つかりません。録画の長さ・黒画面・無音・似た録画の判定とプレビュー画像は使えません（完全に同じ録画の検出とごみ箱への移動は使えます）。 <button class="btn small" data-go="settings">設定を開く</button></div>';
  return html;
}
function renderScanBox() {
  const box = $('#scanbox');
  if (!box) return;
  const p = state.progress || {};
  if (state.scanning) {
    const pct = p.total ? Math.round((p.done / p.total) * 100) : null;
    box.innerHTML = `<div class="row"><b>調査中</b><span class="sub">${esc(p.text || '録画を探しています')}</span><span class="spacer"></span><button class="btn small" data-action="cancel-scan">中断</button></div>
      <div class="progress ${pct == null ? 'indeterminate' : ''}"><div style="width:${pct ?? 30}%"></div></div>
      <p class="caption">見つかったものから順に表示します。調査中もほかの画面を見られます。</p>`;
  } else {
    const r = state.scan_result;
    const msg = r && r.error ? `<span class="sub">調査で問題がありました: ${esc(r.error)}</span>` : `<span class="sub">最終調査: ${date(state.last_scan)}</span>`;
    box.innerHTML = `<div class="row">${msg}<span class="spacer"></span><button class="btn" data-action="scan">⟳ PCをもう一度調べる</button></div>`;
  }
}
async function home() {
  if (!state) await refreshState();
  const s = state;
  const free = s.drives.map((d) => `${esc(d.root)} 空き ${size(d.free)} / ${size(d.total)}`).join('<br>') || '—';
  main.innerHTML = `
    <div class="page-head"><div><h1>ホーム</h1><p class="sub">何が容量を使っているかを確認し、整理のおすすめから始めましょう。</p></div></div>
    ${banners()}
    <div class="card" id="scanbox"></div>
    <div class="card hero" style="margin-top:16px">
      <div class="film" aria-hidden="true">✦</div>
      <div class="stack" style="gap:4px;flex:1">
        <h3>どこから確認するとよいか</h3>
        <h2>${s.plan_count ? `整理を考えるとよい録画が ${s.plan_count} 本（${size(s.plan_bytes)}）あります` : (s.videos ? '今のところ、おすすめはありません' : 'まだ録画が見つかっていません')}</h2>
        <p class="sub">${s.duplicate_bytes ? `完全に同じ録画を整理すると、${size(s.duplicate_bytes)} 空く見込みです（それぞれ1本は必ず残します）。` : '重複・極端に短い・黒画面・読み込めない・無音などの録画を、理由付きでおすすめします。'}</p>
      </div>
      <button class="btn primary" data-go="plan">おすすめを確認する</button>
    </div>
    <div class="grid">
      <div class="card stat"><h3>録画</h3><strong>${s.videos} 本</strong><span class="caption">合計 ${size(s.video_bytes)}</span></div>
      <div class="card stat"><h3>整理候補</h3><strong>${s.plan_count} 本</strong><span class="caption">${size(s.plan_bytes)}</span></div>
      <div class="card stat"><h3>重複を整理した場合</h3><strong>${size(s.duplicate_bytes)}</strong><span class="caption">削減の見込み</span></div>
      <div class="card stat"><h3>選んだ削除候補</h3><strong>${s.chosen_count} 本</strong><span class="caption">${size(s.chosen_bytes)}（まだ移動していません）</span></div>
      <div class="card stat"><h3>ドライブの空き容量</h3><span class="caption">${free}</span></div>
    </div>
    <p class="caption">ごみ箱へ移しただけでは空き容量は増えません。Windows のごみ箱を空にすると増えます。</p>`;
  renderScanBox();
}

// --- ファイル一覧 ----------------------------------------------------------------------
const SORTS = {
  '容量が大きい順': (a, b) => b.size - a.size, '容量が小さい順': (a, b) => a.size - b.size,
  '更新日時が新しい順': (a, b) => b.mtime - a.mtime, '更新日時が古い順': (a, b) => a.mtime - b.mtime,
  '名前順': (a, b) => a.name.localeCompare(b.name, 'ja'), '動画時間が長い順': (a, b) => (b.duration || -1) - (a.duration || -1),
};
async function files() {
  const data = await api('/api/files');
  view.items = data.items;
  view.sort = view.sort || '容量が大きい順';
  view.filter = view.filter || 'すべて';
  view.q = view.q || '';
  main.innerHTML = `
    <div class="page-head"><div><h1>ファイル一覧</h1><p class="sub">録画を選ぶと、プレビューと理由を確認して判断できます。</p></div></div>
    ${banners()}
    <div class="row" style="margin-bottom:12px">
      <input type="search" id="q" placeholder="ファイル名・保存場所で検索" value="${esc(view.q)}" aria-label="検索">
      <select id="filter" aria-label="判断で絞り込み">${['すべて', '未判断', '残す', '保留', '削除候補', '保護中', 'お気に入り', '整理候補の理由あり'].map((f) => `<option ${f === view.filter ? 'selected' : ''}>${f}</option>`).join('')}</select>
      <select id="sort" aria-label="並べ替え">${Object.keys(SORTS).map((s) => `<option ${s === view.sort ? 'selected' : ''}>${s}</option>`).join('')}</select>
      <span class="caption" id="count"></span>
    </div>
    <div class="split"><div class="table-wrap"><table><thead><tr><th>名前</th><th>形式</th><th class="num">容量</th><th>更新日時</th><th>判断</th><th>整理候補の理由</th></tr></thead><tbody id="rows"></tbody></table></div>
    <aside class="card detail" id="detail"><p class="sub">一覧から録画を選んでください。</p></aside></div>`;
  $('#q').addEventListener('input', (e) => { view.q = e.target.value; drawRows(); });
  $('#filter').addEventListener('change', (e) => { view.filter = e.target.value; drawRows(); });
  $('#sort').addEventListener('change', (e) => { view.sort = e.target.value; drawRows(); });
  drawRows();
  if (view.selected) showDetail(view.selected);
}
function drawRows() {
  const q = view.q.toLowerCase();
  const f = view.filter;
  let items = view.items.filter((it) => !q || it.name.toLowerCase().includes(q) || it.folder.toLowerCase().includes(q));
  if (f === '保護中') items = items.filter((it) => it.protected);
  else if (f === 'お気に入り') items = items.filter((it) => it.favorite);
  else if (f === '整理候補の理由あり') items = items.filter((it) => it.reasons.length);
  else if (f !== 'すべて') items = items.filter((it) => !it.protected && decisionText(it) === f);
  items.sort(SORTS[view.sort]);
  $('#count').textContent = `${items.length} 本`;
  const shown = items.slice(0, 1000);
  $('#rows').innerHTML = shown.map((it) => `<tr data-id="${it.id}" class="${it.id === view.selected ? 'selected' : ''}" tabindex="0">
    <td>${esc(it.name)}<div class="path">${esc(it.folder)}</div></td><td>${esc(it.ext.replace('.', '').toUpperCase())}</td>
    <td class="num">${size(it.size)}</td><td>${date(it.mtime)}</td><td>${badges(it)}</td>
    <td>${it.reasons.map((r) => `<span class="pill">${esc(r)}</span>`).join('')}</td></tr>`).join('')
    + (items.length > shown.length ? `<tr><td colspan="6" class="caption">ほか ${items.length - shown.length} 本（検索・絞り込みで探してください）</td></tr>` : '')
    || '<tr><td colspan="6" class="empty">該当する録画はありません</td></tr>';
}
function detailHtml(it, opts = {}) {
  const res = it.width ? `${it.width}×${it.height}` : '—';
  const player = it.cloud_only ? '<div class="thumb">オンライン専用のため再生しません（勝手にダウンロードしないため）</div>'
    : `<div class="thumb"><video controls preload="metadata" src="${mediaUrl(it.id)}" data-id="${it.id}"></video></div>`;
  return `${player}
    <h2 style="margin-top:8px">${esc(it.name)}</h2>
    <div>${badges(it)}</div>
    <dl class="info"><dt>容量</dt><dd>${size(it.size)}</dd><dt>再生時間</dt><dd>${duration(it.duration)}</dd><dt>解像度</dt><dd>${res}</dd>
      <dt>更新日時</dt><dd>${date(it.mtime)}</dd><dt>保存場所</dt><dd>${esc(it.folder)}</dd></dl>
    ${it.detail.length ? `<h3>整理候補の理由</h3><ul class="plain">${it.detail.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}
    ${opts.actions === false ? '' : `<h3 style="margin-top:8px">判断</h3>
    <div class="row">${['残す', '保留', '削除候補'].map((d) => `<button class="btn small ${it.decision === d ? 'primary' : ''}" data-action="decide" data-id="${it.id}" data-decision="${d}" title="${esc(DECISION_HELP[d])}" ${d === '削除候補' && it.protected ? 'disabled' : ''}>${d}</button>`).join('')}
      <button class="btn small" data-action="decide" data-id="${it.id}" data-decision="" ${it.decision ? '' : 'disabled'}>判断を解除</button></div>
    <div class="row" style="margin-top:8px">
      <button class="btn small" data-action="protect" data-id="${it.id}" data-value="${it.protected ? 0 : 1}" title="${esc(DECISION_HELP['保護中'])}">${it.protected ? '保護を解除' : '🛡 保護する'}</button>
      <button class="btn small" data-action="favorite" data-id="${it.id}" data-value="${it.favorite ? 0 : 1}" title="${esc(DECISION_HELP['お気に入り'])}">${it.favorite ? '★ お気に入りを外す' : '☆ お気に入り'}</button>
      <button class="btn small" data-action="reveal" data-id="${it.id}">保存場所を開く</button>
      ${it.reasons.some((r) => /重複|類似|似た/.test(r)) ? `<button class="btn small" data-action="compare" data-id="${it.id}">比較する</button>` : ''}
    </div>`}`;
}
function showDetail(id) {
  const it = view.items.find((x) => x.id === id);
  if (!it) return;
  view.selected = id;
  document.querySelectorAll('#rows tr').forEach((tr) => tr.classList.toggle('selected', Number(tr.dataset.id) === id));
  $('#detail').innerHTML = detailHtml(it);
  api('/api/viewed', { id }).catch(() => {});
}

// --- 整理のおすすめ --------------------------------------------------------------------
function steps(n) {
  return `<div class="steps">${['おすすめを見る', '残すものを選ぶ', '最終確認'].map((t, i) => (i === n ? `<b>${i + 1}. ${t}</b>` : `<span>${i + 1}. ${t}</span>`)).join('<span>→</span>')}</div>`;
}
async function plan() {
  const q = new URLSearchParams();
  if (view.target) q.set('target', view.target);
  if (view.drive) q.set('drive', view.drive);
  const p = await api('/api/plan?' + q);
  view.plan = p;
  const checked = view.checked || new Set(p.items.filter((it) => it.kind === 'duplicate' || (it.kind === 'similar' && it.confirmed)).map((it) => it.id));
  view.checked = checked;
  const section = (kind, title, desc) => {
    const list = p.items.filter((it) => it.kind === kind);
    if (!list.length) return '';
    return `<h2 style="margin-top:16px">${title}（${list.length} 本・${size(list.reduce((a, it) => a + it.size, 0))}）</h2><p class="caption">${desc}</p>
      <div class="items">${list.map((it) => {
        const r = p.rows[it.id]; const keep = it.keep_id != null ? p.rows[it.keep_id] : null;
        const lock = it.kind === 'similar' && !it.confirmed;
        return `<div class="item ${lock ? 'disabled' : ''}">
          <input type="checkbox" data-check="${it.id}" ${checked.has(it.id) && !lock ? 'checked' : ''} ${lock || r.protected ? 'disabled' : ''} aria-label="${esc(r.name)} を選ぶ">
          ${thumb(r)}
          <div><b>${esc(r.name)}</b> <span class="pill">${esc(it.label)}</span> ${badges(r)}<div class="caption">${size(it.size)} · ${esc(r.folder)}</div>
            <div class="caption">${it.reasons.map(esc).join('<br>')}</div>${keep ? `<div class="caption">残す1本: ${esc(keep.name)}（${esc(keep.folder)}）</div>` : ''}</div>
          <div class="row">${it.keep_id != null ? `<button class="btn small" data-action="compare" data-id="${it.id}">比較する</button>` : `<button class="btn small" data-action="preview" data-id="${it.id}">確認する</button>`}</div>
        </div>`;
      }).join('')}</div>`;
  };
  const head = p.target ? `目標（追加で空ける）${size(p.target)} に対して、提案は ${size(p.total)}。${p.achieved ? '目標に届きます。' : `不足 ${size(p.shortage)}（基準を緩めて候補を足すことはしません）。`}`
    : `提案は ${p.items.length} 本・${size(p.total)} です。目標容量を入れると、それに合わせて提案します。`;
  const excluded = Object.entries(p.excluded).filter(([, n]) => n).map(([k, n]) => `${k} ${n} 本`).join('・');
  main.innerHTML = `
    ${steps(0)}
    <div class="page-head"><div><h1>整理のおすすめ</h1><p class="sub">決まったルールによる提案です（AI の判定ではありません）。録画の大切さは判断しません。</p></div></div>
    ${banners()}
    <div class="card row" style="margin-bottom:12px">
      <label>今より <input type="number" id="target" min="0" step="any" placeholder="任意" value="${esc(view.target || '')}" style="width:90px"> GB 空けたい</label>
      <label>ドライブ <select id="drive"><option value="">すべて</option>${p.drives.map((d) => `<option ${d === view.drive ? 'selected' : ''}>${esc(d)}</option>`).join('')}</select></label>
      <button class="btn" data-action="apply-target">この目標で提案</button>
      <span class="spacer"></span><span class="caption" id="selsum"></span>
    </div>
    <div class="note">${head}${excluded ? `<br><span class="caption">おすすめに含めなかった録画: ${excluded}</span>` : ''}${p.unchecked ? `<br><span class="caption">重複の確認がまだの録画が ${p.unchecked} 本あります（調査が終わると増えることがあります）</span>` : ''}</div>
    ${p.items.length ? '' : '<div class="card empty">今のところ、おすすめはありません。目標容量を指定すると、容量が大きい録画なども「要確認」として探します。</div>'}
    ${section('duplicate', '完全に同じ録画（重複）', '中身が完全に一致したコピーです（SHA-256 で確認）。各グループで必ず1本を残します。最初から選んであります。')}
    ${section('similar', '似た録画（要比較）', '同じ内容とは限りません。「比較する」で残す1本を決めると選べるようになります。')}
    ${section('review', '確認が必要な録画', '短い・黒画面・読み込めない・無音などの理由がある録画です。内容を確認してから選んでください（最初は選んでいません）。')}
    ${p.items.length ? `<div class="row" style="margin-top:16px;position:sticky;bottom:44px" ><span class="spacer"></span><button class="btn primary" data-action="add-chosen">チェックした録画を削除候補に追加</button></div>` : ''}`;
  updateSelSum();
}
function updateSelSum() {
  const el = $('#selsum');
  if (!el || !view.plan) return;
  const items = view.plan.items.filter((it) => view.checked.has(it.id) && !(it.kind === 'similar' && !it.confirmed));
  el.textContent = `選択中 ${items.length} 本・${size(items.reduce((a, it) => a + it.size, 0))}`;
}

// --- 比較 --------------------------------------------------------------------------
async function compare() {
  const g = await api('/api/group/' + view.id);
  view.group = g;
  const kindText = { duplicate: '完全に同じ録画（中身が一致）', similar: '似た録画（同じ内容とは限りません）', single: '録画の確認' }[g.kind];
  const many = g.kind === 'similar' && g.items.length > 2;
  main.innerHTML = `
    ${steps(1)}
    <div class="page-head"><div><h1>比較して選ぶ</h1><p class="sub">${kindText} · ${g.items.length} 本。再生して内容を確認できます。</p></div>
      <button class="btn" data-go="plan">← おすすめに戻る</button></div>
    ${view.message ? `<div class="note ok">${esc(view.message)}</div>` : ''}
    <div class="compare">${g.items.map((it) => `<div class="card ${it.id === g.keep_id ? 'recommended' : ''}">
      ${it.id === g.keep_id ? `<p class="caption">【残すおすすめ】${esc(g.keep_reason)}</p>` : ''}
      ${detailHtml(it, { actions: false })}
      <div class="row" style="margin-top:8px">
        <button class="btn primary small" data-action="keep" data-id="${it.id}" title="${g.kind === 'duplicate' ? 'この1本を残し、ほかのコピーを削除候補にします' : many ? 'この1本を残します（ほかの録画は未確認のまま）' : 'この1本を残し、もう1本を削除候補にします'}">これを残す</button>
        ${many ? `<button class="btn small" data-action="decide" data-id="${it.id}" data-decision="削除候補" ${it.protected ? 'disabled' : ''}>削除候補にする</button>` : ''}
      </div></div>`).join('')}</div>
    <p class="caption" style="margin-top:8px">${g.kind === 'duplicate' ? '「これを残す」: ほかのコピーをすべて削除候補にします（まだ移動しません）。' : many ? '3本以上の似た録画では、「これを残す」で選んだ1本だけを残す判断にします。ほかの録画は、確認してから1本ずつ削除候補にしてください。' : '「これを残す」: もう1本を削除候補にします（まだ移動しません）。'} 保護中の録画は削除候補にしません。</p>
    <div class="row" style="margin-top:8px"><button class="btn" data-action="keep-all">すべて残す</button><button class="btn" data-action="hold-all">保留（あとで決める）</button></div>`;
}

// --- 削除候補・最終確認・結果 ------------------------------------------------------------
async function chosen() {
  const data = await api('/api/chosen');
  view.items = data.items;
  view.generation = data.generation;
  const ok = data.items.filter((it) => !it.blocked);
  main.innerHTML = `
    <div class="page-head"><div><h1>削除候補</h1><p class="sub">削除候補にした録画です。最終確認のあとで、Windows のごみ箱へ移します（完全には削除しません）。</p></div></div>
    ${banners()}
    ${data.items.length ? '' : '<div class="card empty">削除候補はまだありません。「整理のおすすめ」やファイル一覧から追加できます。<br><br><button class="btn primary" data-go="plan">おすすめを確認する</button></div>'}
    <div class="items">${data.items.map((it) => `<div class="item ${it.blocked ? 'disabled' : ''}">
      <span aria-hidden="true">${it.blocked ? '⚠' : '🗑'}</span>${thumb(it)}
      <div><b>${esc(it.name)}</b> ${badges(it)}<div class="caption">${size(it.size)} · ${esc(it.folder)}</div>
        ${it.blocked ? `<div class="caption">移動できません: ${esc(it.blocked)}</div>` : ''}${it.keeper ? `<div class="caption">同じ内容の残す録画: ${esc(it.keeper)}</div>` : ''}</div>
      <div class="row"><button class="btn small" data-action="preview" data-id="${it.id}">確認する</button><button class="btn small" data-action="decide" data-id="${it.id}" data-decision="">候補から外す</button></div>
    </div>`).join('')}</div>
    ${data.items.length ? `<div class="row" style="margin-top:16px"><span class="caption">移動できるもの ${ok.length} 本・${size(ok.reduce((a, it) => a + it.size, 0))}</span><span class="spacer"></span>
      <button class="btn primary" data-go="final" ${ok.length && state.protection_ok ? '' : 'disabled'}>最終確認へ →</button></div>` : ''}`;
}
async function finalPage() {
  const data = await api('/api/chosen');
  const ok = data.items.filter((it) => !it.blocked);
  view.ids = ok.map((it) => it.id);
  view.generation = data.generation;
  const total = ok.reduce((a, it) => a + it.size, 0);
  main.innerHTML = `
    ${steps(2)}
    <div class="page-head"><div><h1>最終確認</h1><p class="sub">${ok.length} 本・合計 ${size(total)} を Windows のごみ箱へ移します。名前と保存場所を確認してください。</p></div>
      <button class="btn" data-go="chosen">← 戻る</button></div>
    <div class="note warn">移動の直前に、ファイルが変わっていないか・保護されていないか・同じ内容の残す録画があるかをもう一度確認し、問題があれば移動しません。ごみ箱に入らない場合（完全削除になる場合）は中止します。</div>
    <div class="table-wrap"><table><thead><tr><th>ファイル</th><th>保存場所</th><th class="num">容量</th></tr></thead><tbody>
      ${ok.map((it) => `<tr><td>${esc(it.name)}</td><td class="path">${esc(it.folder)}</td><td class="num">${size(it.size)}</td></tr>`).join('')}</tbody></table></div>
    ${data.items.length > ok.length ? `<p class="caption">移動できない ${data.items.length - ok.length} 本は対象から外しました（理由は削除候補の画面に表示しています）。</p>` : ''}
    <div class="row" style="margin-top:16px"><span class="spacer"></span><button class="btn danger" data-action="trash" ${ok.length ? '' : 'disabled'}>ごみ箱へ移す</button></div>
    <div id="trashprogress"></div>`;
}
async function runTrash() {
  const total = view.generation;
  const ids = view.ids;
  const sum = (await api('/api/chosen')).items.filter((it) => ids.includes(it.id)).reduce((a, it) => a + it.size, 0);
  const yes = await modal('ごみ箱へ移しますか？', `<p><b>${ids.length} 本・合計 ${size(sum)}</b> を Windows のごみ箱へ移します。</p>
    <ul class="plain"><li>完全には削除しません。ごみ箱から元に戻せます。</li><li>ごみ箱を空にするまで、空き容量は増えません。</li></ul>`,
  [['いいえ', false], ['はい、ごみ箱へ移す', true, 'danger']]);
  if (!yes) return;
  await api('/api/trash', { ids, generation: total, confirm: true });
  state.trashing = true;
  document.querySelectorAll('button').forEach((b) => { b.disabled = true; });
  const box = $('#trashprogress');
  box.innerHTML = '<div class="card" style="margin-top:12px"><div class="row"><b id="tp-text">ごみ箱へ移動しています…</b><span class="spacer"></span><button class="btn small" id="tp-cancel">中断</button></div><div class="progress"><div id="tp-bar" style="width:0%"></div></div><p class="caption">中断すると、いま処理中のファイルが終わったところで止まります。</p></div>';
  $('#tp-cancel').disabled = false;
  $('#tp-cancel').addEventListener('click', () => { api('/api/trash/cancel', {}); $('#tp-cancel').disabled = true; });
  for (;;) {
    const st = await api('/api/trash');
    $('#tp-text').textContent = `ごみ箱へ移動しています… ${st.current} / ${st.total}: ${st.name || ''}`;
    $('#tp-bar').style.width = `${st.total ? (st.current / st.total) * 100 : 0}%`;
    if (!st.running) {
      state.trashing = false;
      view.trash = st;
      await refreshState();
      return go('result', { trash: st });
    }
    await new Promise((r) => setTimeout(r, 400));
  }
}
async function result() {
  const st = view.trash || await api('/api/trash');
  const rs = st.results || [];
  const group = (status) => rs.filter((r) => r.status === status);
  const block = (title, list, cls) => (list.length ? `<h2 style="margin-top:16px">${title}（${list.length} 本）</h2><div class="table-wrap"><table><tbody>${list.map((r) => `<tr><td>${esc(r.path)}</td><td class="num">${size(r.size)}</td><td class="caption">${esc(r.message)}${r.record_error ? `<br>記録の保存に失敗: ${esc(r.record_error)}` : ''}</td></tr>`).join('')}</tbody></table></div>` : '');
  const moved = group('moved');
  main.innerHTML = `
    <div class="page-head"><div><h1>整理の結果</h1></div></div>
    ${st.error ? `<div class="note danger">移動を続けられませんでした: ${esc(st.error)}。移動できたファイルの結果は保存されています。</div>` : ''}
    <div class="note ${moved.length ? 'ok' : 'warn'}">ごみ箱へ移動した ${moved.length} 本・${size(moved.reduce((a, r) => a + r.size, 0))}。ごみ箱を空にすると空き容量が増えます。元に戻すときは Windows のごみ箱から復元してください。</div>
    ${block('移動した', moved)}${block('安全確認で止めた', group('skipped'))}${block('移動に失敗した', group('failed'))}
    <div class="row" style="margin-top:16px"><button class="btn primary" data-go="home">ホームへ</button><button class="btn" data-go="plan">おすすめを確認する</button></div>`;
}

// --- 設定 --------------------------------------------------------------------------
async function settings() {
  const s = await api('/api/settings');
  const list = (key, title, desc) => `<div class="card"><h2>${title}</h2><p class="caption">${desc}</p>
    <ul class="plain">${s[key].map((p) => `<li>${esc(p)} <button class="btn small" data-action="remove-path" data-key="${key}" data-path="${esc(p)}">外す</button></li>`).join('') || '<li class="caption">なし</li>'}</ul>
    <div class="row"><input type="text" id="add-${key}" placeholder="例: D:\\録画" style="flex:1;min-width:200px" aria-label="${title}に追加するフルパス"><button class="btn small" data-action="add-path" data-key="${key}">追加</button></div></div>`;
  main.innerHTML = `
    <div class="page-head"><div><h1>設定</h1></div></div>
    <div class="stack">
      <div class="card"><h2>FFmpeg（録画の長さ・代表場面・プレビュー画像に使用）</h2>
        <p>${s.ffmpeg ? `✓ 見つかりました（${esc(s.ffmpeg_source)}）` : '見つかりません。アプリは自動でインストールしません。'}</p>
        <p class="caption">インストール例: <code>winget install Gyan.FFmpeg</code>。指定しない場合は PATH と WinGet の場所を探します。</p>
        <div class="row"><input type="text" id="ffmpeg_dir" value="${esc(s.ffmpeg_dir)}" placeholder="ffmpeg.exe があるフォルダ（任意）" style="flex:1;min-width:240px" aria-label="FFmpeg のフォルダ"><button class="btn" data-action="save-ffmpeg">保存</button></div></div>
      <div class="card"><h2>自動調査</h2><label class="row"><input type="checkbox" id="auto_scan" ${s.auto_scan ? 'checked' : ''}> 起動したときに自動で調べる</label></div>
      <div class="card"><h2>調べている場所</h2><p class="caption">ビデオ・デスクトップ・ダウンロードなどのフォルダを先に調べ、その後に内蔵ドライブ全体を調べます。Windows・Program Files・AppData などのシステムの領域は除外します。</p>
        <ul class="plain">${s.locations.map((l) => `<li>${esc(l.label || l.path)}</li>`).join('') || '<li class="caption">まだ調べていません</li>'}</ul>
        ${s.not_scanned.length ? `<p class="caption">自動では調べないドライブ（外付け・ネットワーク・リムーバブル）: ${s.not_scanned.map(esc).join('、')}</p>` : ''}</div>
      ${list('extra_locations', '追加で調べる場所', '外付け・ネットワークドライブやフォルダを調べたいときに追加します。次の調査から対象になります。')}
      ${list('excluded_folders', '除外する場所', 'ここに入れたフォルダの中は調べません。')}
      <div class="card"><h2>データの保存場所</h2><p class="caption">${esc(s.data_dir)}（設定・判断・保護・調査結果。データベースは使いません）</p></div>
    </div>`;
  $('#auto_scan').addEventListener('change', (e) => api('/api/settings', { auto_scan: e.target.checked }));
}

// --- 操作 --------------------------------------------------------------------------
async function decide(ids, decision) {
  await api('/api/decide', { ids, decision: decision || null });
  await refreshState();
}
async function preview(id) {
  const all = await api('/api/files');
  const it = all.items.find((x) => x.id === id);
  if (!it) return;
  await modal('録画の確認', detailHtml(it, { actions: false }), [['閉じる', true, 'primary']]);
  $('#modal-body').innerHTML = '';  // 再生を止める
}

document.addEventListener('click', async (e) => {
  const nav = e.target.closest('[data-go]');
  if (nav) { e.preventDefault(); go(nav.dataset.go); return; }
  const tr = e.target.closest('#rows tr[data-id]');
  if (tr) { showDetail(Number(tr.dataset.id)); return; }
  const el = e.target.closest('[data-action]');
  if (!el || el.disabled) return;
  const id = Number(el.dataset.id);
  const a = el.dataset.action;
  try {
    if (a === 'scan') { await api('/api/scan', {}); await refreshState(); }
    else if (a === 'cancel-scan') { await api('/api/scan/cancel', {}); }
    else if (a === 'decide') {
      await decide([id], el.dataset.decision);
      if (page === 'files') { const it = view.items.find((x) => x.id === id); if (it) it.decision = el.dataset.decision || null; drawRows(); showDetail(id); }
      else render();
    } else if (a === 'protect' || a === 'favorite') {
      await api('/api/' + a, { id, value: el.dataset.value === '1' });
      if (page === 'files') { const it = view.items.find((x) => x.id === id); if (it) it[a === 'protect' ? 'protected' : 'favorite'] = el.dataset.value === '1'; drawRows(); showDetail(id); }
      await refreshState();
    } else if (a === 'reveal') { await api('/api/reveal', { id }); }
    else if (a === 'compare') { go('compare', { id, back: page }); }
    else if (a === 'preview') { await preview(id); }
    else if (a === 'apply-target') {
      const t = $('#target');
      if (!t.reportValidity()) return;
      view = { target: t.value, drive: $('#drive').value };
      render();
    } else if (a === 'add-chosen') {
      const ids = view.plan.items.filter((it) => view.checked.has(it.id) && !(it.kind === 'similar' && !it.confirmed)).map((it) => it.id);
      if (!ids.length) { await alertModal('削除候補に追加', '<p>録画が選ばれていません。</p>'); return; }
      await decide(ids, '削除候補');
      const go2 = await modal('削除候補に追加しました', `<p>${ids.length} 本を削除候補に追加しました。まだ移動していません。最終確認でごみ箱へ移すかを決めます。</p>`, [['おすすめに戻る', false], ['削除候補を見る', true, 'primary']]);
      if (go2) go('chosen'); else { view.checked = null; render(); }
    } else if (a === 'keep') {
      const g = view.group;
      const others = g.kind === 'duplicate' || g.items.length === 2 ? g.items.filter((x) => x.id !== id).map((x) => x.id) : [];
      const r = await api('/api/keep', { keep: id, ids: others });
      const name = g.items.find((x) => x.id === id).name;
      view.message = others.length ? `「${name}」を残します。ほかの ${r.count} 本を削除候補に追加しました（まだ移動していません）` : `「${name}」を残します。`;
      await refreshState();
      render();
    } else if (a === 'keep-all' || a === 'hold-all') {
      await decide(view.group.items.map((x) => x.id), a === 'keep-all' ? '残す' : '保留');
      view.message = a === 'keep-all' ? `このグループの ${view.group.items.length} 本をすべて残します` : 'このグループは「保留」にしました（候補からいったん外れます）';
      render();
    } else if (a === 'trash') { await runTrash(); }
    else if (a === 'save-ffmpeg') { await api('/api/settings', { ffmpeg_dir: $('#ffmpeg_dir').value }); await refreshState(); render(); }
    else if (a === 'add-path' || a === 'remove-path') {
      const key = el.dataset.key;
      const cur = (await api('/api/settings'))[key];
      const next = a === 'add-path' ? [...cur, $('#add-' + key).value.trim()].filter(Boolean) : cur.filter((p) => p !== el.dataset.path);
      await api('/api/settings', { [key]: next });
      render();
    }
  } catch (err) {
    console.error(err);
  }
});
document.addEventListener('change', (e) => {
  const c = e.target.closest('[data-check]');
  if (c && view.checked) {
    const id = Number(c.dataset.check);
    if (c.checked) view.checked.add(id); else view.checked.delete(id);
    updateSelSum();
  }
});
document.addEventListener('keydown', (e) => {
  const tr = e.target.closest && e.target.closest('#rows tr[data-id]');
  if (tr && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); showDetail(Number(tr.dataset.id)); }
});
// サムネイルを作れなかったとき（FFmpeg がない・読めない）は、文字で示す
document.addEventListener('error', (e) => {
  const img = e.target;
  if (img.tagName === 'IMG' && img.dataset.fallback) img.parentElement.textContent = 'プレビューなし';
  if (img.tagName === 'VIDEO') {
    const box = img.parentElement;
    box.innerHTML = `<div class="caption" style="padding:12px;text-align:center">この形式はブラウザで再生できません。<br><button class="btn small" data-action="reveal" data-id="${img.dataset.id}">保存場所を開く</button></div>`;
  }
}, true);
window.addEventListener('hashchange', () => {
  const p = location.hash.slice(1);
  if (p && p !== page && NAV.some(([k]) => k === p)) go(p);
});

(async () => {
  const start = location.hash.slice(1);
  await refreshState();
  go(NAV.some(([k]) => k === start) ? start : 'home');
  setInterval(refreshState, 1500);
})();
