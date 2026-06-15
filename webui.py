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
import socket
import sqlite3
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse, parse_qs

import media_index as mi
import scan_jobs
import file_ops

DB_PATH = Path(__file__).parent / "media_index.db"
PAGE_SIZE = 100
CHUNK = 256 * 1024
# Max files returned per duplicate group; a few hash groups can hold thousands
# of identical copies, and shipping/rendering them all freezes the browser.
GROUP_FILE_CAP = 50

# Set from CLI args in main(); the footer reads these to advertise the LAN URL.
BIND_HOST = "127.0.0.1"
BIND_PORT = 8765

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
  .controls { display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px; }
  .controls-search input { width: 100%; box-sizing: border-box; }
  .controls-filters { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .controls-filters select { flex: 1 1 160px; min-width: 160px; max-width: 100%; }
  .controls-filters #machine { flex: 2 1 220px; min-width: 220px; }
  #resetBtn { border-color: #3d4458; color: var(--muted); flex: 0 0 auto; }
  #resetBtn:hover { color: var(--text); border-color: var(--accent); }
  input, select { background: var(--panel2); color: var(--text);
    border: 1px solid #2c3344; border-radius: 8px; padding: 9px 12px;
    font-size: 14px; outline: none; }
  input:focus, select:focus { border-color: var(--accent); }
  .content { display: flex; gap: 16px; align-items: flex-start; }
  .results { flex: 1; min-width: 0; }
  /* Fixed layout makes the table fill its container and lets every cell
     truncate to a single line with an ellipsis. */
  table { width: 100%; table-layout: fixed; border-collapse: collapse; background: var(--panel);
          border: 1px solid #262b38; border-radius: 10px; overflow: hidden; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #232836;
           white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  td.path { color: var(--muted); font-family: Consolas, monospace; font-size: 12.5px; }
  /* Column widths (sum ~100%); Name and Path take the flexible share. */
  th:nth-child(1), td:nth-child(1) { width: 23%; }
  th:nth-child(2), td:nth-child(2) { width: 8%; }
  th:nth-child(3), td:nth-child(3) { width: 9%; }
  th:nth-child(4), td:nth-child(4) { width: 12%; }
  th:nth-child(5), td:nth-child(5) { width: 8%; }
  th:nth-child(6), td:nth-child(6) { width: 10%; }
  th:nth-child(7), td:nth-child(7) { width: 30%; }
  th { background: var(--panel2); color: var(--muted); font-size: 12px;
       text-transform: uppercase; letter-spacing: .5px; position: sticky; top: 0; }
  tbody tr { cursor: pointer; }
  tr:hover td { background: #1d2230; }
  tr.sel td { background: #233252 !important; }
  tr.marked td { opacity: .72; }
  tr.marked td:first-child { text-decoration: line-through; color: var(--video); }
  .trash-bar { position: fixed; left: 0; right: 0; bottom: 0; background: #1a1520;
    border-top: 1px solid #4a3040; padding: 10px 20px; z-index: 20;
    display: flex; flex-direction: column; max-height: 45vh; }
  .trash-bar h3 { font-size: 13px; color: var(--muted); margin-bottom: 8px; flex: 0 0 auto; }
  #trashList { overflow-y: auto; min-height: 0; }
  #trashList.collapsed .trash-item:nth-child(n+5) { display: none; }
  #trashToggle { align-self: flex-start; margin-top: 6px; background: none; border: none;
    color: var(--accent); cursor: pointer; font-size: 12.5px; padding: 2px 0; }
  #trashToggle:hover { text-decoration: underline; }
  .trash-item { display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
    padding: 6px 0; border-bottom: 1px solid #2a2230; font-size: 13px; }
  .trash-item .nm { flex: 1; min-width: 140px; word-break: break-all; }
  .trash-item .meta { color: var(--muted); font-size: 12px; }
  .opsmsg { font-size: 12.5px; margin-top: 8px; min-height: 18px; }
  .opsmsg.ok { color: var(--image); }
  .opsmsg.err { color: var(--video); }
  .renlbl { margin-top: 4px; }
  .mark-row { display: flex; align-items: center; gap: 8px; margin: 10px 0; font-size: 13px; }
  .mark-row input { width: auto; }
  .btn-danger { border-color: #7a3040 !important; color: #ff9cab !important; }
  .btn-danger:hover { border-color: var(--video) !important; color: var(--video) !important; }
  body.has-trash { padding-bottom: 120px; }
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
  .classify-btns { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  #classifymsg { font-size: 12.5px; margin-bottom: 12px; min-height: 18px; }
  #classifymsg.ok { color: var(--image); }
  #classifymsg.err { color: var(--video); }
</style>
</head>
<body>
<div class="wrap">
  <div class="topnav"><a href="/">&larr; Home</a> &middot; <a href="/duplicates">Duplicates</a></div>
  <h1>__TITLE__ <span>Index</span></h1>
  <div class="sub" id="sub">Loading&hellip;</div>
  <div class="cards" id="cards"></div>
  <div class="controls">
    <div class="controls-search">
      <input type="search" id="q" placeholder="Search filename or path&hellip;" autofocus>
    </div>
    <div class="controls-filters">
      <select id="kind">
        <option value="">All types</option>
      </select>
      <select id="source">
        <option value="">All sources</option>
        <option value="local">Local</option>
        <option value="onedrive">OneDrive</option>
        <option value="gdrive">Google Drive</option>
        <option value="qnap">QNAP NAS</option>
      </select>
      <select id="machine">
        <option value="">All machines</option>
      </select>
      <select id="year"><option value="">Any year</option></select>
      <select id="sort">
        <option value="modified">Newest first</option>
        <option value="size">Largest first</option>
        <option value="name">Name A&ndash;Z</option>
        <option value="path">Path A&ndash;Z</option>
      </select>
      <button id="resetBtn" type="button">Reset filters</button>
    </div>
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
      <div id="classifyBlock" hidden>
        <div class="renlbl">Classification</div>
        <div class="classify-btns">
          <button id="markPhoto" type="button">Mark as photo</button>
          <button id="markGraphic" type="button">Mark as computer image</button>
        </div>
        <div id="classifymsg"></div>
      </div>
      <button id="dupBtn" type="button" style="width:100%;margin-bottom:12px">Check duplicates</button>
      <div class="renlbl">File actions</div>
      <button id="revealBtn" type="button" style="width:100%;margin-bottom:8px">&#128193; Open folder in Explorer</button>
      <label class="mark-row"><input type="checkbox" id="markDelete"> Mark for deletion</label>
      <input id="moveDest" placeholder="Move to folder (full path)" spellcheck="false">
      <button id="moveBtn" type="button" style="width:100%;margin-bottom:8px">Move file</button>
      <button id="deleteBtn" type="button" class="btn-danger" style="width:100%;margin-bottom:12px">Delete file</button>
      <div id="opsmsg" class="opsmsg"></div>
      <div class="renlbl">Rename &middot; suggestions</div>
      <div class="chips" id="chips"></div>
      <input id="newname" spellcheck="false">
      <button id="renameBtn">Rename file</button>
      <div id="renamemsg"></div>
    </aside>
  </div>
</div>
<div class="trash-bar" id="trashBar" hidden>
  <h3>Session trash &mdash; restore before closing the browser
    <button id="trashEmpty" type="button" style="margin-left:10px;font-size:11px;padding:2px 8px;border-radius:6px;cursor:pointer">Empty trash</button></h3>
  <div id="trashList"></div>
  <button id="trashToggle" type="button" hidden></button>
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

function dirOf(path) {
  const i = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\\\"));
  return i > 0 ? path.slice(0, i) : path;
}

async function loadStats() {
  const s = await IH.cachedFetch("/api/stats?domain=" + PAGE.domain);
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
  ["onedrive", "OneDrive"], ["gdrive", "Google Drive"], ["qnap", "QNAP NAS"],
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
  const f = await IH.cachedFetch("/api/facets?" + p);
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

function resetFilters() {
  $("q").value = "";
  $("kind").value = "";
  $("source").value = "local";
  $("machine").value = "";
  $("year").value = "";
  $("sort").value = "modified";
  page = 0;
  closePanel();
  updateFacets();
  search();
}

async function search() {
  const p = new URLSearchParams({
    domain: PAGE.domain, q: $("q").value, kind: $("kind").value,
    source: $("source").value, machine: $("machine").value,
    year: $("year").value, sort: $("sort").value, page,
  });
  const d = await IH.cachedFetch("/api/search?" + p);
  total = d.total;
  lastRows = d.rows;
  $("rows").innerHTML = d.rows.length ? d.rows.map((r, i) => `<tr data-i="${i}"
      class="${sel && sel.id === r.id ? "sel" : ""}${r.marked_delete ? " marked" : ""}">
      <td title="${esc(r.name)}">${r.marked_delete ? "&#9888; " : ""}${esc(r.name)}</td>
      <td><span class="badge ${r.category || r.kind}">${r.category || r.kind}</span></td>
      <td class="src">${esc(r.source)}</td>
      <td class="src" title="${esc(r.device_label || "-")}">${esc(r.device_label || "-")}</td>
      <td>${fmtSize(r.size)}</td>
      <td class="src">${r.modified ? esc(r.modified.slice(0,10)) : ""}</td>
      <td class="path" title="${esc(r.path)}">${esc(r.path)}</td>
    </tr>`).join("")
    : `<tr><td colspan="7" class="empty">No matches</td></tr>`;
  const pages = Math.max(1, Math.ceil(total / d.page_size));
  $("pageinfo").textContent =
    `${total.toLocaleString()} result(s) \u00b7 page ${page + 1} of ${pages}`;
  $("prev").disabled = page === 0;
  $("next").disabled = page >= pages - 1;
  saveMediaState();
}

function saveMediaState() {
  IH.saveState("media-" + PAGE.domain, {
    q: $("q").value, kind: $("kind").value, source: $("source").value,
    machine: $("machine").value, year: $("year").value, sort: $("sort").value,
    page, scrollY: window.scrollY,
  });
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
  const showClassify = r.kind === "image";
  $("classifyBlock").hidden = !showClassify;
  $("classifymsg").textContent = "";
  $("classifymsg").className = "";
  if (showClassify) {
    $("markPhoto").disabled = r.category === "photo";
    $("markGraphic").disabled = r.category === "graphic";
  }
  $("markDelete").checked = !!r.marked_delete;
  $("moveDest").value = dirOf(r.path);
  $("opsmsg").textContent = "";
  $("opsmsg").className = "opsmsg";
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
async function reclassify(category) {
  if (!sel || sel.kind !== "image") return;
  $("classifymsg").textContent = "Saving\u2026";
  $("classifymsg").className = "";
  try {
    const res = await (await fetch("/api/reclassify", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({id: sel.id, category}),
    })).json();
    if (res.ok) {
      sel.category = res.category;
      $("classifymsg").textContent = "Updated \u2713";
      $("classifymsg").className = "ok";
      $("markPhoto").disabled = res.category === "photo";
      $("markGraphic").disabled = res.category === "graphic";
      $("fmeta").innerHTML =
        `<b>${esc(res.category)}</b> \u00b7 ${fmtSize(sel.size)} \u00b7 ` +
        `${sel.modified ? esc(sel.modified.slice(0,10)) : "no date"} \u00b7 ${esc(sel.source)} \u00b7 ${esc(sel.device_label || "-")}<br>` +
        `${esc(sel.path)}`;
      IH.bustCache();
      search();
      updateFacets();
    } else {
      $("classifymsg").textContent = res.error || "Update failed";
      $("classifymsg").className = "err";
    }
  } catch (err) {
    $("classifymsg").textContent = "Update failed: " + err;
    $("classifymsg").className = "err";
  }
}
$("markPhoto").onclick = () => reclassify("photo");
$("markGraphic").onclick = () => reclassify("graphic");
$("dupBtn").onclick = () => {
  if (sel) location.href = "/duplicates?file_id=" + encodeURIComponent(sel.id);
};
async function loadTrash() {
  const d = await (await fetch("/api/trash")).json();
  const bar = $("trashBar");
  const list = $("trashList");
  if (!d.items.length) {
    bar.hidden = true;
    document.body.classList.remove("has-trash");
    document.body.style.paddingBottom = "";
    return;
  }
  bar.hidden = false;
  document.body.classList.add("has-trash");
  list.innerHTML = d.items.map(it => `
    <div class="trash-item">
      <span class="nm">${esc(it.name)}</span>
      <span class="meta">${esc(it.source)} \u00b7 ${esc(it.original_path)}</span>
      <button type="button" data-restore="${esc(it.entry_id)}">Restore</button>
    </div>`).join("");
  applyTrashCollapse(d.items.length);
}
let trashExpanded = false;
function applyTrashCollapse(count) {
  const bar = $("trashBar"), list = $("trashList"), toggle = $("trashToggle");
  if (count > 4) {
    toggle.hidden = false;
    list.classList.toggle("collapsed", !trashExpanded);
    toggle.textContent = trashExpanded ? "Show less" : `Show all ${count}`;
  } else {
    toggle.hidden = true;
    list.classList.remove("collapsed");
  }
  document.body.style.paddingBottom = bar.offsetHeight + "px";
}
$("trashToggle").onclick = () => {
  trashExpanded = !trashExpanded;
  applyTrashCollapse(document.querySelectorAll("#trashList .trash-item").length);
};
$("trashList").addEventListener("click", async e => {
  const id = e.target.dataset.restore;
  if (!id) return;
  const res = await (await fetch("/api/restore", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({entry_id: id}),
  })).json();
  if (res.ok) { IH.bustCache(); loadTrash(); search(); updateFacets(); loadStats(); }
  else alert(res.error || "Restore failed");
});
$("trashEmpty").onclick = async () => {
  if (!confirm("Permanently delete all files in the session trash? This cannot be undone.")) return;
  const btn = $("trashEmpty"); btn.disabled = true;
  const res = await (await fetch("/api/trash/empty", {method: "POST"})).json();
  btn.disabled = false;
  if (res.errors && res.errors.length) alert("Some items could not be removed: " + res.errors.join("; "));
  IH.bustCache(); loadTrash();
};
$("markDelete").addEventListener("change", async () => {
  if (!sel) return;
  const res = await (await fetch("/api/mark-delete", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: sel.id, marked: $("markDelete").checked}),
  })).json();
  if (res.ok) {
    sel.marked_delete = res.marked_delete;
    IH.bustCache();
    search();
  } else {
    $("markDelete").checked = ! $("markDelete").checked;
    $("opsmsg").textContent = res.error || "Could not update mark";
    $("opsmsg").className = "opsmsg err";
  }
});
$("revealBtn").onclick = async () => {
  if (!sel) return;
  $("opsmsg").textContent = "Opening folder\u2026";
  $("opsmsg").className = "opsmsg";
  const res = await (await fetch("/api/reveal", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: sel.id}),
  })).json();
  if (res.ok) {
    if (res.url) { window.open(res.url, "_blank"); $("opsmsg").textContent = "Opened in Google Drive \u2713"; }
    else { $("opsmsg").textContent = "Opened in Explorer \u2713"; }
    $("opsmsg").className = "opsmsg ok";
  } else {
    $("opsmsg").textContent = res.error || "Open folder failed";
    $("opsmsg").className = "opsmsg err";
  }
};
$("moveBtn").onclick = async () => {
  if (!sel) return;
  $("opsmsg").textContent = "Moving\u2026";
  $("opsmsg").className = "opsmsg";
  const res = await (await fetch("/api/move", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: sel.id, dest_dir: $("moveDest").value.trim()}),
  })).json();
  if (res.ok) {
    sel.path = res.path; sel.name = res.name; sel.ext = res.ext;
    $("fname").textContent = res.name;
    $("opsmsg").textContent = "Moved \u2713";
    $("opsmsg").className = "opsmsg ok";
    IH.bustCache();
    search();
  } else {
    $("opsmsg").textContent = res.error || "Move failed";
    $("opsmsg").className = "opsmsg err";
  }
};
$("deleteBtn").onclick = async () => {
  if (!sel) return;
  if (!confirm(`Delete "${sel.name}"?\n\nYou can restore it from Session trash until you close the browser.`)) return;
  $("opsmsg").textContent = "Queued for deletion\u2026";
  $("opsmsg").className = "opsmsg";
  const res = await (await fetch("/api/delete", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: sel.id}),
  })).json();
  if (res.ok) {
    closePanel();
    IH.onDeleteComplete(() => { loadTrash(); search(); updateFacets(); loadStats(); });
    IH.pollDeletes();
  } else {
    $("opsmsg").textContent = res.error || "Delete failed";
    $("opsmsg").className = "opsmsg err";
  }
};
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
      IH.bustCache();
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
$("resetBtn").onclick = resetFilters;
$("prev").onclick = () => { page--; search(); };
$("next").onclick = () => { page++; search(); };

