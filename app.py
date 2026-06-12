"""
app.py — Multi-Site Lead Automation Web UI
──────────────────────────────────────────
Run up to 4 offers simultaneously. Each offer has its own engine thread,
log queue, stats, screenshot directory, and stop event.

Open: http://localhost:5000
"""
from __future__ import annotations

import os
import queue
import socket
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests as _requests
import yaml
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, make_response, render_template_string, request

import urllib3.util.connection as _urllib3_cn
_urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

load_dotenv()

app = Flask(__name__)

# ── Offers registry ───────────────────────────────────────────────────────────
OFFERS: dict[str, dict] = {
    "50k_loans": {
        "name":          "50k Loans",
        "url":           "https://50kloans.com/",
        "filler":        "core.form_filler",
        "color":         "#38bdf8",
        "sheet_url_env": "SHEET_URL_50K",
        "sheet_ws_env":  "SHEET_WS_50K",
    },
    "low_credit": {
        "name":          "Low Credit Finance",
        "url":           "https://lowcreditfinance.com",
        "filler":        "core.form_filler_lowcredit",
        "color":         "#4ade80",
        "sheet_url_env": "SHEET_URL_LOW_CREDIT",
        "sheet_ws_env":  "SHEET_WS_LOW_CREDIT",
    },
    "borrow_money": {
        "name":          "BorrowMoney",
        "url":           "https://borrowmoney.us",
        "filler":        "core.form_filler_borrowmoney",
        "color":         "#fb923c",
        "sheet_url_env": "SHEET_URL_BORROW_MONEY",
        "sheet_ws_env":  "SHEET_WS_BORROW_MONEY",
    },
    "super_personal": {
        "name":          "Super Personal Finder",
        "url":           "https://superpersonalfinder.com",
        "filler":        "core.form_filler_superpersonal",
        "color":         "#c084fc",
        "sheet_url_env": "SHEET_URL_SUPER_PERSONAL",
        "sheet_ws_env":  "SHEET_WS_SUPER_PERSONAL",
    },
}

# ── Per-engine state ──────────────────────────────────────────────────────────

def _mk_engine() -> dict:
    return {
        "running":    False,
        "stop_event": threading.Event(),
        "thread":     None,
        "log_queue":  queue.Queue(maxsize=1000),
        "stats":      {"fresh": 0, "duplicate": 0, "total": 0, "processed": 0},
    }

_engines: dict[str, dict] = {oid: _mk_engine() for oid in OFFERS}

# Thread-local: each engine thread stores its offer_id so the global
# structlog processor routes log lines to the right queue.
_tl = threading.local()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ss_path(offer_id: str) -> Path:
    return Path(f"screenshots/{offer_id}/live_view.png")


def _log(offer_id: str, msg: str) -> None:
    q = _engines[offer_id]["log_queue"]
    ts = time.strftime("%H:%M:%S")
    for part in str(msg).splitlines() or [""]:
        entry = f"[{ts}] {part}"
        if q.full():
            try:
                q.get_nowait()
            except queue.Empty:
                pass
        q.put_nowait(entry)


def _get_outbound_ip(proxy_url: str | None) -> str:
    try:
        kwargs: dict = {"timeout": 10}
        if proxy_url:
            kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
        return _requests.get("https://api.ipify.org?format=text", **kwargs).text.strip()
    except Exception:
        if proxy_url:
            return urlparse(proxy_url).hostname or "unknown"
        return "direct"


# ── Structlog global config ───────────────────────────────────────────────────

def _setup_structlog() -> None:
    import structlog

    def _routing_renderer(lgr, method, ev):
        offer_id = getattr(_tl, "offer_id", None)
        if offer_id:
            lvl = ev.get("level", method).upper()[:4]
            event = str(ev.get("event", ""))
            _skip = {"level", "event", "_record", "timestamp", "_logger"}

            def _fv(v):
                s = str(v).replace('"', "'")
                return f'"{s}"' if " " in s else s

            parts = [
                f"{k}={_fv(v)}"
                for k, v in ev.items()
                if k not in _skip and not k.startswith("_")
            ][:5]
            _log(offer_id, f"{lvl}  {event}" + ("  " + "  ".join(parts) if parts else ""))
        raise structlog.DropEvent()

    structlog.configure(
        processors=[_routing_renderer],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        logger_factory=structlog.PrintLoggerFactory(file=open(os.devnull, "w")),
        cache_logger_on_first_use=False,
    )


