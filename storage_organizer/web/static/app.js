"use strict";

const PRIORITY_LABEL = { 3: "高", 2: "中", 1: "低", 0: "問題なし" };
const CATEGORIES = ["短すぎる", "重複", "画質が粗い", "音声が不明瞭", "長すぎる", "読み込みエラー"];

let items = [];
let selected = new Set();
let filter = null;      // null | "p3" | "p2" | "p1" | カテゴリ名
let folderList = [];    // [{path, on}]
let scanned = false;    // スキャン完了後は上のボタンが「削除予定に移動」になる
let pendingPath = "";

// ---------------------------------------------------------------- 共通

const $ = id => document.getElementById(id);

function api(path, body) {
  const opts = body === undefined
    ? { headers: { "X-Token": TOKEN } }
    : { method: "POST", headers: { "X-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(body) };
  return fetch(path, opts).then(r => r.json());
}

const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtSize = b => b >= 2 ** 30 ? (b / 2 ** 30).toFixed(2) + " GB" : (b / 2 ** 20).toFixed(1) + " MB";
const sumSize = list => list.reduce((a, i) => a + i.size, 0);

function fmtDur(s) {
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => (t.hidden = true), 5000);
}

// ---------------------------------------------------------------- フォルダ選択

function drawFolders() {
  $("folders").innerHTML = folderList.length
    ? folderList.map((f, i) =>
        `<label><input type="checkbox" ${f.on ? "checked" : ""} onchange="folderList[${i}].on = this.checked"> ${esc(f.path)}</label>`).join("")
    : '<div class="meta">フォルダを追加してください</div>';
}

function addFolder() {
  const v = $("newFolder").value.trim().replace(/^"|"$/g, "");
  if (!v) return;
  folderList.push({ path: v, on: true });
  $("newFolder").value = "";
  drawFolders();
}

// ---------------------------------------------------------------- スキャン

// 上のボタン: スキャン前は「スキャン開始」、スキャン後は「削除予定に移動」
function mainAction() {
  scanned ? confirmMove() : scan();
}

function setScanned(on) {
  scanned = on;
  const b = $("mainBtn");
  b.textContent = on ? "削除予定に移動" : "スキャン開始";
  b.className = on ? "danger" : "primary";
  $("rescanBtn").hidden = !on;
}

async function scan() {
  const folders = folderList.filter(f => f.on).map(f => f.path);
  if (!folders.length) return toast("フォルダを1つ以上選んでください");
  const r = await api("/api/scan", { folders, blur: $("blur").checked });
  if (r.error) return toast(r.error);
  setScanned(false);
  $("mainBtn").disabled = true;
  $("rescanBtn").hidden = true;
  $("prog").hidden = false;
  $("result").hidden = true;
  $("footer").hidden = true;
  selected.clear();
  poll();
}

async function poll() {
  const s = await api("/api/status");
  $("bar").style.width = (s.total ? Math.round(s.done / s.total * 100) : 0) + "%";
  $("progText").textContent = s.state === "scanning" ? `${s.message} ${s.done} / ${s.total} 本` : s.message;
  if (s.state === "scanning") return setTimeout(poll, 800);
  $("mainBtn").disabled = false;
  if (s.state === "error") return toast(s.message);
  if (s.state !== "done") return;
  $("prog").hidden = true;
  setScanned(true);
  items = s.items;
  $("result").hidden = false;
  $("footer").hidden = false;
  render();
}

// ---------------------------------------------------------------- 一覧表示

function visibleItems() {
  let v = items.filter(i => $("showOk").checked || i.priority > 0);
  if (filter) v = v.filter(i => filter.startsWith("p") ? i.priority === +filter.slice(1) : i.categories.includes(filter));
  const sorters = {
    size: (a, b) => b.size - a.size,
    dur: (a, b) => a.duration - b.duration,
    date: (a, b) => a.mtime.localeCompare(b.mtime),
    pri: (a, b) => b.priority - a.priority || b.size - a.size,
  };
  return v.sort(sorters[$("sort").value]);
}

function renderSummary() {
  const stat = (label, count, list) =>
    `<div class="stat"><b>${count} 本</b><span>${label}<br>${fmtSize(sumSize(list))}</span></div>`;
  $("summary").innerHTML = stat("見つかった動画", items.length, items)
    + [3, 2, 1].map(p => {
        const g = items.filter(i => i.priority === p);
        return stat(`削除推奨度「${PRIORITY_LABEL[p]}」`, g.length, g);
      }).join("");
}

function renderChips() {
  const chip = (key, label, n) =>
    `<span class="chip ${filter === key ? "on" : ""}" data-key="${key ?? ""}">${esc(label)} (${n})</span>`;
  const flagged = items.filter(i => i.priority > 0).length;
  $("chips").innerHTML = chip(null, "すべて", flagged)
    + [3, 2, 1].map(p => chip("p" + p, "推奨度" + PRIORITY_LABEL[p], items.filter(i => i.priority === p).length)).join("")
    + CATEGORIES.map(c => [c, items.filter(i => i.categories.includes(c)).length])
        .filter(([, n]) => n).map(([c, n]) => chip(c, c, n)).join("");
  $("chips").querySelectorAll(".chip").forEach(el =>
    el.addEventListener("click", () => { filter = el.dataset.key || null; render(); }));
}

function cardHtml(i) {
  const sel = selected.has(i.id);
  const reasons = i.reasons.length
    ? `<ul class="reasons">${i.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>`
    : '<div class="meta">問題は見つかりませんでした</div>';
  return `<div class="card ${sel ? "sel" : ""}" data-id="${i.id}">
    <div class="thumb" data-act="play" title="クリックで再生">
      ${i.thumb ? `<img loading="lazy" src="/thumb?id=${i.id}&t=${TOKEN}" alt="">` : ""}
      <div class="play">▶</div>
      <span class="pri p${i.priority}">削除推奨度: ${PRIORITY_LABEL[i.priority]}</span>
      <span class="dur">${fmtDur(i.duration)}</span>
    </div>
    <div class="body">
      <div class="name">${esc(i.name)}</div>
      <div class="meta">${fmtSize(i.size)} ・ ${esc(i.res || "映像なし")} ・ ${esc(i.mtime)}</div>
      <div class="meta path">${esc(i.folder)}</div>
      ${reasons}
    </div>
    <div class="actions">
      <button data-act="play">▶ 再生</button>
      <button data-act="folder">📁 場所を開く</button>
      <label><input type="checkbox" data-act="select" ${sel ? "checked" : ""}> 削除</label>
    </div>
  </div>`;
}

function render() {
  renderSummary();
  renderChips();
  const v = visibleItems();
  $("grid").innerHTML = v.length ? v.map(cardHtml).join("") : '<div class="empty">該当する動画はありません 🎉</div>';
  updateFooter();
}

// カード内のボタンはイベント委譲でまとめて処理
$("grid").addEventListener("click", e => {
  const actEl = e.target.closest("[data-act]");
  const card = e.target.closest(".card");
  if (!actEl || !card) return;
  const id = card.dataset.id, act = actEl.dataset.act;
  if (act === "select") {
    actEl.checked ? selected.add(id) : selected.delete(id);
    card.classList.toggle("sel", actEl.checked);
    updateFooter();
  } else {
    openFile(id, act);
  }
});

// ---------------------------------------------------------------- 選択と「削除予定」への移動
// ※ このアプリは削除しない。削除予定フォルダの中身をごみ箱へ移すのは利用者自身が行う

function selectWhere(fn) { items.filter(fn).forEach(i => selected.add(i.id)); render(); }
function selectVisible() { visibleItems().forEach(i => selected.add(i.id)); render(); }
function clearSelection() { selected.clear(); render(); }
const selectedItems = () => items.filter(i => selected.has(i.id));

function updateFooter() {
  const s = selectedItems();
  $("selInfo").innerHTML = s.length
    ? `<b>${s.length} 本</b>（<b>${fmtSize(sumSize(s))}</b>）を選択中`
    : "削除したい動画の「削除」にチェックを入れてください";
  $("footerMoveBtn").disabled = !s.length;
  if (scanned) $("mainBtn").disabled = !s.length;
}

async function openFile(id, mode) {
  const r = await api("/api/open", { id, mode });
  if (r.error) toast(r.error);
}

function confirmMove() {
  const s = selectedItems();
  if (!s.length) return toast("移動する動画の「削除」にチェックを入れてください");
  $("modalInfo").innerHTML = `<b>${s.length} 本</b>・合計 <b>${fmtSize(sumSize(s))}</b> を<br>`
    + `<b>${esc(pendingPath)}</b> へ移動します（フォルダが無ければ作成します）。<br>`
    + '<span class="meta">動画は削除されません。移動後に中身を確認し、不要なものはご自身でごみ箱へ移動してください。</span>';
  $("modalList").innerHTML = s.map(i => `<li>${esc(i.path)}</li>`).join("");
  $("modal").hidden = false;
}

function closeModal() { $("modal").hidden = true; }

async function doMove() {
  closeModal();
  const r = await api("/api/move", { ids: [...selected] });
  if (r.error) return toast(r.error);
  const ok = new Set(r.results.filter(x => x.ok).map(x => x.id));
  const ng = r.results.filter(x => !x.ok);
  const movedSize = sumSize(items.filter(i => ok.has(i.id)));
  items = items.filter(i => !ok.has(i.id));
  ok.forEach(id => selected.delete(id));
  render();
  if (ok.size) {
    $("movedText").innerHTML = `<b>${ok.size} 本</b>（${fmtSize(movedSize)}）を <b>${esc(r.folder)}</b> へ移動しました。`;
    $("movedNotice").hidden = false;
  }
  if (ng.length) toast(`${ng.length} 本は移動できませんでした: ${ng.map(x => `${x.name || ""} ${x.error}`).join("、")}`);
}

async function openPending() {
  const r = await api("/api/open-pending", {});
  if (r.error) toast(r.error);
}

async function quitApp() {
  await api("/api/shutdown", {});
  document.body.innerHTML = '<div class="empty" style="padding:80px">アプリを終了しました。このタブは閉じてください。</div>';
}

// ---------------------------------------------------------------- 起動

$("newFolder").addEventListener("keydown", e => { if (e.key === "Enter") addFolder(); });
api("/api/defaults").then(d => {
  folderList = d.folders.map(p => ({ path: p, on: true }));
  pendingPath = d.pending;
  drawFolders();
});