setOptions($("kind"), KIND_OPTS.map(([v, l]) => ({value: v, label: l, count: null})));
setOptions($("source"), SOURCE_OPTS.map(([v, l]) => ({value: v, label: l, count: null})));
setOptions($("machine"), [{value: "", label: "All machines", count: null}]);
// Restore the filters/scroll from the last visit so switching pages and coming
// back doesn't reset everything.
const _st = IH.loadState("media-" + PAGE.domain) || {};
if (_st.q) $("q").value = _st.q;
if (_st.kind) $("kind").value = _st.kind;
if (_st.source) $("source").value = _st.source;
if (_st.machine) $("machine").value = _st.machine;
if (_st.year) $("year").value = _st.year;
if (_st.sort) $("sort").value = _st.sort;
if (typeof _st.page === "number") page = _st.page;
// Deep-link support: /media?q=...&source=... (used by the duplicate checker's
// "Library" jump) pre-fills the filters so you land on the file. It overrides
// any restored state and resets pagination.
const _initP = new URLSearchParams(location.search);
if (_initP.get("q")) { $("q").value = _initP.get("q"); page = 0; }
if (_initP.get("source")) { $("source").value = _initP.get("source"); page = 0; }
loadStats();
updateFacets();
search().then(() => { if (_st.scrollY) window.scrollTo(0, _st.scrollY); });
loadTrash();
window.addEventListener("beforeunload", saveMediaState);
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
  .topnav { margin-bottom: 18px; }
  .topnav a { color: var(--muted); text-decoration: none; font-size: 13px; margin-right: 8px; }
  .topnav a:hover { color: var(--accent); }
  .scan-panel { background: var(--panel); border: 1px solid #262b38; border-radius: 14px;
    padding: 22px 26px; margin-top: 28px; }
  .scan-panel h2 { font-size: 17px; margin-bottom: 12px; }
  .scan-row { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; align-items: center; }
  .scan-row select, .scan-row input { background: var(--panel2); color: var(--text);
    border: 1px solid #2c3344; border-radius: 8px; padding: 9px 12px; font-size: 14px; }
  .scan-row input { flex: 1; min-width: 220px; }
  .scan-row button { background: var(--panel2); color: var(--text); border: 1px solid #2c3344;
    border-radius: 8px; padding: 9px 16px; cursor: pointer; font-size: 13.5px; }
  .scan-row button.primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
  .scan-row button:hover:not(:disabled) { border-color: var(--accent); }
  .scan-row button:disabled { opacity: .45; cursor: default; }
  #scanStatus { color: var(--muted); font-size: 13px; line-height: 1.7; min-height: 40px; }
  #scanStatus.running { color: var(--text); }
  label.chk { color: var(--muted); font-size: 13px; display: flex; align-items: center; gap: 6px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="topnav"><a href="/">&larr; Home</a> &middot; <a href="/media-org">Media Org</a> &middot; <a href="/duplicates">Duplicates</a></div>
  <h1>File <span>Index</span> Hub</h1>
  <div class="sub">Your local, OneDrive, Google Drive, and QNAP NAS files, indexed and searchable.</div>
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
    <a class="bigcard" href="/duplicates">
      <div class="icon">&#128257;</div>
      <h2>Duplicates</h2>
      <div class="desc">Find matches by name, metadata, or content hash &mdash; with the reclaimable-space report</div>
      <div class="stats" id="dup-stats">Loading&hellip;</div>
      <span class="open">Open &rarr;</span>
    </a>
    <a class="bigcard" href="/media-org">
      <div class="icon">&#128194;</div>
      <h2>Media Org</h2>
      <div class="desc">Sort a drive's files into media-org/ buckets &mdash; undoable</div>
      <div class="stats">Audio &middot; Video &middot; Images &middot; Documents</div>
      <span class="open">Open &rarr;</span>
    </a>
  </div>
  <div class="scan-panel">
    <h2>Rescan / Reindex</h2>
    <div class="scan-row">
      <select id="scanScope">
        <option value="all">All drives</option>
        <option value="local">Local only</option>
        <option value="onedrive">OneDrive only</option>
        <option value="gdrive">Google Drive only</option>
        <option value="qnap">QNAP NAS only</option>
      </select>
      <input id="scanPath" type="text" placeholder="Optional folder path (limits scope)">
    </div>
    <div class="scan-row">
      <label class="chk"><input type="checkbox" id="scanRescan" checked> Reindex files</label>
      <label class="chk"><input type="checkbox" id="scanHash" checked> Compute missing hashes</label>
    </div>
    <div class="scan-row">
      <button class="primary" id="scanStart">Start scan</button>
      <button id="scanCancel" disabled>Cancel</button>
      <button id="pruneMissing" type="button" title="Remove index entries for local / OneDrive files no longer on disk">Prune missing files</button>
    </div>
    <div id="scanStatus">Idle</div>
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
  IH.cachedFetch("/api/stats?domain=" + domain).then(s => {
    const cats = Object.entries(s.categories)
      .map(([c, n]) => `${n.toLocaleString()} ${c}`).join(" \u00b7 ");
    document.getElementById(domain + "-stats").innerHTML =
      `<b>${s.total.toLocaleString()}</b> files \u00b7 ${fmtSize(s.bytes)}<br>` +
      `<span style="font-size:12.5px">${cats || "&nbsp;"}</span>`;
  });
}
Promise.all([
  IH.cachedFetch("/api/duplicates/summary"),
  IH.cachedFetch("/api/duplicates/report"),
]).then(([s, d]) => {
  document.getElementById("dup-stats").innerHTML =
    `<b>${s.groups.toLocaleString()}</b> duplicate groups \u00b7 <b>${fmtSize(d.reclaim)}</b> reclaimable<br>` +
    `<span style="font-size:12.5px">${s.hashed.toLocaleString()} hashed / ${s.total.toLocaleString()} indexed` +
    (s.possible ? ` \u00b7 ${s.possible.toLocaleString()} large files for manual review` : "") +
    `</span>`;
}).catch(() => {
  document.getElementById("dup-stats").textContent = "Open to scan for duplicates";
});
let scanPoll;
async function refreshScanStatus() {
  const s = await (await fetch("/api/scan/status")).json();
  const el = document.getElementById("scanStatus");
  const startBtn = document.getElementById("scanStart");
  const cancelBtn = document.getElementById("scanCancel");
  startBtn.disabled = s.running;
  cancelBtn.disabled = !s.running;
  el.className = s.running ? "running" : "";
  if (s.running) {
    const prog = s.total ? ` (${s.files.toLocaleString()} / ${s.total.toLocaleString()})` : (s.files ? ` (${s.files.toLocaleString()} files)` : "");
    el.textContent = `${s.phase}${s.source ? " \u00b7 " + s.source : ""}: ${s.message}${prog}`;
  } else if (s.error) {
    el.textContent = "Error: " + s.error;
  } else if (s.phase === "done") {
    el.textContent = "Last run complete. " + (s.message || "");
  } else if (s.phase === "cancelled") {
    el.textContent = "Last run cancelled safely.";
  } else {
    el.textContent = "Idle";
  }
  if (!s.running && scanPoll) { clearInterval(scanPoll); scanPoll = null; IH.bustCache(); }
}
document.getElementById("scanStart").onclick = async () => {
  const scope = document.getElementById("scanScope").value;
  const body = {
    sources: scope === "all" ? null : [scope],
    path_prefix: document.getElementById("scanPath").value.trim(),
    rescan: document.getElementById("scanRescan").checked,
    hash_missing: document.getElementById("scanHash").checked,
  };
  const res = await (await fetch("/api/scan/start", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  })).json();
  if (!res.ok) { document.getElementById("scanStatus").textContent = res.error || "Could not start"; return; }
  if (!scanPoll) scanPoll = setInterval(refreshScanStatus, 1500);
  refreshScanStatus();
};
document.getElementById("scanCancel").onclick = async () => {
  await fetch("/api/scan/cancel", {method: "POST"});
  refreshScanStatus();
};
document.getElementById("pruneMissing").onclick = async () => {
  if (!confirm("Remove index entries for local / OneDrive files that no longer exist on disk?\n\nRemote files (QNAP, Google Drive) are not checked.")) return;
  const btn = document.getElementById("pruneMissing");
  const status = document.getElementById("scanStatus");
  btn.disabled = true;
  status.className = "running";
  status.textContent = "Checking files on disk…";
  try {
    const res = await (await fetch("/api/prune-missing", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
    })).json();
    if (res.ok) {
      IH.bustCache();
      status.textContent = `Pruned ${res.pruned.toLocaleString()} missing file(s) of ${res.checked.toLocaleString()} checked.`;
    } else {
      status.textContent = res.error || "Prune failed";
    }
  } catch (e) {
    status.textContent = "Prune failed: " + e;
  }
  status.className = "";
  btn.disabled = false;
};
refreshScanStatus();
</script>
</body>
</html>
"""

DUPS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duplicate Checker</title>
<style>
  :root { --bg:#0f1115; --panel:#181b22; --panel2:#1f2330; --text:#e6e9f0; --muted:#8b93a7; --accent:#5b8cff; --good:#46c08a; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--text); font:14px/1.5 "Segoe UI", system-ui, sans-serif; }
  .wrap { max-width:1200px; margin:0 auto; padding:24px 20px 60px; }
  h1 { font-size:22px; font-weight:600; }
  h1 span { color:var(--accent); }
  .sub { color:var(--muted); margin:4px 0 18px; font-size:13px; }
  .topnav { margin-bottom:10px; }
  .topnav a { color:var(--muted); text-decoration:none; font-size:13px; margin-right:8px; }
  .topnav a:hover { color:var(--accent); }
  .tabs { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }
  .tabs button { background:var(--panel2); color:var(--text); border:1px solid #2c3344; border-radius:8px;
    padding:8px 14px; cursor:pointer; font-size:13px; }
  .tabs button.active { border-color:var(--accent); color:var(--accent); }
  .group { background:var(--panel); border:1px solid #262b38; border-radius:10px; margin-bottom:14px; overflow:hidden; }
  .group-h { padding:10px 14px; background:var(--panel2); font-size:13px; color:var(--muted); word-break:break-all; }
  .group-h b { color:var(--text); }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:8px 12px; border-top:1px solid #232836; font-size:13px; }
  th { color:var(--muted); font-size:11px; text-transform:uppercase; }
  td.path { color:var(--muted); font-family:Consolas,monospace; font-size:12px; word-break:break-all; white-space:normal; }
  .empty { padding:40px; text-align:center; color:var(--muted); }
  .more-row { color:var(--muted); font-size:12px; font-style:italic; }
  .pager { display:flex; gap:10px; align-items:center; margin-top:14px; color:var(--muted); }
  button { background:var(--panel2); color:var(--text); border:1px solid #2c3344; border-radius:8px;
    padding:7px 16px; cursor:pointer; font-size:13.5px; }
  button:hover:not(:disabled) { border-color:var(--accent); }
  button:disabled { opacity:.4; cursor:default; }
  .anchor { background:#233252; border:1px solid #3a5080; border-radius:8px; padding:10px 14px; margin-bottom:14px; font-size:13px; }
  .flag { background:#4a3520; color:#f0b35e; border-radius:6px; padding:1px 7px; font-size:11px; white-space:nowrap; }
  .filters { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:16px; }
  .filters input[type=text], .filters select { background:var(--panel2); color:var(--text);
    border:1px solid #2c3344; border-radius:8px; padding:7px 10px; font-size:13px; }
  .filters input[type=text] { min-width:220px; flex:1 1 220px; }
  .filters label { color:var(--muted); font-size:13px; display:flex; align-items:center; gap:6px; }
  .reveal-btn { background:var(--panel2); color:var(--text); border:1px solid #2c3344; border-radius:7px;
    padding:4px 10px; font-size:12px; cursor:pointer; white-space:nowrap; }
  .reveal-btn:hover { border-color:var(--accent); color:var(--accent); }
  .reveal-btn:disabled { opacity:.5; cursor:default; }
  .del-btn { background:#3a2330; color:#f0859e; border:1px solid #5a3344; border-radius:7px;
    padding:4px 10px; font-size:12px; cursor:pointer; white-space:nowrap; }
  .del-btn:hover { border-color:#e0506e; color:#ff7090; }
  .del-btn:disabled { opacity:.5; cursor:default; }
  .lib-link { color:var(--accent); text-decoration:none; font-size:12px; white-space:nowrap; padding:4px 4px; }
  .lib-link:hover { text-decoration:underline; }
  .acts { display:flex; gap:6px; justify-content:flex-end; align-items:center; flex-wrap:wrap; }
  .trash-bar { position:fixed; bottom:0; left:0; right:0; background:#1a1d26; border-top:1px solid #2c3344;
    padding:10px 18px; max-height:45vh; box-shadow:0 -4px 14px rgba(0,0,0,.4);
    display:flex; flex-direction:column; }
  .trash-bar h3 { font-size:13px; color:#46c08a; margin-bottom:6px; font-weight:600; flex:0 0 auto; }
  #trashList { overflow-y:auto; min-height:0; }
  #trashList.collapsed .trash-item:nth-child(n+5) { display:none; }
  #trashToggle { align-self:flex-start; margin-top:6px; background:none; border:none;
    color:var(--accent); cursor:pointer; font-size:12.5px; padding:2px 0; }
  #trashToggle:hover { text-decoration:underline; }
  .trash-item { display:flex; gap:10px; align-items:center; font-size:12.5px; padding:3px 0; }
  .trash-item .nm { color:var(--text); }
  .trash-item .meta { color:var(--muted); font-family:Consolas,monospace; font-size:11px; word-break:break-all; flex:1; }
  body.has-trash .wrap { padding-bottom:36vh; }
  .batch-toggle.active { border-color:var(--accent); color:var(--accent); }
  .batchbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:14px;
    background:var(--panel); border:1px solid #2c3344; border-radius:10px; padding:10px 12px; }
  .batchbar select { background:var(--panel2); color:var(--text); border:1px solid #2c3344; border-radius:8px; padding:7px 10px; font-size:13px; }
  .selinfo { color:var(--muted); font-size:13px; margin-left:auto; }
  .selinfo b { color:var(--text); }
  td.cb, th.cb { width:34px; text-align:center; }
  td.cb input { width:16px; height:16px; cursor:pointer; }
  /* Top-level view switcher (Checker vs. Overview) */
  .viewtabs { display:flex; gap:4px; margin-bottom:16px; border-bottom:1px solid #262b38; }
  .viewtabs button { background:none; border:none; border-bottom:2px solid transparent; color:var(--muted);
    padding:9px 16px; cursor:pointer; font-size:14px; border-radius:0; }
  .viewtabs button:hover:not(:disabled) { color:var(--text); border-color:transparent; }
  .viewtabs button.active { color:var(--accent); border-bottom-color:var(--accent); }
  /* Overview tab (merged duplicate report), scoped so it can't clash with the checker */
  #view-overview h2 { font-size:15px; font-weight:600; margin:26px 0 10px; color:var(--text); }
  #view-overview h2.collapsible { cursor:pointer; user-select:none; }
  #view-overview h2.collapsible::before { content:"\\25be "; color:var(--muted); font-size:12px; }
  #view-overview h2.collapsible.collapsed::before { content:"\\25b8 "; }
  #view-overview .controls { display:flex; gap:8px; align-items:center; margin-bottom:8px; }
  #view-overview select { background:var(--panel2); color:var(--text); border:1px solid #2c3344; border-radius:8px; padding:7px 10px; font-size:13px; }
  #view-overview .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:6px; }
  #view-overview .card { background:var(--panel); border:1px solid #262b38; border-radius:12px; padding:14px 16px; }
  #view-overview .card .num { font-size:24px; font-weight:700; }
  #view-overview .card .num.hl { color:var(--good); }
  #view-overview .card .lbl { color:var(--muted); font-size:12px; margin-top:2px; }
  #view-overview table { width:100%; border-collapse:collapse; background:var(--panel); border:1px solid #262b38; border-radius:10px; overflow:hidden; }
  #view-overview th, #view-overview td { text-align:left; padding:9px 12px; border-top:1px solid #232836; font-size:13px; }
  #view-overview th { color:var(--muted); font-size:11px; text-transform:uppercase; border-top:none; background:var(--panel2); }
  #view-overview td.num, #view-overview th.num { text-align:right; font-variant-numeric:tabular-nums; }
  #view-overview tr.clickable:hover { background:#1c2433; cursor:pointer; }
  #view-overview .bar { height:7px; background:var(--panel2); border-radius:4px; overflow:hidden; margin-top:4px; }
  #view-overview .bar > i { display:block; height:100%; background:var(--accent); }
  #view-overview .src { display:inline-block; font-size:11px; color:var(--muted); }
  #view-overview a.glink { color:var(--accent); text-decoration:none; cursor:pointer; }
  #view-overview a.glink:hover { text-decoration:underline; }
</style>
</head>
<body>
<div class="wrap">
  <div class="topnav"><a href="/">&larr; Home</a> &middot; <a href="/media">Media</a> &middot; <a href="/documents">Documents</a></div>
  <h1>Dupli<span>cates</span></h1>
  <div class="sub" id="sub">Find and remove duplicate files across your drives.</div>
  <div class="viewtabs">
    <button data-view="checker" class="active">Checker</button>
    <button data-view="overview">Overview</button>
  </div>
  <div id="view-checker">
  <div class="tabs">
    <button data-mode="name" class="active">Filename</button>
    <button data-mode="meta">Metadata</button>
    <button data-mode="hash">Content hash</button>
  </div>
  <div class="filters">
    <input type="text" id="fq" placeholder="Search name or path…" spellcheck="false">
    <select id="fsource">
      <option value="">All sources</option>
      <option value="local">Local</option>
      <option value="onedrive">OneDrive</option>
      <option value="gdrive">Google Drive</option>
      <option value="qnap">QNAP NAS</option>
    </select>
    <select id="fsize">
      <option value="0">Any size</option>
      <option value="10000000">&ge; 10 MB</option>
      <option value="100000000">&ge; 100 MB</option>
      <option value="500000000">&ge; 500 MB</option>
      <option value="1000000000">&ge; 1 GB</option>
    </select>
    <label><input type="checkbox" id="fpossible"> Large files for review only</label>
    <button id="batchToggle" class="batch-toggle" type="button">&#9745; Batch select</button>
    <button id="freset" type="button">Reset</button>
  </div>
  <div class="batchbar" id="batchbar" hidden>
    <select id="keepRule" title="Which copy to keep in each group">
      <option value="shallow">Keep shallowest path</option>
      <option value="deep">Keep deepest path</option>
      <option value="alpha">Keep first path (A&rarr;Z)</option>
      <option value="newest">Keep newest</option>
      <option value="oldest">Keep oldest</option>
    </select>
    <button id="autoSel" type="button">Auto-select duplicates</button>
    <button id="clearSel" type="button">Clear</button>
    <span class="selinfo" id="selInfo"><b>0</b> selected</span>
    <button id="delSel" class="del-btn" type="button" disabled>Delete selected</button>
  </div>
  <div id="anchor" hidden></div>
  <div id="groups"></div>
  <div class="pager">
    <button id="prev">&larr; Prev</button>
    <span id="pageinfo"></span>
    <button id="next">Next &rarr;</button>
  </div>
  </div><!-- /view-checker -->
  <div id="view-overview" hidden>
    <div class="sub" id="ovsub">Exact content-hash duplicates &mdash; every removable copy is byte-identical to a kept original.</div>
    <div class="controls">
      <label class="src">Scope:</label>
      <select id="scope">
        <option value="">All sources</option>
        <option value="local">Local</option>
        <option value="onedrive">OneDrive</option>
        <option value="gdrive">Google Drive</option>
        <option value="qnap">QNAP NAS</option>
      </select>
    </div>
    <div class="cards" id="cards"></div>
    <h2 class="collapsible collapsed" data-target="buckets">Reclaimable space by file size</h2>
    <table id="buckets" style="display:none"><thead><tr>
      <th>Per-file size</th><th class="num">Groups</th><th class="num">Removable copies</th>
      <th class="num">Reclaimable</th><th style="width:30%">Share</th>
    </tr></thead><tbody></tbody></table>
    <h2 class="collapsible collapsed" data-target="persource">By source</h2>
    <table id="persource" style="display:none"><thead><tr>
      <th>Source</th><th class="num">Hashed files</th><th class="num">Dup groups</th>
      <th class="num">Removable copies</th><th class="num">Reclaimable</th>
    </tr></thead><tbody></tbody></table>
    <h2>Biggest duplicate groups</h2>
    <table id="top"><thead><tr>
      <th>File</th><th class="num">Copies</th><th class="num">Each</th>
      <th class="num">Reclaimable</th><th></th>
    </tr></thead><tbody></tbody></table>
  </div>
</div>
<div class="trash-bar" id="trashBar" hidden>
  <h3>Session trash &mdash; restore before closing the browser
    <button id="trashEmpty" type="button" style="margin-left:10px;font-size:11px;padding:2px 8px;border-radius:6px;cursor:pointer">Empty trash</button></h3>
  <div id="trashList"></div>
  <button id="trashToggle" type="button" hidden></button>
</div>
<script>
const $ = id => document.getElementById(id);
const esc = s => (s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const params = new URLSearchParams(location.search);
let mode = "name", page = 0, fileId = params.get("file_id") || "";
let batch = true, lastGroups = [], curView = "checker", fromOverview = false;
function fmtSize(n) {
  if (n == null || n < 0) return "";
  const u = ["B","KB","MB","GB","TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toLocaleString(undefined, {maximumFractionDigits: 1}) + " " + u[i];
}
async function load() {
  const p = new URLSearchParams({mode, page, limit: 25});
  if (fileId) p.set("file_id", fileId);
  if (!fileId) {
    if ($("fq").value.trim()) p.set("q", $("fq").value.trim());
    if ($("fsource").value) p.set("source", $("fsource").value);
    if ($("fsize").value !== "0") p.set("min_size", $("fsize").value);
    if ($("fpossible").checked) p.set("possible", "1");
  }
  const d = await IH.cachedFetch("/api/duplicates?" + p);
  if (d.anchor) {
    $("anchor").hidden = false;
    $("anchor").innerHTML = `Showing duplicates for <b>${esc(d.anchor.name)}</b> (${esc(d.anchor.source)}) \u00b7 ` +
      `<a href="/duplicates?mode=${mode}" style="color:var(--accent)">Show all groups</a>`;
  } else {
    $("anchor").hidden = true;
  }
  lastGroups = d.groups;
  // Cap rendered rows per group: a few hash groups can hold thousands of
  // identical files, and rendering them all at once freezes the page.
  const ROW_CAP = 50;
  $("groups").innerHTML = d.groups.length ? d.groups.map(g => {
    const shown = g.files.slice(0, ROW_CAP);
    const hidden = (g.count || g.files.length) - shown.length;
    const cols = batch ? 7 : 6;
    return `
    <div class="group">
      <div class="group-h"><b>${g.count} files</b> \u00b7 ${esc(g.label)}</div>
      <table><thead><tr>${batch ? '<th class="cb"></th>' : ""}<th>Name</th><th>Source</th><th>Size</th><th>Modified</th><th>Path</th><th></th></tr></thead>
      <tbody>${shown.map(f => `<tr data-row="${f.id}">
        ${batch ? `<td class="cb"><input type="checkbox" class="rowcb" data-id="${f.id}" data-group="${esc(String(g.key))}" data-size="${f.size || 0}"></td>` : ""}
        <td>${esc(f.name)}${f.possible_dupe ? ' <span class="flag">&ge;1 GB &middot; not hashed</span>' : ""}</td><td>${esc(f.source)}</td><td>${fmtSize(f.size)}</td>
        <td>${f.modified ? esc(f.modified.slice(0,10)) : ""}</td>
        <td class="path">${esc(f.path)}</td>
        <td class="acts">
          <a class="lib-link" href="/${f.kind === "document" ? "documents" : "media"}?q=${encodeURIComponent(f.name)}&source=${encodeURIComponent(f.source)}" target="_blank" title="Open in the library with rename / move / delete">Library &#8599;</a>
          <button class="reveal-btn" data-reveal="${f.id}" title="Open containing folder in Explorer">&#128193;</button>
          <button class="del-btn" data-del="${f.id}" data-name="${esc(f.name)}" title="Delete to session trash (restorable)">Delete</button>
        </td></tr>`).join("")}${hidden > 0 ? `<tr><td colspan="${cols}" class="more-row">+ ${hidden.toLocaleString()} more copies not shown \u2014 narrow with filters or open one in Library</td></tr>` : ""}</tbody></table>
    </div>`;
  }).join("") : `<div class="empty">No duplicate groups found for this filter.</div>`;
  const pages = Math.max(1, Math.ceil(d.total_groups / d.page_size));
  $("pageinfo").textContent = `${d.total_groups.toLocaleString()} group(s) \u00b7 page ${page + 1} of ${pages}`;
  $("prev").disabled = page === 0;
  $("next").disabled = page >= pages - 1;
  updateSelInfo();
  if (batch) autoSelect();  // pre-select duplicates (keeping one per group)
  saveDupState();
  // If we drilled in from the Overview and just cleared the last copies of
  // this group, hop back to the Overview and refresh its numbers.
  if (fromOverview && fileId && d.groups.length === 0) {
    fromOverview = false;
    fileId = "";
    IH.bustCache();
    showView("overview");
    loadOverview();
  }
}
function saveDupState() {
  IH.saveState("dups", {
    mode, page, batch, fileId, view: curView, ovscope: $("scope").value,
    fq: $("fq").value, fsource: $("fsource").value,
    fsize: $("fsize").value, fpossible: $("fpossible").checked,
    scrollY: window.scrollY,
  });
}
// ---- batch selection ----
function pathDepth(p) { return (p.match(/[\\\\/]/g) || []).length; }
function keeperId(files, rule) {
  const a = files.slice();
  if (rule === "shallow") a.sort((x, y) => pathDepth(x.path) - pathDepth(y.path) || x.path.length - y.path.length || x.path.localeCompare(y.path));
  else if (rule === "deep") a.sort((x, y) => pathDepth(y.path) - pathDepth(x.path) || y.path.length - x.path.length || x.path.localeCompare(y.path));
  else if (rule === "alpha") a.sort((x, y) => x.path.localeCompare(y.path));
  else if (rule === "newest") a.sort((x, y) => (y.modified || "").localeCompare(x.modified || ""));
  else if (rule === "oldest") a.sort((x, y) => (x.modified || "").localeCompare(y.modified || ""));
  return a[0].id;
}
function autoSelect() {
  const rule = $("keepRule").value;
  const keepers = new Set();
  lastGroups.forEach(g => keepers.add(String(g.key) + "|" + keeperId(g.files, rule)));
  document.querySelectorAll(".rowcb").forEach(cb => {
    cb.checked = !keepers.has(cb.dataset.group + "|" + cb.dataset.id);
  });
  updateSelInfo();
}
function updateSelInfo() {
  if (!batch) return;
  const checked = [...document.querySelectorAll(".rowcb:checked")];
  const bytes = checked.reduce((s, cb) => s + (+cb.dataset.size || 0), 0);
  $("selInfo").innerHTML = `<b>${checked.length}</b> selected \u00b7 ${fmtSize(bytes)}`;
  $("delSel").disabled = checked.length === 0;
}
async function deleteSelected() {
  const checked = [...document.querySelectorAll(".rowcb:checked")];
  if (!checked.length) return;
  // Guard: never delete every visible copy in a group.
  const tally = {};
  document.querySelectorAll(".rowcb").forEach(cb => { (tally[cb.dataset.group] ||= {total: 0, sel: 0}).total++; });
  checked.forEach(cb => { tally[cb.dataset.group].sel++; });
  const bad = Object.values(tally).filter(g => g.sel >= g.total).length;
  if (bad) { alert(`${bad} group(s) would have every shown copy deleted.\nLeave at least one copy per group.`); return; }
  const ids = checked.map(cb => +cb.dataset.id);
  if (!confirm(`Delete ${ids.length} file(s) to session trash?\nThey can be restored until you close the browser.`)) return;
  $("delSel").disabled = true; $("delSel").textContent = "Deleting\u2026";
  const res = await (await fetch("/api/delete-batch", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ids}),
  })).json();
  $("delSel").textContent = "Delete selected";
  if (res.ok) {
    $("delSel").disabled = false;
    IH.onDeleteComplete((s) => {
      if (s && s.failed) alert(`${s.failed} delete(s) failed:\\n` + (s.errors || []).slice(0, 6).join("\\n"));
      loadTrash(); load();
    });
    IH.pollDeletes();
  } else { alert(res.error || "Batch delete failed"); $("delSel").disabled = false; }
}
$("batchToggle").onclick = () => {
  batch = !batch;
  $("batchToggle").classList.toggle("active", batch);
  $("batchbar").hidden = !batch;
  load();
};
$("autoSel").onclick = autoSelect;
$("clearSel").onclick = () => {
  document.querySelectorAll(".rowcb").forEach(cb => cb.checked = false);
  updateSelInfo();
};
$("delSel").onclick = deleteSelected;
$("groups").addEventListener("change", e => {
  if (e.target.classList.contains("rowcb")) updateSelInfo();
});
$("groups").addEventListener("click", async e => {
  const rev = e.target.closest("[data-reveal]");
  if (rev) {
    const orig = rev.textContent;
    rev.textContent = "\u2026"; rev.disabled = true;
    const res = await (await fetch("/api/reveal", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({id: +rev.dataset.reveal}),
    })).json();
    if (res.ok && res.url) window.open(res.url, "_blank");
    else if (!res.ok) alert(res.error || "Open folder failed");
    rev.textContent = orig; rev.disabled = false;
    return;
  }
  const del = e.target.closest("[data-del]");
  if (del) {
    if (!confirm(`Delete "${del.dataset.name}"?\n\nIt moves to session trash and can be restored until you close the browser.`)) return;
    del.disabled = true; del.textContent = "Deleting\u2026";
    const res = await (await fetch("/api/delete", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({id: +del.dataset.del}),
    })).json();
    if (res.ok) { IH.onDeleteComplete(() => { loadTrash(); load(); }); IH.pollDeletes(); }
    else { alert(res.error || "Delete failed"); del.disabled = false; del.textContent = "Delete"; }
  }
});
async function loadTrash() {
  const d = await (await fetch("/api/trash")).json();
  const bar = $("trashBar"), list = $("trashList"), wrap = document.querySelector(".wrap");
  if (!d.items.length) { bar.hidden = true; document.body.classList.remove("has-trash"); wrap.style.paddingBottom = ""; return; }
  bar.hidden = false; document.body.classList.add("has-trash");
  list.innerHTML = d.items.map(it => `
    <div class="trash-item">
      <span class="nm">${esc(it.name)}</span>
      <span class="meta">${esc(it.source)} \u00b7 ${esc(it.original_path)}</span>
      <button class="reveal-btn" data-restore="${esc(it.entry_id)}">Restore</button>
    </div>`).join("");
  applyTrashCollapse(d.items.length);
}
let trashExpanded = false;
function applyTrashCollapse(count) {
  const bar = $("trashBar"), list = $("trashList"), toggle = $("trashToggle"), wrap = document.querySelector(".wrap");
  if (count > 4) {
    toggle.hidden = false;
    list.classList.toggle("collapsed", !trashExpanded);
    toggle.textContent = trashExpanded ? "Show less" : `Show all ${count}`;
  } else {
    toggle.hidden = true;
    list.classList.remove("collapsed");
  }
  wrap.style.paddingBottom = bar.offsetHeight + "px";
}
$("trashToggle").onclick = () => {
  trashExpanded = !trashExpanded;
  applyTrashCollapse(document.querySelectorAll("#trashList .trash-item").length);
};
$("trashList").addEventListener("click", async e => {
  const b = e.target.closest("[data-restore]");
  if (!b) return;
  b.disabled = true;
  const res = await (await fetch("/api/restore", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({entry_id: b.dataset.restore}),
  })).json();
  if (res.ok) { IH.bustCache(); loadTrash(); load(); }
  else { alert(res.error || "Restore failed"); b.disabled = false; }
});
$("trashEmpty").onclick = async () => {
  if (!confirm("Permanently delete all files in the session trash? This cannot be undone.")) return;
  const btn = $("trashEmpty"); btn.disabled = true;
  const res = await (await fetch("/api/trash/empty", {method: "POST"})).json();
  btn.disabled = false;
  if (res.errors && res.errors.length) alert("Some items could not be removed: " + res.errors.join("; "));
  IH.bustCache(); loadTrash();
};
let ft;
$("fq").addEventListener("input", () => { clearTimeout(ft); ft = setTimeout(() => { page = 0; load(); }, 300); });
["fsource", "fsize", "fpossible"].forEach(id =>
  $(id).addEventListener("change", () => { page = 0; load(); }));
$("freset").onclick = () => {
  $("fq").value = ""; $("fsource").value = ""; $("fsize").value = "0"; $("fpossible").checked = false;
  page = 0; load();
};
document.querySelectorAll(".tabs button").forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll(".tabs button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    fromOverview = false;
    mode = btn.dataset.mode;
    page = 0;
    load();
  };
});
$("prev").onclick = () => { page--; load(); };
$("next").onclick = () => { page++; load(); };
// ---- Overview tab (the merged duplicate report) ----
const N = n => (n || 0).toLocaleString();
let _ovLoaded = false;
async function revealPath(id, btn) {
  const orig = btn.textContent; btn.textContent = "Opening…"; btn.disabled = true;
  const res = await (await fetch("/api/reveal", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id})})).json();
  if (res.ok && res.url) window.open(res.url, "_blank");
  else if (!res.ok) alert(res.error || "Open folder failed");
  btn.textContent = orig; btn.disabled = false;
}
async function loadOverview() {
  const scope = $("scope").value;
  const d = await IH.cachedFetch("/api/duplicates/report?source=" + encodeURIComponent(scope));
  $("cards").innerHTML = `
    <div class="card"><div class="num">${N(d.files)}</div><div class="lbl">files in scope</div></div>
    <div class="card"><div class="num">${N(d.groups)}</div><div class="lbl">duplicate groups</div></div>
    <div class="card"><div class="num">${N(d.redundant)}</div><div class="lbl">removable copies</div></div>
    <div class="card"><div class="num hl">${fmtSize(d.reclaim)}</div><div class="lbl">reclaimable space</div></div>
    <div class="card"><div class="num">${fmtSize(d.ibytes)}</div><div class="lbl">indexed in scope</div></div>`;
  const maxB = Math.max(1, ...d.buckets.map(b => b.bytes));
  $("buckets").querySelector("tbody").innerHTML = d.buckets.map(b => `
    <tr><td>${esc(b.label)}</td><td class="num">${N(b.groups)}</td><td class="num">${N(b.copies)}</td>
    <td class="num">${fmtSize(b.bytes)}</td>
    <td><div class="bar"><i style="width:${(100*b.bytes/maxB).toFixed(1)}%"></i></div></td></tr>`).join("");
  const SRCLABEL = {local:"Local", onedrive:"OneDrive", gdrive:"Google Drive", qnap:"QNAP NAS"};
  $("persource").querySelector("tbody").innerHTML = d.per_source.map(s => `
    <tr class="clickable" onclick="$('scope').value='${s.source}';loadOverview()">
      <td>${esc(SRCLABEL[s.source] || s.source)}</td><td class="num">${N(s.files)}</td>
      <td class="num">${N(s.groups)}</td><td class="num">${N(s.copies)}</td>
      <td class="num">${fmtSize(s.reclaim)}</td></tr>`).join("")
      || `<tr><td colspan="5" style="color:var(--muted)">No duplicates.</td></tr>`;
  $("top").querySelector("tbody").innerHTML = d.top.map(t => `
    <tr>
      <td><a class="glink" onclick="openGroup(${t.id})" title="Show this group in the checker">${esc(t.name)}</a>
        <div class="path">${esc(t.path)} <span class="src">· ${esc(t.source)}</span></div></td>
      <td class="num">${t.count}×</td><td class="num">${fmtSize(t.each)}</td>
      <td class="num">${fmtSize(t.waste)}</td>
      <td><button class="reveal-btn" onclick="revealPath(${t.id}, this)" title="Open containing folder in Explorer">&#128193; Open</button></td>
    </tr>`).join("") || `<tr><td colspan="5" style="color:var(--muted)">No duplicate groups.</td></tr>`;
  $("ovsub").innerHTML = `Exact content-hash duplicates in <b>${d.scope === "all" ? "all sources" : esc(SRCLABEL[d.scope] || d.scope)}</b> ` +
    `— ${N(d.hashed)} of ${N(d.files)} files hashed. Every removable copy is byte-identical to a kept original.`;
  _ovLoaded = true;
}
// Jump from an Overview group straight into the Checker, anchored on that file.
function openGroup(id) {
  fromOverview = true;  // so we can return to Overview once this group is cleared
  fileId = String(id); mode = "hash"; page = 0;
  document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("active", b.dataset.mode === "hash"));
  showView("checker");
  load();
}
function showView(v) {
  curView = v;
  $("view-checker").hidden = (v !== "checker");
  $("view-overview").hidden = (v !== "overview");
  document.querySelectorAll(".viewtabs button").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  if (v === "overview" && !_ovLoaded) loadOverview();
  saveDupState();
}
$("scope").onchange = () => { saveDupState(); loadOverview(); };
document.querySelectorAll(".viewtabs button").forEach(b => b.onclick = () => { fromOverview = false; showView(b.dataset.view); });
document.querySelectorAll("#view-overview h2.collapsible").forEach(h => {
  h.onclick = () => {
    const t = $(h.dataset.target);
    const hide = t.style.display !== "none";
    t.style.display = hide ? "none" : "";
    h.classList.toggle("collapsed", hide);
  };
});
if (params.get("mode")) mode = params.get("mode");
// Restore the last view (mode, filters, batch toggle, scroll) unless the URL
// carries an explicit anchor/mode deep-link, which always wins.
const _hasUrl = params.get("file_id") || params.get("mode");
const _ds = IH.loadState("dups") || {};
if (!_hasUrl) {
  if (_ds.mode) mode = _ds.mode;
  if (typeof _ds.page === "number") page = _ds.page;
  if (typeof _ds.batch === "boolean") batch = _ds.batch;
  if (_ds.fileId) fileId = _ds.fileId;
  if (_ds.fq != null) $("fq").value = _ds.fq;
  if (_ds.fsource != null) $("fsource").value = _ds.fsource;
  if (_ds.fsize != null) $("fsize").value = _ds.fsize;
  if (_ds.fpossible) $("fpossible").checked = true;
}
if (_ds.ovscope != null) $("scope").value = _ds.ovscope;
if (params.get("source")) $("scope").value = params.get("source");
if (batch) { $("batchToggle").classList.add("active"); $("batchbar").hidden = false; }
document.querySelectorAll(".tabs button").forEach(b =>
  b.classList.toggle("active", b.dataset.mode === mode));
load().then(() => { if (curView === "checker" && !_hasUrl && _ds.scrollY) window.scrollTo(0, _ds.scrollY); });
loadTrash();
// Initial tab: ?view=overview, a file_id anchor forces Checker, else the saved view.
let _initView = "checker";
if (params.get("view") === "overview") _initView = "overview";
else if (!_hasUrl && _ds.view) _initView = _ds.view;
showView(_initView);
window.addEventListener("beforeunload", saveDupState);
</script>
</body>
</html>
"""