# ── Engine runner ─────────────────────────────────────────────────────────────

def _run_engine(offer_id: str, target_url: str = "") -> None:  # noqa: ARG001 — url kept for API compat
    eng = _engines[offer_id]
    stop_event = eng["stop_event"]
    eng["running"] = True
    eng["stats"] = {"fresh": 0, "duplicate": 0, "total": 0, "processed": 0}
    _tl.offer_id = offer_id

    try:
        import importlib
        with open("config.yaml") as fh:
            config = yaml.safe_load(fh)

        # Per-offer sheet override (optional).
        # NOTE: resolved into locals and passed explicitly to SheetHandler.
        # Do NOT mutate os.environ here — engines run as concurrent threads in
        # one process, so global env vars would race and an engine could load
        # another offer's worksheet (causing fresh leads to be miscategorised).
        offer_cfg = OFFERS[offer_id]
        url_env = offer_cfg.get("sheet_url_env", "")
        ws_env  = offer_cfg.get("sheet_ws_env",  "")
        sheet_url      = os.getenv(url_env) or os.getenv("GOOGLE_SHEET_URL", "")
        worksheet_name = os.getenv(ws_env)  or os.getenv("GOOGLE_SHEET_WORKSHEET", "Sheet1")

        _log(offer_id, f"INFO  Offer: {OFFERS[offer_id]['name']}")
        _log(offer_id, "INFO  Mode: website classify (Duplicate / Fresh via email + SSN on site)")
        _log(offer_id, f"INFO  Connecting to Google Sheets (worksheet: {worksheet_name})...")

        from utils.sheet_handler import SheetHandler
        from core.exceptions import FormFillerError

        sheet = SheetHandler(config, worksheet_name=worksheet_name, sheet_url=sheet_url)
        direct_ip = _get_outbound_ip(None)

        # Load the form filler for this offer
        filler_mod = offer_cfg.get("filler", "core.form_filler")
        site_url   = offer_cfg.get("url", "")
        mod = importlib.import_module(filler_mod)
        offer_config = dict(config)
        offer_config["target"] = {**config.get("target", {}), "url": site_url}
        from pathlib import Path as _Path
        ss_dir = _Path(f"screenshots/{offer_id}")
        ss_dir.mkdir(parents=True, exist_ok=True)
        offer_config.setdefault("screenshots", {})["directory"] = str(ss_dir)

        pending = sheet.get_pending_rows()
        if not pending:
            _log(offer_id, "WARN  No pending rows found. Nothing to do.")
            return

        eng["stats"]["total"] = len(pending)
        max_concurrent = int(os.getenv("MAX_CONCURRENT_ROWS", "5"))
        _log(offer_id, f"INFO  {len(pending)} pending row(s) — {max_concurrent} concurrent workers — no proxy — single device")

        stats_lock = threading.Lock()

        def _process_one(row: dict) -> None:
            _tl.offer_id = offer_id  # thread-local so logging routes correctly
            if stop_event.is_set():
                return
            row_num = row["_row_number"]
            _log(offer_id, f"INFO  -- Row {row_num} --")

            # Each thread needs its own FormFiller — _classify_only is instance state
            thread_filler = mod.FormFiller(offer_config)
            status = "Failed"
            notes  = ""
            try:
                result = thread_filler.classify_lead_on_site(
                    row=row,
                    proxy_url=None,
                    row_number=row_num,
                    stop_event=stop_event,
                )
                status = result.get("status", "Fresh")
                notes  = result.get("notes", "")
            except FormFillerError as exc:
                if exc.error_type == "duplicate":
                    status = "Duplicate"
                    notes  = str(exc)
                else:
                    status = "Failed"
                    notes  = f"[{exc.error_type}] {exc}"
                    _log(offer_id, f"ERR   Row {row_num} classify failed ({exc.error_type}): {exc}")

            sheet.update_row(
                row_num,
                status=status,
                notes=notes,
                proxy_used="direct",
                ip=direct_ip,
            )

            with stats_lock:
                eng["stats"]["processed"] += 1
                if status == "Duplicate":
                    eng["stats"]["duplicate"] += 1
                    _log(offer_id, f"WARN  Row {row_num} -> Duplicate (website)")
                elif status == "Fresh":
                    eng["stats"]["fresh"] += 1
                    _log(offer_id, f"OK    Row {row_num} -> Fresh (website)")
                else:
                    _log(offer_id, f"ERR   Row {row_num} -> {status}")

        with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            futures = {
                executor.submit(_process_one, row): row["_row_number"]
                for row in pending
            }
            for future in as_completed(futures):
                if stop_event.is_set():
                    _log(offer_id, "INFO  Stop signal received -- halting.")
                    # cancel queued futures that haven't started yet
                    for f in futures:
                        f.cancel()
                    break
                try:
                    future.result()
                except Exception as exc:
                    row_num = futures[future]
                    _log(offer_id, f"ERR   Row {row_num} worker crashed: {exc!r}")

        s = eng["stats"]
        _log(offer_id,
             f"DONE  Run complete -- Fresh: {s['fresh']}  "
             f"Duplicate: {s['duplicate']}  "
             f"Processed: {s['processed']}/{s['total']}")

    except Exception as e:
        _log(offer_id, f"FATAL Engine crashed: {type(e).__name__}: {e!r}")
        _log(offer_id, traceback.format_exc())
    finally:
        eng["running"] = False
        _tl.offer_id   = None


# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Lead Automation Engine</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Segoe UI', system-ui, sans-serif;
  background: #0b0e18; color: #e2e8f0;
  min-height: 100vh; padding: 22px 14px 50px;
}
.wrap { max-width: 1080px; margin: 0 auto; }
.header {
  display: flex; align-items: flex-start; justify-content: space-between;
  margin-bottom: 22px; flex-wrap: wrap; gap: 12px;
}
h1 { font-size: 1.45rem; font-weight: 700; color: #7dd3fc; letter-spacing: -.5px; }
.subtitle { font-size: .78rem; color: #4b5563; margin-top: 3px; }
.stop-all {
  padding: 9px 20px; background: #7f1d1d; color: #fca5a5;
  border: 1px solid #b91c1c; border-radius: 8px;
  font-size: .82rem; font-weight: 600; cursor: pointer; white-space: nowrap;
}
.stop-all:hover { background: #991b1b; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }
.card {
  background: #131926; border: 2px solid #1e2d40;
  border-radius: 12px; overflow: hidden;
  display: flex; flex-direction: column; transition: border-color .2s;
}
.card.is-running  { border-color: #166534; }
.card.is-stopping { border-color: #7c2d12; }
.card-head { padding: 14px 16px 12px; }
.name-row {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 4px;
}
.offer-name { font-size: .95rem; font-weight: 700; }
.offer-url {
  font-size: .61rem; color: #374151; word-break: break-all;
  margin-bottom: 11px; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis;
}
.badge {
  padding: 3px 10px; border-radius: 20px;
  font-size: .64rem; font-weight: 700; letter-spacing: .5px; white-space: nowrap;
}
.badge.idle     { background: #1a2d47; color: #60a5fa; }
.badge.running  { background: #14532d; color: #4ade80; animation: pulse 1.4s infinite; }
.badge.stopping { background: #431407; color: #fb923c; animation: pulse 1s infinite; }
@keyframes pulse { 0%,100%{opacity:1}50%{opacity:.5} }
.ctrls { display: flex; gap: 8px; margin-bottom: 11px; }
.btn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 7px 15px; font-size: .78rem; font-weight: 600;
  border: none; border-radius: 7px; cursor: pointer; transition: opacity .15s;
}
.btn:disabled { opacity: .3; cursor: not-allowed; }
.btn-start { background: #16a34a; color: #f0fdf4; }
.btn-start:hover:not(:disabled) { background: #22c55e; }
.btn-stop  { background: #dc2626; color: #fff; }
.btn-stop:hover:not(:disabled)  { background: #ef4444; }
.stats { display: flex; gap: 5px; }
.stat {
  flex: 1; background: #0d1117; border: 1px solid #1e2d40;
  border-radius: 6px; padding: 6px 4px; text-align: center;
}
.stat-val { font-size: 1.05rem; font-weight: 700; line-height: 1.1; }
.stat-lbl {
  font-size: .52rem; color: #4b5563; margin-top: 2px;
  text-transform: uppercase; letter-spacing: .5px;
}
.sv-ok   .stat-val { color: #4ade80; }
.sv-dup  .stat-val { color: #facc15; }
.sv-fail .stat-val { color: #f87171; }
.sv-tot  .stat-val { color: #7dd3fc; }
.ss-wrap {
  position: relative; background: #07090f;
  border-top: 1px solid #1a2535;
  min-height: 80px; max-height: 200px; overflow: hidden;
  display: flex; align-items: center; justify-content: center;
}
.ss-img { width: 100%; height: auto; display: none; max-height: 200px; object-fit: contain; }
.ss-ph  { font-size: .68rem; color: #1f2937; }
.step-lbl {
  position: absolute; bottom: 0; left: 0; right: 0;
  background: rgba(7,9,15,.8); text-align: center;
  font-size: .6rem; color: #60a5fa; padding: 3px 6px;
}
.mini-log {
  flex: 1; min-height: 120px; max-height: 160px; overflow-y: auto;
  padding: 8px 10px;
  font-family: 'JetBrains Mono','Cascadia Code','Fira Code',monospace;
  font-size: .66rem; line-height: 1.65;
  border-top: 1px solid #1a2535; background: #07090f;
}
.ll   { white-space: pre-wrap; word-break: break-all; }
.ok   { color: #4ade80; }
.warn { color: #facc15; }
.err  { color: #f87171; }
.rty  { color: #fb923c; }
.done { color: #c4b5fd; font-weight: 600; }
.ftl  { color: #f43f5e; font-weight: 700; }
.inf  { color: #64748b; }
.muted{ color: #1f2937; font-style: italic; }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div>
      <h1>Lead Automation Engine</h1>
      <p class="subtitle">Run multiple offers simultaneously — each card is fully independent.</p>
    </div>
    <button class="stop-all" onclick="stopAll()">&#9632; Stop All Running</button>
  </div>

  <div class="grid">
    {% for key, offer in offers.items() %}
    <div class="card" id="card-{{ key }}">
      <div class="card-head">
        <div class="name-row">
          <span class="offer-name" style="color:{{ offer.color }}">{{ offer.name }}</span>
          <span class="badge idle" id="badge-{{ key }}">IDLE</span>
        </div>
        <div class="offer-url" title="{{ offer.url }}">{{ offer.url }}</div>
        <div class="ctrls">
          <button class="btn btn-start" id="btn-start-{{ key }}"
                  onclick="startEngine('{{ key }}')">&#9654; Start</button>
          <button class="btn btn-stop"  id="btn-stop-{{ key }}"
                  onclick="stopEngine('{{ key }}')" disabled>&#9632; Stop</button>
        </div>
        <div class="stats">
          <div class="stat sv-ok">
            <div class="stat-val" id="s-fresh-{{ key }}">0</div>
            <div class="stat-lbl">Fresh</div>
          </div>
          <div class="stat sv-dup">
            <div class="stat-val" id="s-dup-{{ key }}">0</div>
            <div class="stat-lbl">Dup</div>
          </div>
          <div class="stat sv-tot">
            <div class="stat-val" id="s-proc-{{ key }}">0</div>
            <div class="stat-lbl">Done</div>
          </div>
          <div class="stat sv-tot">
            <div class="stat-val" id="s-tot-{{ key }}">0</div>
            <div class="stat-lbl">Total</div>
          </div>
        </div>
      </div>
      <div class="ss-wrap">
        <img class="ss-img" id="ss-{{ key }}" alt="preview"/>
        <div class="ss-ph" id="ss-ph-{{ key }}">no preview</div>
        <div class="step-lbl" id="step-{{ key }}"></div>
      </div>
      <div class="mini-log" id="log-{{ key }}">
        <div class="ll muted">Waiting to start...</div>
      </div>
    </div>
    {% endfor %}
  </div>
</div>

<script>
const OFFER_KEYS = {{ offer_keys | tojson }};
const evtSrcs = {}, pollTmrs = {}, ssTmrs = {}, stopping = {};

function logCls(l) {
  const u = l.toUpperCase();
  if (u.includes('] OK') || u.includes('FRESH')) return 'ok';
  if (u.includes('] WARN'))                        return 'warn';
  if (u.includes('] ERR') || u.includes('] FAIL')) return 'err';
  if (u.includes('] RETRY'))                       return 'rty';
  if (u.includes('] DONE'))                        return 'done';
  if (u.includes('] FATAL'))                       return 'ftl';
  return 'inf';
}

function appendLog(key, line) {
  const box = document.getElementById('log-' + key);
  box.querySelectorAll('.muted').forEach(e => e.remove());
  const d = document.createElement('div');
  d.className = 'll ' + logCls(line);
  d.textContent = line;
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  const all = box.querySelectorAll('.ll');
  if (all.length > 300) all[0].remove();
}

function setRunning(key, running) {
  const card  = document.getElementById('card-' + key);
  const badge = document.getElementById('badge-' + key);
  document.getElementById('btn-start-' + key).disabled = running;
  document.getElementById('btn-stop-'  + key).disabled = !running || !!stopping[key];
  card.classList.toggle('is-running',  running && !stopping[key]);
  card.classList.toggle('is-stopping', !!stopping[key]);
  if (running) {
    stopping[key] = false;
    badge.className = 'badge running'; badge.textContent = 'RUNNING';
  } else {
    badge.className = 'badge idle'; badge.textContent = 'IDLE';
    stopping[key] = false; card.classList.remove('is-stopping');
  }
}

function updStats(key, s) {
  document.getElementById('s-fresh-' + key).textContent = s.fresh ?? 0;
  document.getElementById('s-dup-'   + key).textContent = s.duplicate;
  document.getElementById('s-proc-'  + key).textContent = s.processed;
  document.getElementById('s-tot-'   + key).textContent = s.total;
}

function startSsPoll(key) {
  if (ssTmrs[key]) clearInterval(ssTmrs[key]);
  ssTmrs[key] = setInterval(() => {
    const probe = new Image();
    probe.onload = () => {
      const img = document.getElementById('ss-' + key);
      const ph  = document.getElementById('ss-ph-' + key);
      img.src = probe.src; img.style.display = 'block'; ph.style.display = 'none';
    };
    probe.src = '/screenshot/' + key + '?t=' + Date.now();
  }, 1500);
}
function stopSsPoll(key) { if (ssTmrs[key]) { clearInterval(ssTmrs[key]); delete ssTmrs[key]; } }

function startSSE(key) {
  if (evtSrcs[key]) evtSrcs[key].close();
  evtSrcs[key] = new EventSource('/logs/' + key);
  evtSrcs[key].onmessage = e => {
    if (!e.data || !e.data.trim()) return;
    appendLog(key, e.data);
    const m = e.data.match(/form\.step.*?step=(\d+).*?title="([^"]+)"/);
    if (m) document.getElementById('step-' + key).textContent =
             'Step ' + m[1] + ': ' + m[2].substring(0, 38);
  };
  evtSrcs[key].onerror = () => setTimeout(() => startSSE(key), 2000);
}

function startPoll(key) {
  if (pollTmrs[key]) clearInterval(pollTmrs[key]);
  pollTmrs[key] = setInterval(async () => {
    try {
      const d = await fetch('/status').then(r => r.json());
      const eng = d[key]; if (!eng) return;
      setRunning(key, eng.running); updStats(key, eng.stats);
      if (!eng.running) {
        clearInterval(pollTmrs[key]); delete pollTmrs[key];
        stopSsPoll(key);
        document.getElementById('step-' + key).textContent = '';
      }
    } catch(_) {}
  }, 1500);
}

async function startEngine(key) {
  document.getElementById('btn-start-' + key).disabled = true;
  const d = await fetch('/start/' + key, { method: 'POST' })
    .then(r => r.json()).catch(() => ({ ok: false, msg: 'Network error' }));
  if (d.ok) { setRunning(key, true); startSSE(key); startPoll(key); startSsPoll(key); }
  else { alert(d.msg || 'Could not start.'); document.getElementById('btn-start-' + key).disabled = false; }
}

async function stopEngine(key) {
  stopping[key] = true;
  document.getElementById('btn-stop-' + key).disabled = true;
  const badge = document.getElementById('badge-' + key);
  badge.className = 'badge stopping'; badge.textContent = 'STOPPING';
  document.getElementById('card-' + key).classList.replace('is-running', 'is-stopping');
  await fetch('/stop/' + key, { method: 'POST' }).catch(() => {});
}

function stopAll() {
  OFFER_KEYS.forEach(key => {
    const b = document.getElementById('badge-' + key);
    if (b && b.textContent === 'RUNNING') stopEngine(key);
  });
}

(async () => {
  try {
    const d = await fetch('/status').then(r => r.json());
    OFFER_KEYS.forEach(key => {
      const eng = d[key]; if (!eng) return;
      setRunning(key, eng.running); updStats(key, eng.stats);
      if (eng.running) { startSSE(key); startPoll(key); startSsPoll(key); }
    });
  } catch(_) {}
})();
</script>
</body>
</html>
"""


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(_HTML, offers=OFFERS, offer_keys=list(OFFERS.keys()))


@app.route("/start/<offer_id>", methods=["POST"])
def start(offer_id: str):
    if offer_id not in OFFERS:
        return jsonify({"ok": False, "msg": f"Unknown offer: {offer_id}"})
    eng = _engines[offer_id]
    if eng["running"]:
        return jsonify({"ok": False, "msg": f"{OFFERS[offer_id]['name']} is already running."})
    offer = OFFERS[offer_id]
    eng["stop_event"].clear()
    while not eng["log_queue"].empty():
        try:
            eng["log_queue"].get_nowait()
        except queue.Empty:
            break
    t = threading.Thread(target=_run_engine, args=(offer_id, offer["url"]), daemon=True)
    eng["thread"] = t
    t.start()
    _log(offer_id, f"INFO  Engine started -- {offer['name']}")
    return jsonify({"ok": True})


@app.route("/stop/<offer_id>", methods=["POST"])
def stop(offer_id: str):
    if offer_id not in _engines:
        return jsonify({"ok": False, "msg": "Unknown offer"})
    _engines[offer_id]["stop_event"].set()
    _log(offer_id, "INFO  Stop requested -- will halt after the current form step...")
    return jsonify({"ok": True})


@app.route("/status")
def status():
    return jsonify({
        oid: {"running": eng["running"], "stats": eng["stats"]}
        for oid, eng in _engines.items()
    })


@app.route("/logs/<offer_id>")
def logs(offer_id: str):
    if offer_id not in _engines:
        return "", 404

    def stream():
        q = _engines[offer_id]["log_queue"]
        while True:
            try:
                msg = q.get(timeout=1.0)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield "data: \n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/screenshot/<offer_id>")
def screenshot(offer_id: str):
    if offer_id not in OFFERS:
        return "", 404
    p = _ss_path(offer_id)
    if not p.exists():
        return "", 204
    try:
        data = p.read_bytes()
    except OSError:
        return "", 204
    resp = make_response(data)
    resp.headers["Content-Type"]  = "image/png"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _setup_structlog()
    print("\n  Lead Automation UI  (multi-engine)")
    print("  ──────────────────────────────────")
    print("  Open in browser:  http://localhost:8080\n")
    app.run(host="0.0.0.0", port=8080, debug=False,
            use_reloader=False, threaded=True)
