#!/usr/bin/env python3
"""Local web UI for browsing the Media Index.

Run:  python webui.py  [--port 8765]
Then open http://localhost:8765
"""

import argparse
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse, parse_qs

import media_index as mi

DB_PATH = Path(__file__).parent / "media_index.db"
PAGE_SIZE = 100
CHUNK = 256 * 1024

SORT_COLUMNS = {
    "name": "name COLLATE NOCASE ASC",
    "size": "size DESC",
    "modified": "modified DESC",
    "path": "path COLLATE NOCASE ASC",
}

ILLEGAL_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
GENERIC_DIRS = {
    "pictures", "photos", "photo", "images", "image", "camera roll", "dcim",
    "downloads", "documents", "desktop", "onedrive", "users", "videos",
    "video", "music", "screenshots", "captures", "camera", "saved pictures",
    "pics", "media", "my drive", "google photos", "takeout", "c:",
}

APP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ Index</title>
<style>
  :root {
    --bg: #0f1115; --panel: #181b22; --panel2: #1f2330;
    --text: #e6e9f0; --muted: #8b93a7; --accent: #5b8cff;
    --video: #ff7b72; --audio: #d2a8ff; --image: #56d364;
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--bg); color: var(--text);
         font: 14px/1.5 "Segoe UI", system-ui, sans-serif; }
  .wrap { max-width: 1500px; margin: 0 auto; padding: 24px 20px 60px; }
  h1 { font-size: 22px; font-weight: 600; letter-spacing: .3px; }
  h1 span { color: var(--accent); }
  .sub { color: var(--muted); margin: 2px 0 20px; font-size: 13px; }
  .cards { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
  .card { background: var(--panel); border: 1px solid #262b38; border-radius: 10px;
          padding: 12px 18px; min-width: 150px; }
  .card .num { font-size: 20px; font-weight: 600; }
  .card .lbl { color: var(--muted); font-size: 12px; text-transform: uppercase;
               letter-spacing: .6px; }
  .controls { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
  input, select { background: var(--panel2); color: var(--text);
    border: 1px solid #2c3344; border-radius: 8px; padding: 9px 12px;
    font-size: 14px; outline: none; }
  input:focus, select:focus { border-color: var(--accent); }
  input[type=search] { flex: 1; min-width: 220px; }
  .content { display: flex; gap: 16px; align-items: flex-start; }
  .results { flex: 1; min-width: 0; }
  table { width: 100%; border-collapse: collapse; background: var(--panel);
          border: 1px solid #262b38; border-radius: 10px; overflow: hidden; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #232836;
           white-space: nowrap; }
  td.path { white-space: normal; word-break: break-all; color: var(--muted);
            font-family: Consolas, monospace; font-size: 12.5px; }
  th { background: var(--panel2); color: var(--muted); font-size: 12px;
       text-transform: uppercase; letter-spacing: .5px; position: sticky; top: 0; }
  tbody tr { cursor: pointer; }
  tr:hover td { background: #1d2230; }
  tr.sel td { background: #233252 !important; }
  .badge { display: inline-block; padding: 1px 9px; border-radius: 99px;
           font-size: 11.5px; font-weight: 600; }
  .badge.video { background: #3d1d1d; color: var(--video); }
  .badge.audio { background: #2d2240; color: var(--audio); }
  .badge.image, .badge.photo { background: #16301c; color: var(--image); }
  .badge.graphic { background: #1d2c45; color: #79b8ff; }
  .badge.text { background: #2a2a33; color: #c9d1d9; }
  .badge.data { background: #15302e; color: #4dd4c2; }
  .badge.word { background: #1d2c45; color: #79b8ff; }
  .badge.spreadsheet { background: #16301c; color: var(--image); }
  .badge.presentation { background: #3a2a14; color: #f0a45d; }
  .badge.pdf { background: #3d1d1d; color: var(--video); }
  .topnav { margin-bottom: 6px; }
  .topnav a { color: var(--muted); text-decoration: none; font-size: 13px; }
  .topnav a:hover { color: var(--accent); }
  pre.textprev { white-space: pre-wrap; max-height: 300px; overflow: auto;
    background: var(--panel2); padding: 10px; border-radius: 8px; margin: 12px 0;
    font: 12px/1.5 Consolas, monospace; color: var(--text); word-break: break-all; }
  iframe.preview { height: 380px; border: none; }
  .src { color: var(--muted); font-size: 12.5px; }
  .pager { display: flex; gap: 10px; align-items: center; margin-top: 14px;
           color: var(--muted); }
  button { background: var(--panel2); color: var(--text); border: 1px solid #2c3344;
           border-radius: 8px; padding: 7px 16px; cursor: pointer; font-size: 13.5px; }
  button:hover:not(:disabled) { border-color: var(--accent); }
  button:disabled { opacity: .4; cursor: default; }
  .empty { padding: 40px; text-align: center; color: var(--muted); }
  .panel { width: 380px; flex-shrink: 0; background: var(--panel);
           border: 1px solid #262b38; border-radius: 10px; padding: 16px;
           position: sticky; top: 16px; display: none; }
  .panel.open { display: block; }
  .panel .close { float: right; padding: 2px 10px; font-size: 13px; }
  .panel h2 { font-size: 15px; word-break: break-all; padding-right: 40px; }
  .preview { width: 100%; max-height: 300px; object-fit: contain;
             border-radius: 8px; background: #000; margin: 12px 0; display: block; }
  audio.preview { height: 44px; background: transparent; }
  .noprev { padding: 36px 10px; text-align: center; color: var(--muted);
            background: var(--panel2); border-radius: 8px; margin: 12px 0; }
  .meta { color: var(--muted); font-size: 12.5px; line-height: 1.8;
          word-break: break-all; margin-bottom: 12px; }
  .meta b { color: var(--text); font-weight: 600; }
  .renlbl { font-size: 12px; color: var(--muted); text-transform: uppercase;
            letter-spacing: .5px; margin: 14px 0 6px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
  .chip { background: var(--panel2); border: 1px solid #2c3344; border-radius: 99px;
          padding: 4px 11px; font-size: 12px; cursor: pointer; word-break: break-all; }
  .chip:hover { border-color: var(--accent); color: var(--accent); }
  #newname { width: 100%; margin-bottom: 8px; font-family: Consolas, monospace;
             font-size: 13px; }
  #renameBtn { width: 100%; background: var(--accent); border: none;
               color: #fff; font-weight: 600; }
  #renamemsg { font-size: 12.5px; margin-top: 8px; min-height: 18px; }
  #renamemsg.ok { color: var(--image); }
  #renamemsg.err { color: var(--video); }
</style>
</head>
<body>
<div class="wrap">
  <div class="topnav"><a href="/">&larr; All indexes</a></div>
  <h1>__TITLE__ <span>Index</span></h1>
  <div class="sub" id="sub">Loading&hellip;</div>
  <div class="cards" id="cards"></div>
  <div class="controls">
    <input type="search" id="q" placeholder="Search filename or path&hellip;" autofocus>
    <select id="kind"></select>
    <select id="source">
      <option value="">All sources</option>
      <option value="local">Local</option>
      <option value="onedrive">OneDrive</option>
      <option value="gdrive">Google Drive</option>
    </select>
    <select id="machine"></select>
    <select id="year"><option value="">Any year</option></select>
    <select id="sort">
      <option value="modified">Newest first</option>
      <option value="size">Largest first</option>
      <option value="name">Name A&ndash;Z</option>
      <option value="path">Path A&ndash;Z</option>
    </select>
  </div>
  <div class="content">
    <div class="results">
      <table>
        <thead><tr>
          <th>Name</th><th>Type</th><th>Source</th><th>Machine</th><th>Size</th><th>Modified</th><th>Path</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
      <div class="pager">
        <button id="prev">&larr; Prev</button>
        <span id="pageinfo"></span>
        <button id="next">Next &rarr;</button>
      </div>
    </div>
    <aside class="panel" id="panel">
      <button class="close" id="closePanel">&#10005;</button>
      <h2 id="fname"></h2>
      <div id="pv"></div>
      <div class="meta" id="fmeta"></div>
      <div class="renlbl">Rename &middot; suggestions</div>
      <div class="chips" id="chips"></div>
      <input id="newname" spellcheck="false">
      <button id="renameBtn">Rename file</button>
      <div id="renamemsg"></div>
    </aside>
  </div>
</div>
<script>
const PAGE = __CONFIG__;
const $ = id => document.getElementById(id);
let page = 0, total = 0, lastRows = [], sel = null;

const IMG_OK = ["jpg","jpeg","png","gif","webp","bmp","svg","avif"];
const VID_OK = ["mp4","m4v","webm","mov"];
const AUD_OK = ["mp3","wav","m4a","aac","ogg","flac","opus"];
const TEXT_OK = ["txt","md","markdown","log","json","xml","yaml","yml","csv","tsv","rtf"];

function fmtSize(n) {
  if (n == null || n < 0) return "";
  const u = ["B","KB","MB","GB","TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toLocaleString(undefined, {maximumFractionDigits: 1}) + " " + u[i];
}
const esc = s => (s ?? "").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function loadStats() {
  const s = await (await fetch("/api/stats?domain=" + PAGE.domain)).json();
  const cats = Object.entries(s.categories)
    .map(([c, n]) => `${n.toLocaleString()} ${c}`).join(" / ");
  $("sub").textContent =
    `${s.total.toLocaleString()} ${PAGE.noun} \u00b7 ${fmtSize(s.bytes)} indexed` +
    (cats ? ` \u00b7 ${cats}` : "");
  $("cards").innerHTML = s.sources.map(r =>
    `<div class="card"><div class="num">${r.count.toLocaleString()}</div>
     <div class="lbl">${esc(r.source)} \u00b7 ${fmtSize(r.bytes)}</div></div>`).join("");
}

const KIND_OPTS = PAGE.kindOpts;
const SOURCE_OPTS = [
  ["", "All sources"], ["local", "Local"],
  ["onedrive", "OneDrive"], ["gdrive", "Google Drive"],
];

function setOptions(sel, opts) {
  const cur = sel.value;
  sel.innerHTML = opts.map(o => {
    const disabled = o.count === 0 && o.value && o.value !== cur;
    const n = o.count != null && o.value ? ` (${o.count.toLocaleString()})` : "";
    return `<option value="${esc(o.value)}"${disabled ? " disabled" : ""}>${o.label}${n}</option>`;
  }).join("");
  sel.value = cur;
  if (sel.selectedIndex === -1) sel.value = "";
}

async function updateFacets() {
  const p = new URLSearchParams({
    domain: PAGE.domain, q: $("q").value, kind: $("kind").value,
    source: $("source").value, machine: $("machine").value, year: $("year").value,
  });
  const f = await (await fetch("/api/facets?" + p)).json();
  setOptions($("kind"), KIND_OPTS.map(([v, l]) => ({
    value: v, label: l,
    count: !v ? null
      : ["video", "audio", "image"].includes(v) ? (f.kinds[v] || 0)
      : (f.categories[v] || 0),
  })));
  setOptions($("source"), SOURCE_OPTS.map(([v, l]) => ({
    value: v, label: l, count: v ? (f.sources[v] || 0) : null,
  })));
  const curMachine = $("machine").value;
  const devices = f.devices || [];
  if (curMachine && !devices.some(d => d.value === curMachine))
    devices.unshift({value: curMachine, label: curMachine, count: 0});
  setOptions($("machine"), [{value: "", label: "All machines", count: null}, ...devices]);
  const curYear = $("year").value;
  const years = f.years.map(y => ({value: y.value, label: y.value, count: y.count}));
  if (curYear && !f.years.some(y => y.value === curYear))
    years.unshift({value: curYear, label: curYear, count: 0});
  setOptions($("year"), [{value: "", label: "Any year", count: null}, ...years]);
}

async function search() {
  const p = new URLSearchParams({
    domain: PAGE.domain, q: $("q").value, kind: $("kind").value,
    source: $("source").value, machine: $("machine").value,
    year: $("year").value, sort: $("sort").value, page,
  });
  const d = await (await fetch("/api/search?" + p)).json();
  total = d.total;
  lastRows = d.rows;
  $("rows").innerHTML = d.rows.length ? d.rows.map((r, i) => `<tr data-i="${i}"
      class="${sel && sel.id === r.id ? "sel" : ""}">
      <td>${esc(r.name)}</td>
      <td><span class="badge ${r.category || r.kind}">${r.category || r.kind}</span></td>
      <td class="src">${esc(r.source)}</td>
      <td class="src">${esc(r.device_label || "-")}</td>
      <td>${fmtSize(r.size)}</td>
      <td class="src">${r.modified ? esc(r.modified.slice(0,10)) : ""}</td>
      <td class="path">${esc(r.path)}</td>
    </tr>`).join("")
    : `<tr><td colspan="7" class="empty">No matches</td></tr>`;
  const pages = Math.max(1, Math.ceil(total / d.page_size));
  $("pageinfo").textContent =
    `${total.toLocaleString()} result(s) \u00b7 page ${page + 1} of ${pages}`;
  $("prev").disabled = page === 0;
  $("next").disabled = page >= pages - 1;
}

async function openPanel(r) {
  sel = r;
  $("panel").classList.add("open");
  $("fname").textContent = r.name;
  const url = "/api/file?id=" + r.id;
  let pv;
  if (r.kind === "image" && IMG_OK.includes(r.ext))
    pv = `<img class="preview" src="${url}" alt="">`;
  else if (r.kind === "video" && VID_OK.includes(r.ext))
    pv = `<video class="preview" src="${url}" controls preload="metadata"></video>`;
  else if (r.kind === "audio" && AUD_OK.includes(r.ext))
    pv = `<audio class="preview" src="${url}" controls preload="metadata"></audio>`;
  else if (r.ext === "pdf")
    pv = `<iframe class="preview" src="${url}" title="PDF preview"></iframe>`;
  else if (r.kind === "document" && TEXT_OK.includes(r.ext))
    pv = `<pre class="textprev" id="textprev">Loading\u2026</pre>`;
  else
    pv = `<div class="noprev">No browser preview for .${esc(r.ext)} files</div>`;
  $("pv").innerHTML = pv;
  if ($("textprev")) {
    fetch(url).then(resp => resp.text()).then(txt => {
      if (sel === r && $("textprev"))
        $("textprev").textContent =
          txt.slice(0, 20000) + (txt.length > 20000 ? "\\n\u2026 (truncated)" : "");
    }).catch(() => { if ($("textprev")) $("textprev").textContent = "Preview failed"; });
  }
  $("fmeta").innerHTML =
    `<b>${esc(r.category || r.kind)}</b> \u00b7 ${fmtSize(r.size)} \u00b7 ` +
    `${r.modified ? esc(r.modified.slice(0,10)) : "no date"} \u00b7 ${esc(r.source)} \u00b7 ${esc(r.device_label || "-")}<br>` +
    `${esc(r.path)}`;
  $("newname").value = r.name;
  $("renamemsg").textContent = "";
  $("renamemsg").className = "";
  $("chips").innerHTML = `<span class="src">Loading suggestions\u2026</span>`;
  const s = await (await fetch("/api/suggest?id=" + r.id)).json();
  if (sel !== r) return;  // user clicked another row meanwhile
  $("chips").innerHTML = s.suggestions.map(n =>
    `<span class="chip" title="Use this name">${esc(n)}</span>`).join("") ||
    `<span class="src">No suggestions</span>`;
}

$("rows").addEventListener("click", e => {
  const tr = e.target.closest("tr[data-i]");
  if (!tr) return;
  document.querySelectorAll("#rows tr.sel").forEach(x => x.classList.remove("sel"));
  tr.classList.add("sel");
  openPanel(lastRows[+tr.dataset.i]);
});
function closePanel() {
  $("panel").classList.remove("open");
  sel = null;
  document.querySelectorAll("#rows tr.sel").forEach(x => x.classList.remove("sel"));
}
$("closePanel").onclick = closePanel;
$("chips").addEventListener("click", e => {
  if (e.target.classList.contains("chip")) $("newname").value = e.target.textContent;
});
$("renameBtn").onclick = async () => {
  if (!sel) return;
  $("renameBtn").disabled = true;
  $("renamemsg").textContent = "Renaming\u2026";
  $("renamemsg").className = "";
  try {
    const res = await (await fetch("/api/rename", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({id: sel.id, name: $("newname").value}),
    })).json();
    if (res.ok) {
      sel.name = res.name; sel.path = res.path; sel.ext = res.ext;
      $("fname").textContent = res.name;
      $("renamemsg").textContent = "Renamed \u2713";
      $("renamemsg").className = "ok";
      search();
    } else {
      $("renamemsg").textContent = res.error || "Rename failed";
      $("renamemsg").className = "err";
    }
  } catch (err) {
    $("renamemsg").textContent = "Rename failed: " + err;
    $("renamemsg").className = "err";
  }
  $("renameBtn").disabled = false;
};

let t;
$("q").addEventListener("input", () => {
  clearTimeout(t);
  t = setTimeout(() => { page = 0; closePanel(); search(); updateFacets(); }, 250);
});
for (const id of ["kind","source","machine","year"])
  $(id).addEventListener("change", () => { page = 0; closePanel(); search(); updateFacets(); });
$("sort").addEventListener("change", () => { page = 0; search(); });
$("prev").onclick = () => { page--; search(); };
$("next").onclick = () => { page++; search(); };

setOptions($("kind"), KIND_OPTS.map(([v, l]) => ({value: v, label: l, count: null})));
setOptions($("source"), SOURCE_OPTS.map(([v, l]) => ({value: v, label: l, count: null})));
setOptions($("machine"), [{value: "", label: "All machines", count: null}]);
loadStats();
updateFacets();
search();
</script>
</body>
</html>
"""

LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>File Index Hub</title>
<style>
  :root { --bg: #0f1115; --panel: #181b22; --panel2: #1f2330; --text: #e6e9f0;
          --muted: #8b93a7; --accent: #5b8cff; }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--bg); color: var(--text);
         font: 14px/1.5 "Segoe UI", system-ui, sans-serif; }
  .wrap { max-width: 900px; margin: 0 auto; padding: 60px 20px; }
  h1 { font-size: 28px; font-weight: 600; }
  h1 span { color: var(--accent); }
  .sub { color: var(--muted); margin: 4px 0 36px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
          gap: 20px; }
  a.bigcard { display: block; background: var(--panel); border: 1px solid #262b38;
    border-radius: 14px; padding: 26px; text-decoration: none; color: var(--text);
    transition: border-color .15s, transform .15s; }
  a.bigcard:hover { border-color: var(--accent); transform: translateY(-2px); }
  .icon { font-size: 34px; margin-bottom: 10px; }
  .bigcard h2 { font-size: 19px; margin-bottom: 4px; }
  .bigcard .desc { color: var(--muted); font-size: 13px; margin-bottom: 16px; }
  .stats { font-size: 14px; color: var(--muted); line-height: 1.9; }
  .stats b { color: var(--text); font-size: 17px; }
  .open { color: var(--accent); font-size: 13.5px; font-weight: 600;
          margin-top: 14px; display: inline-block; }
</style>
</head>
<body>
<div class="wrap">
  <h1>File <span>Index</span> Hub</h1>
  <div class="sub">Your local, OneDrive, and Google Drive files, indexed and searchable.</div>
  <div class="grid">
    <a class="bigcard" href="/media">
      <div class="icon">&#127916;</div>
      <h2>Media Index</h2>
      <div class="desc">Photos, computer images, video, and audio</div>
      <div class="stats" id="media-stats">Loading&hellip;</div>
      <span class="open">Open &rarr;</span>
    </a>
    <a class="bigcard" href="/documents">
      <div class="icon">&#128196;</div>
      <h2>Documents Index</h2>
      <div class="desc">Text, data, Word, spreadsheets, presentations, and PDFs</div>
      <div class="stats" id="documents-stats">Loading&hellip;</div>
      <span class="open">Open &rarr;</span>
    </a>
  </div>
</div>
<script>
function fmtSize(n) {
  if (n == null || n < 0) return "0 B";
  const u = ["B","KB","MB","GB","TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toLocaleString(undefined, {maximumFractionDigits: 1}) + " " + u[i];
}
for (const domain of ["media", "documents"]) {
  fetch("/api/stats?domain=" + domain).then(r => r.json()).then(s => {
    const cats = Object.entries(s.categories)
      .map(([c, n]) => `${n.toLocaleString()} ${c}`).join(" \u00b7 ");
    document.getElementById(domain + "-stats").innerHTML =
      `<b>${s.total.toLocaleString()}</b> files \u00b7 ${fmtSize(s.bytes)}<br>` +
      `<span style="font-size:12.5px">${cats || "&nbsp;"}</span>`;
  });
}
</script>
</body>
</html>
"""

PAGES = {
    "media": {
        "domain": "media",
        "title": "Media",
        "noun": "media files",
        "kindOpts": [
            ["", "All types"], ["video", "Video"], ["audio", "Audio"],
            ["image", "Image (any)"], ["photo", "&#128247; Photos"],
            ["graphic", "&#128421; Computer images"],
        ],
    },
    "documents": {
        "domain": "documents",
        "title": "Documents",
        "noun": "documents",
        "kindOpts": [
            ["", "All types"], ["text", "&#128221; Text"], ["data", "&#128202; Data"],
            ["word", "&#128196; Word"], ["spreadsheet", "&#128200; Spreadsheets"],
            ["presentation", "&#128253; Presentations"], ["pdf", "&#128195; PDF"],
        ],
    },
}


def render_app(page_key: str) -> bytes:
    cfg = PAGES[page_key]
    html = APP_HTML.replace("__TITLE__", cfg["title"])
    html = html.replace("__CONFIG__", json.dumps(cfg))
    return html.encode()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_file_row(fid):
    conn = db()
    row = conn.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
    conn.close()
    return row


MEDIA_KINDS = ("video", "audio", "image")
CATEGORY_VALUES = {"photo", "graphic",
                   "text", "data", "word", "spreadsheet", "presentation", "pdf"}


def _domain_clause(params) -> str:
    domain = params.get("domain", ["media"])[0]
    if domain == "documents":
        return "kind = 'document'"
    return "kind IN ('video', 'audio', 'image')"


def api_stats(params):
    dom = _domain_clause(params)
    conn = db()
    sources = [
        {"source": r["source"], "count": r["c"], "bytes": r["b"] or 0}
        for r in conn.execute(
            f"SELECT source, COUNT(*) c, SUM(size) b FROM files "
            f"WHERE {dom} GROUP BY source")
    ]
    total, total_bytes = conn.execute(
        f"SELECT COUNT(*), SUM(size) FROM files WHERE {dom}").fetchone()
    years = [r[0] for r in conn.execute(
        f"SELECT DISTINCT substr(modified,1,4) y FROM files "
        f"WHERE {dom} AND modified != '' ORDER BY y DESC")]
    categories = dict(conn.execute(
        f"SELECT category, COUNT(*) FROM files "
        f"WHERE {dom} AND category IS NOT NULL GROUP BY category"))
    conn.close()
    return {"sources": sources, "total": total, "bytes": total_bytes or 0,
            "years": years, "categories": categories}


def _build_where(params, exclude: str | None = None):
    """WHERE clause from filter params, optionally ignoring one facet dimension."""
    where, args = [_domain_clause(params)], []
    q = params.get("q", [""])[0].strip()
    kind = params.get("kind", [""])[0]
    source = params.get("source", [""])[0]
    machine = params.get("machine", [""])[0]
    year = params.get("year", [""])[0]
    if q:
        where.append("(name LIKE ? OR path LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if exclude != "kind":
        if kind in MEDIA_KINDS:
            where.append("kind = ?")
            args.append(kind)
        elif kind in CATEGORY_VALUES:
            where.append("category = ?")
            args.append(kind)
    if exclude != "source" and source in ("local", "onedrive", "gdrive"):
        where.append("source = ?")
        args.append(source)
    if exclude != "machine" and machine:
        where.append("device_id = ?")
        args.append(machine)
    if exclude != "year" and year.isdigit() and len(year) == 4:
        where.append("substr(modified,1,4) = ?")
        args.append(year)
    return " AND ".join(where), args


def api_facets(params):
    """Valid options per filter, each computed with the *other* filters applied."""
    conn = db()
    cond, args = _build_where(params, exclude="kind")
    kinds = dict(conn.execute(
        f"SELECT kind, COUNT(*) FROM files WHERE {cond} GROUP BY kind", args))
    categories = dict(conn.execute(
        f"SELECT category, COUNT(*) FROM files WHERE {cond} "
        f"AND category IS NOT NULL GROUP BY category", args))
    cond, args = _build_where(params, exclude="source")
    sources = dict(conn.execute(
        f"SELECT source, COUNT(*) FROM files WHERE {cond} GROUP BY source", args))
    cond, args = _build_where(params, exclude="machine")
    devices = [{"value": r[0], "label": r[1] or "Unknown machine", "count": r[2]}
               for r in conn.execute(
        f"SELECT device_id, device_label, COUNT(*) FROM files "
        f"WHERE {cond} GROUP BY device_id, device_label ORDER BY device_label", args)]
    cond, args = _build_where(params, exclude="year")
    years = [{"value": r[0], "count": r[1]} for r in conn.execute(
        f"SELECT substr(modified,1,4) y, COUNT(*) FROM files "
        f"WHERE {cond} AND modified != '' GROUP BY y ORDER BY y DESC", args)]
    conn.close()
    return {"kinds": kinds, "categories": categories,
            "sources": sources, "devices": devices, "years": years}


def api_search(params):
    sort = SORT_COLUMNS.get(params.get("sort", [""])[0], SORT_COLUMNS["modified"])
    try:
        page = max(0, int(params.get("page", ["0"])[0]))
    except ValueError:
        page = 0
    cond, args = _build_where(params)

    conn = db()
    total = conn.execute(f"SELECT COUNT(*) FROM files WHERE {cond}", args).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        f"SELECT id, source, device_id, device_label, path, name, ext, kind, size, modified, category "
        f"FROM files WHERE {cond} ORDER BY {sort} LIMIT ? OFFSET ?",
        args + [PAGE_SIZE, page * PAGE_SIZE])]
    conn.close()
    return {"total": total, "rows": rows, "page": page, "page_size": PAGE_SIZE}


# --- rename suggestions ---------------------------------------------------------

def _sanitize(text: str) -> str:
    return re.sub(r"[-\s]+", "-", ILLEGAL_NAME_CHARS.sub(" ", text)).strip("-")


def _meaningful_folder(path: str) -> str | None:
    username = os.environ.get("USERNAME", "").lower()
    for part in reversed(re.split(r"[\\/]", path)[:-1]):
        p = part.strip().lower()
        if (not p or p in GENERIC_DIRS or p == username
                or re.fullmatch(r"\d{1,4}", p) or p.endswith(":")):
            continue
        return _sanitize(part)
    return None


def api_suggest(params):
    row = get_file_row(params.get("id", [""])[0])
    if not row:
        return {"suggestions": []}
    ext = row["ext"]
    label = row["category"] or row["kind"]
    date = (row["modified"] or "")[:10]
    model = None
    if row["source"] in ("local", "onedrive") and ext in ("jpg", "jpeg", "tif", "tiff"):
        exif = mi.read_jpeg_exif(row["path"]) or {}
        dt = exif.get("datetime_original")
        if dt and len(dt) >= 10:
            date = dt[:10].replace(":", "-")
        model = exif.get("model")

    folder = _meaningful_folder(row["path"])
    candidates = []
    if date:
        if folder:
            candidates.append(f"{date}_{folder}_{label}")
        if model:
            candidates.append(f"{date}_{_sanitize(model)}")
        candidates.append(f"{date}_{label}")
    if folder:
        candidates.append(f"{folder}_{date or label}")
    stem = row["name"].rsplit(".", 1)[0]
    cleaned = _sanitize(re.sub(r"[._]+", " ", stem))
    if cleaned and cleaned.lower() != stem.lower():
        candidates.append(cleaned)

    seen, suggestions = set(), []
    for cand in candidates:
        full = f"{cand}.{ext}" if ext else cand
        if full.lower() not in seen and full != row["name"]:
            seen.add(full.lower())
            suggestions.append(full)
    return {"suggestions": suggestions[:5]}


# --- rename ---------------------------------------------------------------------

def api_rename(body):
    row = get_file_row(body.get("id"))
    if not row:
        raise ValueError("File not found in index")
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise ValueError("Name is empty")
    if ILLEGAL_NAME_CHARS.search(new_name) or new_name in (".", ".."):
        raise ValueError('Name contains illegal characters (<>:"/\\|?*)')
    if "." not in new_name and row["ext"]:
        new_name += "." + row["ext"]
    if new_name == row["name"]:
        raise ValueError("Name is unchanged")

    if row["source"] in ("local", "onedrive"):
        old = Path(row["path"])
        new = old.with_name(new_name)
        if new.exists():
            raise ValueError("A file with that name already exists")
        try:
            old.rename(new)
        except OSError as exc:
            raise ValueError(f"Rename failed: {exc}") from exc
        new_path = str(new)
    else:
        old_rel = row["path"]
        new_rel = str(PurePosixPath(old_rel).with_name(new_name))
        proc = subprocess.run(
            [mi.find_rclone(), "moveto",
             f"{mi.GDRIVE_REMOTE}{old_rel}", f"{mi.GDRIVE_REMOTE}{new_rel}"],
            capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            raise ValueError(f"Google Drive rename failed: {proc.stderr.strip()[:300]}")
        new_path = new_rel

    new_ext = new_name.rsplit(".", 1)[-1].lower() if "." in new_name else ""
    conn = db()
    conn.execute("UPDATE files SET path = ?, name = ?, ext = ? WHERE id = ?",
                 (new_path, new_name, new_ext, row["id"]))
    conn.commit()
    conn.close()
    return {"ok": True, "name": new_name, "path": new_path, "ext": new_ext}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, LANDING_HTML.encode(), "text/html; charset=utf-8")
        elif url.path in ("/media", "/documents"):
            self._send(200, render_app(url.path[1:]), "text/html; charset=utf-8")
        elif url.path == "/api/stats":
            self._json(api_stats(parse_qs(url.query)))
        elif url.path == "/api/search":
            self._json(api_search(parse_qs(url.query)))
        elif url.path == "/api/facets":
            self._json(api_facets(parse_qs(url.query)))
        elif url.path == "/api/suggest":
            self._json(api_suggest(parse_qs(url.query)))
        elif url.path == "/api/file":
            self._serve_file(parse_qs(url.query))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/rename":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            self._json(api_rename(body))
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)})
        except Exception as exc:  # surface unexpected errors to the UI
            self._json({"ok": False, "error": f"Unexpected error: {exc}"})

    # -- file streaming ----------------------------------------------------------

    def _serve_file(self, params):
        row = get_file_row(params.get("id", [""])[0])
        if not row:
            self._send(404, b"unknown file id", "text/plain")
            return
        ctype = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
        try:
            if row["source"] in ("local", "onedrive"):
                self._stream_local(row["path"], ctype)
            else:
                self._stream_gdrive(row, ctype)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # client cancelled (e.g. video seek); harmless

    def _stream_local(self, path, ctype):
        try:
            size = os.path.getsize(path)
            f = open(path, "rb")
        except OSError:
            self._send(404, b"file missing on disk", "text/plain")
            return
        with f:
            start, end = 0, size - 1
            range_header = self.headers.get("Range")
            match = re.match(r"bytes=(\d*)-(\d*)", range_header or "")
            if match and (match[1] or match[2]):
                if match[1]:
                    start = int(match[1])
                    if match[2]:
                        end = min(int(match[2]), size - 1)
                else:  # suffix range: last N bytes
                    start = max(0, size - int(match[2]))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            length = end - start + 1
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _stream_gdrive(self, row, ctype):
        proc = subprocess.Popen(
            [mi.find_rclone(), "cat", f"{mi.GDRIVE_REMOTE}{row['path']}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if row["size"] and row["size"] > 0:
                self.send_header("Content-Length", str(row["size"]))
            self.end_headers()
            shutil.copyfileobj(proc.stdout, self.wfile, CHUNK)
        finally:
            proc.stdout.close()
            proc.terminate()

    # -- helpers -----------------------------------------------------------------

    def _json(self, obj):
        self._send(200, json.dumps(obj).encode(), "application/json")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # keep the console quiet


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets two instances bind the same port silently;
    # fail loudly instead if the port is already taken.
    allow_reuse_address = False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = Server(("127.0.0.1", args.port), Handler)
    print(f"Media Index UI running at http://localhost:{args.port}  (Ctrl+C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    main()