MEDIAORG_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Media Org</title>
<style>
  :root { --bg:#15171c; --card:#1e2128; --text:#e8eaed; --muted:#9aa0a6; --accent:#7aa2f7; --line:#2a2e37; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,Segoe UI,Arial; }
  .wrap { max-width:860px; margin:0 auto; padding:24px 20px 80px; }
  .topnav { color:var(--muted); font-size:13px; margin-bottom:14px; }
  .topnav a { color:var(--accent); text-decoration:none; }
  h1 { font-size:26px; margin:0 0 4px; } h1 span { color:var(--accent); }
  .sub { color:var(--muted); margin-bottom:20px; }
  .panel { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px; margin-bottom:18px; }
  label.src { display:block; padding:8px 10px; border:1px solid var(--line); border-radius:8px; margin-bottom:8px; cursor:pointer; }
  label.src:hover { border-color:var(--accent); }
  button { background:#272b34; color:var(--text); border:1px solid var(--line); border-radius:8px; padding:8px 16px; font-size:14px; cursor:pointer; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#10131a; font-weight:600; }
  button:disabled { opacity:.45; cursor:default; }
  #preview, #progress { color:var(--muted); margin-top:12px; min-height:20px; font-size:14px; }
  #progress.active { color:var(--text); }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; }
  .err { color:#ff8a8a; }
</style></head>
<body>
<div class="wrap">
  <div class="topnav"><a href="/">&larr; Home</a> &middot; <a href="/media">Media</a> &middot; <a href="/documents">Documents</a> &middot; <a href="/duplicates">Duplicates</a></div>
  <h1>Media <span>Org</span></h1>
  <div class="sub">Pick a drive, then sort its files into <code>media-org/</code> buckets (Audio, Video, Photos, Graphics, Documents), with sub-folders by original location and year. Every move is remembered so you can undo a whole run.</div>

  <div class="panel">
    <h3 style="margin-top:0">1. Choose a drive</h3>
    <label class="src"><input type="radio" name="src" value="local"> Local</label>
    <label class="src"><input type="radio" name="src" value="onedrive"> OneDrive</label>
    <label class="src"><input type="radio" name="src" value="gdrive"> Google Drive</label>
    <label style="display:block;margin:10px 2px 0;font-size:13px;color:var(--muted);cursor:pointer">
      <input type="checkbox" id="skipDocs" checked> Skip documents (media only)</label>
    <div style="margin-top:10px">
      <button id="previewBtn">Preview</button>
      <button id="organizeBtn" class="primary" disabled>Organize</button>
    </div>
    <div id="preview"></div>
    <div id="progress"></div>
    <button id="cancelBtn" style="display:none;margin-top:8px">Cancel</button>
  </div>

  <div class="panel">
    <h3 style="margin-top:0">Past runs</h3>
    <table id="batches"><thead><tr><th>When</th><th>Drive</th><th>Files</th><th></th></tr></thead><tbody></tbody></table>
  </div>
</div>
<script>
var lastPreviewSrc = null, poll = null;
function selectedSrc() {
  var el = document.querySelector('input[name=src]:checked');
  return el ? el.value : null;
}
function skipDocs() { return document.getElementById('skipDocs').checked; }
function invalidatePreview() {
  document.getElementById('organizeBtn').disabled = true;
  document.getElementById('preview').textContent = '';
  lastPreviewSrc = null;
}
document.getElementById('skipDocs').onchange = invalidatePreview;
function setProgress(s, active) {
  var el = document.getElementById('progress');
  el.textContent = s; el.className = active ? 'active' : '';
}
document.querySelectorAll('input[name=src]').forEach(function (r) {
  r.onchange = function () {
    document.getElementById('organizeBtn').disabled = true;
    document.getElementById('preview').textContent = '';
    lastPreviewSrc = null;
  };
});
document.getElementById('previewBtn').onclick = function () {
  var src = selectedSrc();
  if (!src) { document.getElementById('preview').textContent = 'Choose a drive first.'; return; }
  document.getElementById('preview').textContent = 'Previewing…';
  fetch('/api/organize/preview?source=' + src + '&skip_documents=' + (skipDocs() ? '1' : '0'))
    .then(function (r) { return r.json(); }).then(function (p) {
    if (!p.ok) { document.getElementById('preview').textContent = p.error || 'Preview failed'; return; }
    var b = p.buckets;
    var parts = 'Audio ' + b.Audio + ', Video ' + b.Video + ', Photos ' + b.Photos +
      ', Graphics ' + b.Graphics + (skipDocs() ? '' : ', Documents ' + b.Documents);
    document.getElementById('preview').textContent =
      p.total + ' files to sort — ' + parts + ' (' + p.skipped + ' already sorted, skipped)';
    lastPreviewSrc = src;
    document.getElementById('organizeBtn').disabled = (p.total === 0);
  });
};
document.getElementById('organizeBtn').onclick = function () {
  var src = selectedSrc();
  if (!src || src !== lastPreviewSrc) { setProgress('Preview this drive first.', false); return; }
  if (!confirm('Move ' + src + ' files into media-org/ buckets?')) return;
  document.getElementById('organizeBtn').disabled = true;
  fetch('/api/organize/start', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ source: src, skip_documents: skipDocs() }) }).then(function (r) { return r.json(); }).then(function (res) {
    if (!res.ok) { setProgress(res.error || 'Could not start', false); return; }
    startPolling();
  });
};
document.getElementById('cancelBtn').onclick = function () {
  this.disabled = true;
  fetch('/api/organize/cancel', { method:'POST' }).catch(function () {});
};
function startPolling() {
  if (poll) return;
  poll = setInterval(tick, 1000); tick();
}
function tick() {
  fetch('/api/organize/status').then(function (r) { return r.json(); }).then(function (s) {
    var done = (s.moved || 0) + (s.skipped || 0) + (s.failed || 0);
    var cancelBtn = document.getElementById('cancelBtn');
    if (s.running) {
      cancelBtn.style.display = 'inline-block';
      if (!s.cancelled) cancelBtn.disabled = false;
      var verb = s.mode === 'undo' ? 'Undoing' : 'Organizing';
      setProgress((s.cancelled ? 'Cancelling… ' : verb + ' ') + done + ' of ' + (s.total || 0) +
        (s.current ? ' — ' + s.current : '') + (s.failed ? '  (' + s.failed + ' failed)' : ''), true);
    } else {
      if (poll) { clearInterval(poll); poll = null; }
      cancelBtn.style.display = 'none';
      cancelBtn.disabled = false;
      if (done > 0 || s.finished_at) {
        setProgress('Done — ' + (s.moved || 0) + ' moved, ' + (s.skipped || 0) +
          ' skipped' + (s.failed ? ', ' + s.failed + ' failed' : '') + '.', false);
      }
      loadBatches();
    }
  });
}
function loadBatches() {
  fetch('/api/organize/batches').then(function (r) { return r.json(); }).then(function (res) {
    var tb = document.querySelector('#batches tbody'); tb.innerHTML = '';
    (res.batches || []).forEach(function (b) {
      var tr = document.createElement('tr');
      var fullyUndone = (b.undone >= b.total);
      tr.innerHTML = '<td>' + (b.started || '').replace('T', ' ') + '</td><td>' + b.source +
        '</td><td>' + (b.total - b.undone) + ' / ' + b.total + '</td><td></td>';
      var btn = document.createElement('button');
      btn.textContent = fullyUndone ? 'Undone' : 'Undo';
      btn.disabled = fullyUndone;
      btn.onclick = function () {
        if (!confirm('Move these files back to their original locations?')) return;
        btn.disabled = true;
        fetch('/api/organize/undo', { method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ batch_id: b.batch_id }) }).then(function (r) { return r.json(); }).then(function (res) {
          if (!res.ok) { setProgress(res.error || 'Undo failed', false); btn.disabled = false; return; }
          startPolling();
        });
      };
      tr.children[3].appendChild(btn);
      tb.appendChild(tr);
    });
  });
}
loadBatches();
tick();  // resume the progress line if a job is already running
</script>
</body></html>"""

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


def _ip_rank(ip: str) -> int:
    """Score an IPv4 as a LAN-URL candidate. Higher is better; <0 excludes it.

    Prefers ordinary home/office private ranges and rejects loopback,
    link-local, and CGNAT (100.64/10, which is what Tailscale hands out).
    """
    octets = ip.split(".")
    if len(octets) != 4 or not all(o.isdigit() for o in octets):
        return -1
    a, b = int(octets[0]), int(octets[1])
    if a == 127 or (a == 169 and b == 254):       # loopback / link-local
        return -1
    if a == 100 and 64 <= b <= 127:               # CGNAT (Tailscale, carrier NAT)
        return -1
    if a == 192 and b == 168:                     # 192.168.0.0/16
        return 3
    if a == 10:                                   # 10.0.0.0/8
        return 2
    if a == 172 and 16 <= b <= 31:                # 172.16.0.0/12 (incl. Hyper-V)
        return 1
    return 0


def lan_ip() -> str | None:
    """Best-guess primary LAN IPv4.

    Gathers every bound IPv4 plus the outbound-route interface, then prefers
    real private ranges (192.168 > 10 > 172.16/12) so we don't advertise a
    Tailscale/CGNAT address or a virtual adapter as the LAN URL.
    """
    candidates = set()
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidates.add(ai[4][0])
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no packets sent; just resolves the route
        candidates.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    ranked = sorted(((_ip_rank(ip), ip) for ip in candidates), reverse=True)
    for score, ip in ranked:
        if score >= 0:
            return ip
    return None


def footer_html() -> str:
    """Footer shown on every page; advertises the LAN URL when bound wide."""
    link = "color:#5b8cff;text-decoration:none"
    local = f"http://localhost:{BIND_PORT}"
    parts = [f'<a style="{link}" href="{local}">{local}</a>']
    if BIND_HOST not in ("127.0.0.1", "localhost"):
        ip = BIND_HOST if BIND_HOST != "0.0.0.0" else lan_ip()
        if ip:
            lan = f"http://{ip}:{BIND_PORT}"
            parts.append(f'LAN: <a style="{link}" href="{lan}">{lan}</a>')
    return (
        '<footer style="max-width:1500px;margin:32px auto 0;padding:16px 20px;'
        "border-top:1px solid #262b38;color:#8b93a7;font-size:12.5px;"
        'text-align:center">File <span style="color:#5b8cff">Index</span> Hub'
        " &middot; " + " &middot; ".join(parts) + "</footer>"
    )


# Shared client helper injected into every page's <head> (before page scripts
# run). Provides sessionStorage-backed API caching and UI-state persistence so
# switching pages doesn't lose filters/scroll or redundantly re-fetch data.
COMMON_JS = """
window.IH = (function () {
  var CACHE = "ihcache:", STATE = "ihstate:";
  function cachedFetch(url, ttl) {
    ttl = ttl || 60000;
    var key = CACHE + url;
    try {
      var raw = sessionStorage.getItem(key);
      if (raw) {
        var hit = JSON.parse(raw);
        if (Date.now() - hit.t < ttl) return Promise.resolve(hit.v);
      }
    } catch (e) {}
    return fetch(url).then(function (r) { return r.json(); }).then(function (v) {
      try {
        var rec = JSON.stringify({ t: Date.now(), v: v });
        // Don't cache huge payloads (e.g. big hash-duplicate result sets) —
        // they blow the sessionStorage quota and slow every read.
        if (rec.length < 1000000) sessionStorage.setItem(key, rec);
      } catch (e) {}
      return v;
    });
  }
  function bustCache() {
    try {
      var del = [];
      for (var i = 0; i < sessionStorage.length; i++) {
        var k = sessionStorage.key(i);
        if (k && k.indexOf(CACHE) === 0) del.push(k);
      }
      del.forEach(function (k) { sessionStorage.removeItem(k); });
    } catch (e) {}
  }
  function saveState(key, obj) {
    try { sessionStorage.setItem(STATE + key, JSON.stringify(obj)); } catch (e) {}
  }
  function loadState(key) {
    try { var r = sessionStorage.getItem(STATE + key); return r ? JSON.parse(r) : null; }
    catch (e) { return null; }
  }
  // ---- background delete progress ----
  var _delTimer = null, _onDone = null;
  function onDeleteComplete(fn) { _onDone = fn; }
  function pollDeletes() {
    if (_delTimer) return;
    var cancelBtn = document.getElementById("ih-delcancel");
    if (cancelBtn && !cancelBtn._wired) {
      cancelBtn._wired = true;
      cancelBtn.onclick = function () {
        cancelBtn.disabled = true;
        fetch("/api/delete/cancel", { method: "POST" }).catch(function () {});
      };
    }
    _delTimer = setInterval(_tickDeletes, 1000);
    _tickDeletes();
  }
  function _tickDeletes() {
    fetch("/api/delete/status").then(function (r) { return r.json(); }).then(function (s) {
      var bar = document.getElementById("ih-delbar");
      var msg = document.getElementById("ih-delmsg");
      var processed = (s.deleted || 0) + (s.pruned || 0) + (s.failed || 0);
      if (s.running) {
        if (bar) bar.hidden = false;
        if (msg) msg.textContent = (s.cancelled ? "Cancelling\\u2026 " : "Deleting ")
          + processed + " of " + (s.total || 0)
          + (s.current ? " \\u2014 " + s.current : "")
          + (s.failed ? "  (" + s.failed + " failed)" : "");
      } else {
        if (_delTimer) { clearInterval(_delTimer); _delTimer = null; }
        if (bar) bar.hidden = true;
        var cb2 = document.getElementById("ih-delcancel"); if (cb2) cb2.disabled = false;
        bustCache();
        var cb = _onDone; _onDone = null;
        if (cb) cb(s);
      }
    }).catch(function () {});
  }
  return { cachedFetch: cachedFetch, bustCache: bustCache,
           saveState: saveState, loadState: loadState,
           pollDeletes: pollDeletes, onDeleteComplete: onDeleteComplete };
})();
"""


_DELBAR = (
    '<div id="ih-delbar" hidden style="position:fixed;top:0;left:0;right:0;'
    "z-index:50;background:#3a2330;color:#ffb3c1;border-bottom:1px solid #5a3344;"
    'padding:8px 16px;font-size:13px;text-align:center;font-weight:600">'
    '<span id="ih-delmsg"></span>'
    '<button id="ih-delcancel" style="margin-left:12px;background:#5a3344;'
    "color:#ffd0d8;border:1px solid #7a4456;border-radius:6px;padding:2px 10px;"
    'font-size:12px;cursor:pointer;font-weight:600">Cancel</button>'
    "</div>"
)


def render_page(html: str) -> bytes:
    """Inject the shared IH helper into <head>, the delete-progress bar and the
    footer before </body>."""
    html = html.replace("</head>", f"<script>{COMMON_JS}</script>\n</head>", 1)
    html = html.replace("</body>", _DELBAR + footer_html() + "\n</body>", 1)
    return html.encode()


def render_app(page_key: str) -> bytes:
    cfg = PAGES[page_key]
    html = APP_HTML.replace("__TITLE__", cfg["title"])
    html = html.replace("__CONFIG__", json.dumps(cfg))
    return render_page(html)


def db():
    conn = mi.get_db()
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
    if exclude != "source" and source in ("local", "onedrive", "gdrive", "qnap"):
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
        f"SELECT id, source, device_id, device_label, path, name, ext, kind, size, modified, "
        f"category, marked_delete "
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


def _date_prefix(date_str: str) -> str | None:
    """Turn a date string into yyyymmdd for filename prefixes."""
    d = (date_str or "")[:10].replace(":", "-")
    if len(d) >= 10 and d[4] == "-" and d[7] == "-":
        return d.replace("-", "")
    return None


def _prefixed_filename(name: str, prefix: str) -> str:
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        return f"{prefix}-{re.sub(r'^\d{8}-', '', stem)}.{ext}"
    return f"{prefix}-{re.sub(r'^\d{8}-', '', name)}"


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
    prefix = _date_prefix(date)
    if not prefix:
        prefix = datetime.now().strftime("%Y%m%d")
    candidates.append(_prefixed_filename(row["name"], prefix))
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
    return {"suggestions": suggestions[:6]}


def api_reclassify(body):
    row = get_file_row(body.get("id"))
    if not row:
        raise ValueError("File not found in index")
    if row["kind"] != "image":
        raise ValueError("Only images can be reclassified as photo or computer image")
    category = body.get("category")
    if category not in ("photo", "graphic"):
        raise ValueError("Category must be photo or graphic")
    if row["category"] == category:
        raise ValueError("Classification is unchanged")
    conn = db()
    conn.execute("UPDATE files SET category = ? WHERE id = ?", (category, row["id"]))
    conn.commit()
    conn.close()
    return {"ok": True, "category": category}


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
    elif row["source"] in ("gdrive", "qnap"):
        old_rel = row["path"]
        new_rel = str(PurePosixPath(old_rel).with_name(new_name))
        proc = subprocess.run(
            [mi.find_rclone(), "moveto",
             mi.rclone_full_path(row["source"], old_rel),
             mi.rclone_full_path(row["source"], new_rel)],
            capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            label = "Google Drive" if row["source"] == "gdrive" else "QNAP"
            raise ValueError(f"{label} rename failed: {proc.stderr.strip()[:300]}")
        new_path = new_rel

    new_ext = new_name.rsplit(".", 1)[-1].lower() if "." in new_name else ""
    conn = db()
    conn.execute("UPDATE files SET path = ?, name = ?, ext = ? WHERE id = ?",
                 (new_path, new_name, new_ext, row["id"]))
    conn.commit()
    conn.close()
    return {"ok": True, "name": new_name, "path": new_path, "ext": new_ext}


# --- duplicates -----------------------------------------------------------------

DUP_MODES = {
    # "name COLLATE NOCASE" groups case-insensitively like LOWER(name) but can
    # use the idx_files_name_lower index, where LOWER(name) cannot.
    "name": "name COLLATE NOCASE",
    "meta": "meta_fingerprint",
    "hash": "content_hash",
}


def _file_brief(row) -> dict:
    return {
        "id": row["id"], "source": row["source"], "name": row["name"],
        "path": row["path"], "size": row["size"], "modified": row["modified"],
        "content_hash": row["content_hash"], "meta_fingerprint": row["meta_fingerprint"],
        "possible_dupe": row["possible_dupe"], "kind": row["kind"],
    }


def api_duplicates_summary():
    conn = db()
    mi.backfill_meta_fingerprints(conn)
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    hashed = conn.execute(
        "SELECT COUNT(*) FROM files WHERE content_hash IS NOT NULL AND content_hash != ''"
    ).fetchone()[0]
    groups = conn.execute(
        "SELECT COUNT(*) FROM ("
        "SELECT content_hash FROM files WHERE content_hash IS NOT NULL AND content_hash != '' "
        "GROUP BY content_hash HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    possible = conn.execute(
        "SELECT COUNT(*) FROM files WHERE possible_dupe = 1").fetchone()[0]
    conn.close()
    return {"total": total, "hashed": hashed, "groups": groups, "possible": possible}


REPORT_BUCKETS = [
    ("≥ 1 GB", 1_000_000_000, None),
    ("100 MB – 1 GB", 100_000_000, 1_000_000_000),
    ("10 – 100 MB", 10_000_000, 100_000_000),
    ("< 10 MB", 0, 10_000_000),
]


def api_duplicates_report(params):
    """Exact-content-hash duplicate breakdown, optionally scoped to one source.
    Reports group/redundant-copy counts, reclaimable bytes, size buckets, a
    per-source rollup, and the biggest groups by reclaimable space."""
    source = params.get("source", [""])[0]
    if source not in ("local", "onedrive", "gdrive", "qnap"):
        source = ""
    conn = db()
    hashed_clause = "content_hash IS NOT NULL AND content_hash != ''"
    scope_clause = hashed_clause + (" AND source = ?" if source else "")
    scope_args = [source] if source else []

    files, ibytes = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE "
        + (("source = ?") if source else "1=1"), scope_args).fetchone()
    hashed = conn.execute(
        f"SELECT COUNT(*) FROM files WHERE {scope_clause}", scope_args).fetchone()[0]

    # Duplicate groups within scope (cross-source when no source is selected).
    rows = conn.execute(
        f"SELECT content_hash h, COUNT(*) c, MAX(size) sz FROM files "
        f"WHERE {scope_clause} GROUP BY content_hash HAVING c > 1", scope_args).fetchall()

    groups = len(rows)
    redundant = sum(r["c"] - 1 for r in rows)
    reclaim = sum((r["c"] - 1) * (r["sz"] or 0) for r in rows)

    buckets = []
    for label, lo, hi in REPORT_BUCKETS:
        sel = [r for r in rows
               if (r["sz"] or 0) >= lo and (hi is None or (r["sz"] or 0) < hi)]
        buckets.append({
            "label": label,
            "groups": len(sel),
            "copies": sum(r["c"] - 1 for r in sel),
            "bytes": sum((r["c"] - 1) * (r["sz"] or 0) for r in sel),
        })

    top = sorted(rows, key=lambda r: (r["c"] - 1) * (r["sz"] or 0), reverse=True)[:25]
    top_out = []
    for r in top:
        sample = conn.execute(
            "SELECT id, name, path, source FROM files WHERE content_hash = ?"
            + (" AND source = ?" if source else "") + " ORDER BY source, path LIMIT 1",
            [r["h"]] + scope_args).fetchone()
        if not sample:
            continue
        top_out.append({
            "id": sample["id"], "name": sample["name"], "path": sample["path"],
            "source": sample["source"], "count": r["c"], "each": r["sz"] or 0,
            "waste": (r["c"] - 1) * (r["sz"] or 0),
        })

    # Per-source rollup: duplicate groups *within* each source.
    per_rows = conn.execute(
        f"SELECT source, content_hash, COUNT(*) c, MAX(size) sz FROM files "
        f"WHERE {hashed_clause} GROUP BY source, content_hash HAVING c > 1").fetchall()
    roll = {}
    for r in per_rows:
        d = roll.setdefault(r["source"], {"groups": 0, "copies": 0, "reclaim": 0})
        d["groups"] += 1
        d["copies"] += r["c"] - 1
        d["reclaim"] += (r["c"] - 1) * (r["sz"] or 0)
    counts = dict(conn.execute(
        f"SELECT source, COUNT(*) FROM files WHERE {hashed_clause} GROUP BY source"))
    per_source = []
    for src in ("local", "onedrive", "gdrive", "qnap"):
        if src not in counts and src not in roll:
            continue
        d = roll.get(src, {"groups": 0, "copies": 0, "reclaim": 0})
        per_source.append({"source": src, "files": counts.get(src, 0), **d})

    conn.close()
    return {
        "scope": source or "all", "files": files, "ibytes": ibytes, "hashed": hashed,
        "groups": groups, "redundant": redundant, "reclaim": reclaim,
        "buckets": buckets, "top": top_out, "per_source": per_source,
    }


def _dup_filters(params):
    """Build extra WHERE conditions for the duplicate-group queries from the
    filter params. Returns (sql_fragment, args); the fragment begins with
    ' AND ...' so it can be appended to an existing WHERE."""
    where, args = [], []
    q = params.get("q", [""])[0].strip()
    source = params.get("source", [""])[0]
    possible = params.get("possible", [""])[0]
    try:
        min_size = int(params.get("min_size", ["0"])[0])
    except ValueError:
        min_size = 0
    if q:
        where.append("(name LIKE ? OR path LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if source in ("local", "onedrive", "gdrive", "qnap"):
        where.append("source = ?")
        args.append(source)
    if min_size > 0:
        where.append("size >= ?")
        args.append(min_size)
    if possible == "1":
        where.append("possible_dupe = 1")
    frag = ("".join(f" AND {c}" for c in where))
    return frag, args


def api_duplicates(params):
    mode = params.get("mode", ["name"])[0]
    if mode not in DUP_MODES:
        mode = "name"
    key_expr = DUP_MODES[mode]
    try:
        page = max(0, int(params.get("page", ["0"])[0]))
    except ValueError:
        page = 0
    try:
        limit = min(100, max(1, int(params.get("limit", ["25"])[0])))
    except ValueError:
        limit = 25
    file_id = params.get("file_id", [""])[0].strip()

    conn = db()
    if mode == "meta":
        mi.backfill_meta_fingerprints(conn)
    anchor = None

    if file_id:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if not row:
            conn.close()
            return {"groups": [], "total_groups": 0, "page": 0, "page_size": limit}
        anchor = _file_brief(row)
        if mode == "name":
            key_val = row["name"].lower()
            key_filter = f"{key_expr} = ?"
            key_args = [key_val]
        elif mode == "meta":
            if not row["meta_fingerprint"]:
                conn.close()
                return {"groups": [], "total_groups": 0, "page": 0, "page_size": limit, "anchor": anchor}
            key_val = row["meta_fingerprint"]
            key_filter = f"{key_expr} = ?"
            key_args = [key_val]
        else:
            if not row["content_hash"]:
                conn.close()
                return {"groups": [], "total_groups": 0, "page": 0, "page_size": limit, "anchor": anchor}
            key_val = row["content_hash"]
            key_filter = f"{key_expr} = ?"
            key_args = [key_val]
        files = [dict(r) for r in conn.execute(
            f"SELECT * FROM files WHERE {key_filter} ORDER BY source, name", key_args)]
        conn.close()
        if len(files) < 2:
            return {"groups": [], "total_groups": 0, "page": 0, "page_size": limit, "anchor": anchor}
        label = key_val if mode != "name" else row["name"]
        return {
            "groups": [{
                "key": key_val, "label": label, "count": len(files),
                "files": [_file_brief(r) for r in files[:GROUP_FILE_CAP]],
            }],
            "total_groups": 1,
            "page": 0,
            "page_size": limit,
            "anchor": anchor,
        }

    null_guard = f"{key_expr} IS NOT NULL AND {key_expr} != ''"
    if mode == "name":
        null_guard = f"{key_expr} IS NOT NULL"

    frag, frag_args = _dup_filters(params)
    where_full = null_guard + frag

    total_groups = conn.execute(
        f"SELECT COUNT(*) FROM ("
        f"SELECT {key_expr} k FROM files WHERE {where_full} "
        f"GROUP BY k HAVING COUNT(*) > 1)", frag_args
    ).fetchone()[0]

    key_rows = conn.execute(
        f"SELECT k, c FROM ("
        f"SELECT {key_expr} k, COUNT(*) c FROM files WHERE {where_full} "
        f"GROUP BY k HAVING c > 1) ORDER BY c DESC, k LIMIT ? OFFSET ?",
        frag_args + [limit, page * limit]).fetchall()

    groups = []
    for key_val, count in key_rows:
        # Fetch only the capped slice, not every row in the group; the true
        # total comes from the grouped count above. Groups can be huge
        # (thousands of identical files), so this avoids a heavy fetchall and
        # the resulting oversized payload.
        rows = conn.execute(
            f"SELECT * FROM files WHERE {key_expr} = ?{frag} ORDER BY source, name LIMIT ?",
            [key_val] + frag_args + [GROUP_FILE_CAP]).fetchall()
        label = rows[0]["name"] if (mode == "name" and rows) else key_val
        groups.append({
            "key": key_val,
            "label": label,
            "count": count,
            "files": [_file_brief(r) for r in rows],
        })
    conn.close()
    return {
        "groups": groups,
        "total_groups": total_groups,
        "page": page,
        "page_size": limit,
        "anchor": anchor,
    }


def api_scan_start(body):
    if scan_jobs.job_manager.status()["running"]:
        return {"ok": False, "error": "A scan is already running"}
    sources = body.get("sources")
    if sources == "all" or sources is None:
        sources = None
    elif isinstance(sources, str):
        sources = [sources]
    ok = scan_jobs.job_manager.start(
        sources=sources,
        path_prefix=(body.get("path_prefix") or "").strip(),
        rescan=bool(body.get("rescan", True)),
        hash_missing=bool(body.get("hash_missing", True)),
    )
    if not ok:
        return {"ok": False, "error": "Could not start scan"}
    return {"ok": True}


def api_scan_cancel():
    return {"ok": scan_jobs.job_manager.cancel()}


def api_scan_status():
    return scan_jobs.job_manager.status()


def api_trash(session_id: str):
    items = [{
        "entry_id": it["entry_id"],
        "name": it["name"],
        "source": it["source"],
        "original_path": it["original_path"],
        "deleted_at": it["deleted_at"],
    } for it in file_ops.file_sessions.list_trash(session_id)]
    return {"items": items}


def api_trash_empty(session_id: str):
    return file_ops.file_sessions.empty_trash(session_id)


def api_mark_delete(body, session_id: str):
    conn = db()
    try:
        return file_ops.file_sessions.mark_for_deletion(
            conn, str(body.get("id")), bool(body.get("marked")))
    finally:
        conn.close()


def api_delete_file(body, session_id: str):
    """Queue a single delete on the background worker; returns immediately."""
    fid = body.get("id")
    if fid is None:
        return {"ok": False, "error": "No file id"}
    file_ops.delete_jobs.enqueue(session_id, [fid])
    return {"ok": True, "queued": 1}


def api_delete_batch(body, session_id: str):
    """Queue many deletes on the background worker; returns immediately. The UI
    polls /api/delete/status for progress."""
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return {"ok": False, "error": "No files selected"}
    if len(ids) > 5000:
        return {"ok": False, "error": "Too many files in one batch (max 5000)"}
    file_ops.delete_jobs.enqueue(session_id, ids)
    return {"ok": True, "queued": len(ids)}


def api_delete_status():
    return file_ops.delete_jobs.status()


def api_delete_cancel():
    return {"ok": file_ops.delete_jobs.cancel()}


def api_prune_missing():
    """Remove index rows for local/OneDrive files that no longer exist on disk.

    Only filesystem-backed sources are swept — checking remote sources (QNAP,
    Google Drive) would mean one rclone call per file. The rows point at files
    that are already gone, so they're hard-deleted (no trash)."""
    conn = db()
    try:
        rows = conn.execute(
            "SELECT id, path FROM files WHERE source IN ('local', 'onedrive')"
        ).fetchall()
        # Prune only files truly gone from disk — never OneDrive cloud-only
        # placeholders (present in the namespace but not downloaded locally).
        gone = [r["id"] for r in rows
                if not file_ops.exists_on_disk(r["path"])
                and not file_ops.is_cloud_placeholder(r["path"])]
        for i in range(0, len(gone), 500):
            chunk = gone[i:i + 500]
            conn.execute(
                f"DELETE FROM files WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        if gone:
            conn.commit()
        return {"ok": True, "pruned": len(gone), "checked": len(rows)}
    finally:
        conn.close()


def api_ingest(body, token_ok: bool):
    """Accept a remote machine's local-file inventory and upsert it into the
    shared index, tagged with that machine's identity. Used by `media_index.py
    push` running on another computer (e.g. a laptop)."""
    if not token_ok:
        return {"ok": False, "error": "Invalid or missing ingest token"}
    device_id = (body.get("device_id") or "").strip()
    device_label = (body.get("device_label") or "").strip()
    source = body.get("source")
    files = body.get("files")
    if source not in ("local", "onedrive"):
        return {"ok": False, "error": "source must be 'local' or 'onedrive'"}
    if not device_id:
        return {"ok": False, "error": "device_id is required"}
    if not isinstance(files, list):
        return {"ok": False, "error": "files must be a list"}
    conn = db()
    try:
        count = mi.ingest_rows(conn, source, device_id, device_label, files)
    finally:
        conn.close()
    return {"ok": True, "count": count, "device_id": device_id,
            "device_label": device_label}


def api_restore_file(body, session_id: str):
    conn = db()
    try:
        return file_ops.file_sessions.restore_file(
            conn, session_id, str(body.get("entry_id")))
    finally:
        conn.close()


def api_move_file(body, session_id: str):
    conn = db()
    try:
        return file_ops.file_sessions.move_file(
            conn, str(body.get("id")), (body.get("dest_dir") or "").strip())
    finally:
        conn.close()


def api_reveal(body):
    """Open the containing folder in Windows Explorer with the file selected."""
    row = get_file_row(body.get("id"))
    if not row:
        return {"ok": False, "error": "File not found in index"}
    if row["source"] == "gdrive":
        url = mi.gdrive_web_url(row["path"])
        if url:
            return {"ok": True, "url": url}
        return {"ok": False, "error": "Could not resolve the Google Drive link"}
    target = mi.reveal_path(row["source"], row["path"])
    if not target:
        label = "Google Drive" if row["source"] == "gdrive" else row["source"]
        return {"ok": False, "error": f"Open folder is not available for {label} files"}
    target = os.path.normpath(target)
    if not os.path.exists(target):
        return {"ok": False, "error": f"Not found on this machine: {target}"}
    # explorer.exe does its own command-line parsing: the path must be quoted
    # *inside* the /select, switch, because filenames often contain spaces or
    # parentheses. Passing a list would quote the whole "/select,<path>" token,
    # which Explorer ignores — it then opens the default folder instead of
    # selecting the file. Use a raw command string so the quotes land around
    # only the path. Explorer returns non-zero even on success, so fire/forget.
    subprocess.Popen(f'explorer /select,"{target}"')
    return {"ok": True, "path": target}


ORGANIZE_SOURCES = ("local", "onedrive", "gdrive")  # QNAP deferred


def api_organize_preview(params):
    source = (params.get("source", [""])[0] or "").strip()
    if source not in ORGANIZE_SOURCES:
        return {"ok": False, "error": "Choose a drive"}
    skip_documents = (params.get("skip_documents", ["1"])[0] != "0")
    conn = db()
    try:
        return file_ops.organize_plan(conn, source, skip_documents)
    finally:
        conn.close()


def api_organize_start(body):
    source = (body.get("source") or "").strip()
    if source not in ORGANIZE_SOURCES:
        return {"ok": False, "error": "Choose a drive"}
    skip_documents = bool(body.get("skip_documents", True))
    try:
        batch_id = file_ops.organize_jobs.enqueue_organize(source, skip_documents)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "batch_id": batch_id}


def api_organize_status():
    return file_ops.organize_jobs.status()


def api_organize_cancel():
    return {"ok": file_ops.organize_jobs.cancel()}


def api_organize_batches():
    conn = db()
    try:
        return {"ok": True, "batches": file_ops.list_batches(conn)}
    finally:
        conn.close()


def api_organize_undo(body):
    batch_id = (body.get("batch_id") or "").strip()
    if not batch_id:
        return {"ok": False, "error": "batch_id required"}
    try:
        file_ops.organize_jobs.enqueue_undo(batch_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    session_id = ""
    session_is_new = False

    def _ensure_session(self):
        self.session_id, self.session_is_new = file_ops.parse_session_id(
            self.headers.get("Cookie"))

    def _maybe_set_session_cookie(self):
        if self.session_is_new:
            self.send_header(
                "Set-Cookie",
                f"indexhub_session={self.session_id}; Path=/; HttpOnly; SameSite=Lax",
            )

    def do_GET(self):
        self._ensure_session()
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, render_page(LANDING_HTML), "text/html; charset=utf-8")
        elif url.path in ("/media", "/documents"):
            self._send(200, render_app(url.path[1:]), "text/html; charset=utf-8")
        elif url.path == "/duplicates":
            self._send(200, render_page(DUPS_HTML), "text/html; charset=utf-8")
        elif url.path == "/duplicates/report":
            # The report is now the Overview tab of /duplicates; keep old
            # bookmarks and deep-links working.
            self._redirect("/duplicates?view=overview")
        elif url.path == "/api/duplicates/report":
            self._json(api_duplicates_report(parse_qs(url.query)))
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
        elif url.path == "/api/duplicates":
            self._json(api_duplicates(parse_qs(url.query)))
        elif url.path == "/api/duplicates/summary":
            self._json(api_duplicates_summary())
        elif url.path == "/api/scan/status":
            self._json(api_scan_status())
        elif url.path == "/api/delete/status":
            self._json(api_delete_status())
        elif url.path == "/api/trash":
            self._json(api_trash(self.session_id))
        elif url.path == "/media-org":
            self._send(200, render_page(MEDIAORG_HTML), "text/html; charset=utf-8")
        elif url.path == "/api/organize/preview":
            self._json(api_organize_preview(parse_qs(url.query)))
        elif url.path == "/api/organize/status":
            self._json(api_organize_status())
        elif url.path == "/api/organize/batches":
            self._json(api_organize_batches())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        self._ensure_session()
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "Invalid JSON body"})
            return

        if url.path == "/api/scan/start":
            self._json(api_scan_start(body))
            return
        if url.path == "/api/scan/cancel":
            self._json(api_scan_cancel())
            return
        if url.path == "/api/delete/cancel":
            self._json(api_delete_cancel())
            return
        if url.path == "/api/organize/start":
            self._json(api_organize_start(body))
            return
        if url.path == "/api/organize/cancel":
            self._json(api_organize_cancel())
            return
        if url.path == "/api/organize/undo":
            self._json(api_organize_undo(body))
            return
        if url.path == "/api/mark-delete":
            try:
                self._json(api_mark_delete(body, self.session_id))
            except ValueError as exc:
                self._json({"ok": False, "error": str(exc)})
            return
        if url.path == "/api/delete":
            try:
                self._json(api_delete_file(body, self.session_id))
            except ValueError as exc:
                self._json({"ok": False, "error": str(exc)})
            return
        if url.path == "/api/delete-batch":
            try:
                self._json(api_delete_batch(body, self.session_id))
            except ValueError as exc:
                self._json({"ok": False, "error": str(exc)})
            return
        if url.path == "/api/ingest":
            expected = os.environ.get("INDEXHUB_TOKEN")
            token_ok = (not expected) or (self.headers.get("X-IndexHub-Token") == expected)
            try:
                self._json(api_ingest(body, token_ok))
            except Exception as exc:
                self._json({"ok": False, "error": f"Ingest failed: {exc}"})
            return
        if url.path == "/api/restore":
            try:
                self._json(api_restore_file(body, self.session_id))
            except ValueError as exc:
                self._json({"ok": False, "error": str(exc)})
            return
        if url.path == "/api/trash/empty":
            self._json(api_trash_empty(self.session_id))
            return
        if url.path == "/api/move":
            try:
                self._json(api_move_file(body, self.session_id))
            except ValueError as exc:
                self._json({"ok": False, "error": str(exc)})
            return
        if url.path == "/api/reveal":
            try:
                self._json(api_reveal(body))
            except Exception as exc:
                self._json({"ok": False, "error": f"Open folder failed: {exc}"})
            return
        if url.path == "/api/prune-missing":
            try:
                self._json(api_prune_missing())
            except Exception as exc:
                self._json({"ok": False, "error": f"Prune failed: {exc}"})
            return
        if url.path not in ("/api/rename", "/api/reclassify"):
            self._send(404, b"not found", "text/plain")
            return
        try:
            if url.path == "/api/rename":
                self._json(api_rename(body))
            else:
                self._json(api_reclassify(body))
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
            elif row["source"] in ("gdrive", "qnap"):
                self._stream_remote(row, ctype)
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

    def _stream_remote(self, row, ctype):
        proc = subprocess.Popen(
            [mi.find_rclone(), "cat", mi.rclone_full_path(row["source"], row["path"])],
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

    def _redirect(self, location, code=301):
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj):
        self._send(200, json.dumps(obj).encode(), "application/json", set_cookie=True)

    def _send(self, code, body, ctype, set_cookie=False):
        self.send_response(code)
        if set_cookie:
            self._maybe_set_session_cookie()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Pages and API responses are generated fresh each load; never let a
        # browser serve a stale copy (e.g. an old page without a new button).
        self.send_header("Cache-Control", "no-store")
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
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; use 0.0.0.0 to let other machines "
                        "on your network reach this server")
    args = parser.parse_args()
    global BIND_HOST, BIND_PORT
    BIND_HOST, BIND_PORT = args.host, args.port
    server = Server((args.host, args.port), Handler)
    print(f"Media Index UI running at http://localhost:{args.port}  (Ctrl+C to stop)")
    if args.host == "0.0.0.0":
        ip = lan_ip() or socket.gethostbyname(socket.gethostname())
        print(f"  Reachable from other machines on your LAN at http://{ip}:{args.port}")
        if not os.environ.get("INDEXHUB_TOKEN"):
            print("  NOTE: no INDEXHUB_TOKEN set — any machine on your LAN can push "
                  "to /api/ingest. Set INDEXHUB_TOKEN to require a shared secret.")
    server.serve_forever()


if __name__ == "__main__":
    main()
