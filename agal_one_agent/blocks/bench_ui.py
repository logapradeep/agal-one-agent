"""The bench page — a local web page served by a laptop node.

Shows the in-memory ports, the program version and the plots live, and carries
the buttons that provoke what the physics never does by itself: dry run, a lost
phase, flow on a closed plot, no flow on an open plot, a node outage (the
runtime stops, safe state applies, then it restarts and resumes — R-14, R-21)
and going offline (no heartbeat, no MQTT — the cloud raises node offline,
R-28). Bound to 127.0.0.1 only; exists only with ``blocks.simulated_io``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .bench import BenchPhysics

logger = logging.getLogger(__name__)


class BenchUI:
    def __init__(self, runtime, physics: BenchPhysics, mqtt_client=None, heartbeat=None, program_sync=None,
                 cloud_sink=None, node_uid: str = "", node_name: str = "", host: str = "127.0.0.1", port: int = 8765):
        self.runtime = runtime
        self.physics = physics
        self.mqtt = mqtt_client
        self.heartbeat = heartbeat
        self.program_sync = program_sync
        self.cloud_sink = cloud_sink
        self.node_uid = node_uid
        self.node_name = node_name
        self.host = host
        self.port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.outage_until: Optional[float] = None
        self.offline_until: Optional[float] = None
        self._busy = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> str:
        ui = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # quiet
                logger.debug("bench ui: " + fmt, *args)

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    self._send(200, PAGE.encode("utf-8"), "text/html")
                elif self.path.startswith("/api/state"):
                    self._send(200, json.dumps(ui.state(), default=str).encode("utf-8"))
                else:
                    self._send(404, b'{"error":"not found"}')

            def do_POST(self):
                if not self.path.startswith("/api/fault"):
                    self._send(404, b'{"error":"not found"}')
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    result = ui.fault(body)
                    self._send(200, json.dumps(result, default=str).encode("utf-8"))
                except Exception as e:  # noqa: BLE001
                    self._send(400, json.dumps({"error": str(e)}).encode("utf-8"))

        try:
            self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as e:
            # The port is held (an earlier agent still shutting down, or another
            # tool): take any free port rather than fail the agent (found on the
            # laptop node's second restart, 2026-09-08).
            logger.warning("bench page: port %d unavailable (%s) — using a free port", self.port, e)
            self._server = ThreadingHTTPServer((self.host, 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="bench-ui", daemon=True)
        self._thread.start()
        url = f"http://{self.host}:{self.port}/"
        logger.warning("BENCH PAGE: %s", url)
        self.physics.note(f"bench page at {url}")
        return url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None

    # ----------------------------------------------------------------- state
    def state(self) -> dict:
        bundle = getattr(self.runtime, "bundle", None) or {}
        nab = bundle.get("nab") or {}
        last_ack = getattr(self.cloud_sink, "last_ack", None) if self.cloud_sink else None
        now = time.monotonic()
        return {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "node": {"uid": self.node_uid, "name": self.node_name},
            "mqtt": {"connected": bool(getattr(self.mqtt, "is_connected", False)) if self.mqtt else None},
            "program": {
                "version": getattr(self.runtime, "version", 0),
                "running": bool(getattr(self.runtime, "_running", False)),
                "ack": last_ack,
                "rules": len(nab.get("rules", []) or []),
                "schedules": len(nab.get("schedules", []) or []),
                "assets": len(bundle.get("assets", []) or []),
            },
            "outageSecondsLeft": max(0, round(self.outage_until - now)) if self.outage_until else 0,
            "offlineSecondsLeft": max(0, round(self.offline_until - now)) if self.offline_until else 0,
            **self.physics.describe(),
        }

    # ---------------------------------------------------------------- faults
    def fault(self, body: dict) -> dict:
        kind = body.get("kind")
        if kind == "clear":
            self.physics.clear_faults()
            return {"ok": True}
        if kind == "outage":
            seconds = int(body.get("seconds") or 30)
            self._start_outage(seconds)
            return {"ok": True, "seconds": seconds}
        if kind == "offline":
            seconds = int(body.get("seconds") or 180)
            self._start_offline(seconds)
            return {"ok": True, "seconds": seconds}
        key = self.physics.set_fault(kind, asset_id=body.get("assetId"), plot_id=body.get("plotId"),
                                     phase=body.get("phase"), on=bool(body.get("on", True)))
        return {"ok": True, "key": key}

    def _start_outage(self, seconds: int) -> None:
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("an outage or offline period is already running")

        def run():
            try:
                self.outage_until = time.monotonic() + seconds
                self.physics.note(f"NODE OUTAGE for {seconds}s: runtime stops, safe state applies")
                if self.heartbeat is not None and hasattr(self.heartbeat, "pause"):
                    self.heartbeat.pause()
                self.runtime.stop(safe_state=True)
                time.sleep(seconds)
                # A real node boots from its persisted program: the `on boot`
                # rules run, then interrupted runs resume within resume_within.
                restored = False
                if self.program_sync is not None:
                    try:
                        restored = bool(self.program_sync.load_persisted())
                    except Exception as e:  # noqa: BLE001
                        logger.warning("bench outage: reload failed: %s", e)
                if not restored and getattr(self.runtime, "bundle", None):
                    self.runtime.compile(self.runtime.bundle)
                setattr(self.runtime, "_booted", False)
                self.runtime.start()
                if self.heartbeat is not None and hasattr(self.heartbeat, "resume"):
                    self.heartbeat.resume()
                self.physics.note("node back: program reloaded, boot pass done")
            finally:
                self.outage_until = None
                self._busy.release()

        threading.Thread(target=run, name="bench-outage", daemon=True).start()

    def _start_offline(self, seconds: int) -> None:
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("an outage or offline period is already running")

        def run():
            try:
                self.offline_until = time.monotonic() + seconds
                self.physics.note(f"OFFLINE for {seconds}s: no heartbeat, MQTT disconnected (program keeps running)")
                if self.heartbeat is not None and hasattr(self.heartbeat, "pause"):
                    self.heartbeat.pause()
                if self.mqtt is not None:
                    try:
                        self.mqtt.disconnect()
                    except Exception as e:  # noqa: BLE001
                        logger.debug("bench offline: disconnect: %s", e)
                time.sleep(seconds)
                if self.mqtt is not None:
                    try:
                        self.mqtt.connect()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("bench offline: reconnect failed: %s", e)
                if self.heartbeat is not None and hasattr(self.heartbeat, "resume"):
                    self.heartbeat.resume()
                self.physics.note("back online: MQTT reconnected, heartbeats resumed")
            finally:
                self.offline_until = None
                self._busy.release()

        threading.Thread(target=run, name="bench-offline", daemon=True).start()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agal One · bench</title>
<style>
  :root { --navy:#0F1352; --ink:#1B2032; --slate:#5B6478; --line:#D9DDE6; --tint:#EEF0F7; --green:#2F7D4A; --blue:#2E7BD6; --orange:#E8842B; --red:#C4443A; }
  * { box-sizing: border-box; } body { margin:0; font: 14px/1.45 -apple-system, "Inter Tight", "Helvetica Neue", Arial, sans-serif; color: var(--ink); background:#F7F8FA; }
  header { background: var(--navy); color:#fff; padding: 14px 22px; display:flex; align-items:center; gap:18px; flex-wrap:wrap; }
  header h1 { font: 700 20px/1 "Antonio", "Arial Narrow", sans-serif; letter-spacing:.02em; margin:0; text-transform: uppercase; }
  header .chip { background: rgba(255,255,255,.12); border-radius: 999px; padding: 4px 10px; font-size: 12px; display:inline-flex; align-items:center; gap:6px; }
  .dot { width:8px; height:8px; border-radius:50%; background:#999; display:inline-block; } .dot.on { background:#5BD17C; } .dot.off { background:#FF7B72; }
  main { padding: 18px 22px 40px; display:grid; grid-template-columns: 1.1fr .9fr; gap: 18px; max-width: 1180px; }
  section { background:#fff; border:1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
  h2 { font: 600 12px/1 "Antonio", "Arial Narrow", sans-serif; letter-spacing:.12em; text-transform: uppercase; color: var(--slate); margin: 0 0 10px; }
  table { width:100%; border-collapse: collapse; font-size: 13px; } th, td { text-align:left; padding: 5px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { color: var(--slate); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing:.06em; }
  .k { font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: var(--slate); }
  .val { font-variant-numeric: tabular-nums; } .on { color: var(--green); font-weight:600; } .offv { color: var(--slate); }
  .flow { color: var(--blue); font-weight:600; }
  button { font: inherit; font-size: 12px; font-weight: 600; border: 1px solid var(--line); background: #fff; border-radius: 8px; padding: 6px 10px; cursor: pointer; }
  button:hover { border-color: var(--navy); } button.on { background: var(--orange); border-color: var(--orange); color:#fff; }
  button.danger { color: var(--red); } button.danger.on { background: var(--red); border-color: var(--red); color:#fff; }
  .row { display:flex; gap: 8px; flex-wrap: wrap; align-items:center; margin: 6px 0; }
  .plot { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; margin-bottom: 8px; }
  .plot b { font-size: 14px; } .muted { color: var(--slate); font-size: 12px; }
  .journal { font-family: ui-monospace, Menlo, monospace; font-size: 12px; max-height: 320px; overflow:auto; }
  .journal div { padding: 3px 0; border-bottom: 1px dashed var(--line); } .journal .t { color: var(--slate); margin-right: 8px; }
  .warn { background:#FBE7D3; border-radius: 8px; padding: 8px 10px; font-size: 12px; margin-bottom: 10px; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
</style></head>
<body>
<header>
  <h1>Agal One · bench</h1>
  <span class="chip"><span id="mqttDot" class="dot"></span><span id="nodeName">node</span></span>
  <span class="chip" id="program">program</span>
  <span class="chip" id="clock"></span>
</header>
<main>
  <div>
    <section><h2>Plots</h2><div id="plots"></div></section>
    <section style="margin-top:18px"><h2>Ports (in memory)</h2><div id="ports"></div></section>
  </div>
  <div>
    <section>
      <h2>Node faults</h2>
      <div class="warn">Simulated node. Faults are sticky until cleared; the program reacts as it would on a farm.</div>
      <div class="row">
        <button onclick="fault({kind:'outage',seconds:30})">Node outage 30 s</button>
        <button onclick="fault({kind:'outage',seconds:120})">Outage 2 min</button>
        <button onclick="fault({kind:'offline',seconds:200})">Offline 200 s</button>
        <button class="danger" onclick="fault({kind:'clear'})">Clear all faults</button>
      </div>
      <div id="periods" class="muted"></div>
      <div id="pumpFaults"></div>
    </section>
    <section style="margin-top:18px"><h2>Journal</h2><div id="journal" class="journal"></div></section>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let faults = [];
function has(kind, extra) { return faults.some(f => f.kind === kind && Object.entries(extra).every(([k,v]) => f[k] === v)); }
async function fault(body) {
  const r = await fetch('/api/fault', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if (!r.ok) { const e = await r.json().catch(() => ({})); alert(e.error || 'failed'); }
  refresh();
}
function toggle(kind, extra) { const on = !has(kind, extra); return fault({kind, on, ...extra}); }
function fmt(v) { if (v === true) return '<span class="on">ON</span>'; if (v === false) return '<span class="offv">off</span>'; if (v === null || v === undefined) return '<span class="offv">—</span>'; return '<span class="val">' + (typeof v === 'number' ? v.toFixed(2) : v) + '</span>'; }
async function refresh() {
  let s; try { s = await (await fetch('/api/state')).json(); } catch { return; }
  faults = s.faults || [];
  $('nodeName').textContent = (s.node.name || 'node') + (s.node.uid ? ' · ' + s.node.uid.slice(0, 8) : '');
  $('mqttDot').className = 'dot ' + (s.mqtt.connected === true ? 'on' : (s.mqtt.connected === false ? 'off' : ''));
  const ack = s.program.ack ? ' · ' + s.program.ack.status + (s.program.ack.reason ? ' (' + s.program.ack.reason + ')' : '') : '';
  $('program').textContent = 'program v' + s.program.version + ack + ' · ' + s.program.assets + ' assets · ' + s.program.rules + ' rules · ' + s.program.schedules + ' schedules' + (s.program.running ? '' : ' · RUNTIME STOPPED');
  $('clock').textContent = s.time;
  $('periods').textContent = s.outageSecondsLeft ? 'Outage: ' + s.outageSecondsLeft + ' s left' : (s.offlineSecondsLeft ? 'Offline: ' + s.offlineSecondsLeft + ' s left' : '');
  // plots
  $('plots').innerHTML = (s.plots.length ? '' : '<div class="muted">No program yet — design the field on the phone.</div>') + s.plots.map(p => {
    const valves = p.valves.map(v => `${v.name} ${fmt(v.on)}`).join(' · ') || (p.manual ? 'hand valve' : '<span class="muted">no valves</span>');
    const flow = p.flow ? `${p.flow.name} ${p.flow.flowing ? '<span class="flow">FLOWING</span>' : '<span class="offv">no flow</span>'}` : '<span class="muted">no flow switch</span>';
    const nf = has('no_flow', {plotId: p.plotId}), fc = has('flow_on_closed', {plotId: p.plotId});
    return `<div class="plot"><b>${p.name}</b> <span class="muted">${p.manual ? 'manual plot' : ''}</span><div>${valves}</div><div>${flow}</div>
      <div class="row">${p.flow ? `<button class="${nf ? 'on' : ''}" onclick="toggle('no_flow',{plotId:'${p.plotId}'})">No flow when open</button>
      <button class="${fc ? 'on' : ''}" onclick="toggle('flow_on_closed',{plotId:'${p.plotId}'})">Flow while closed</button>` : ''}</div></div>`;
  }).join('');
  // ports
  $('ports').innerHTML = s.assets.map(a => `<table><thead><tr><th colspan="3">${a.name} <span class="k">${a.type}</span></th></tr></thead><tbody>` +
    a.ports.map(p => `<tr><td class="k">${p.key}</td><td>${p.direction}</td><td>${fmt(p.value)}</td></tr>`).join('') + '</tbody></table>').join('');
  // pump faults
  const pumps = s.assets.filter(a => a.type === 'motor_controller' || a.type === 'dosing_pump');
  $('pumpFaults').innerHTML = pumps.map(a => {
    const three = a.ports.some(p => p.key === 'sensor.current.r');
    const dr = has('dry_run', {assetId: a.assetId});
    const phases = three ? ['r','y','b'].map(ph => `<button class="${has('phase_loss',{assetId:a.assetId, phase:ph}) ? 'on' : ''}" onclick="toggle('phase_loss',{assetId:'${a.assetId}', phase:'${ph}'})">Lose phase ${ph.toUpperCase()}</button>`).join('') : '';
    return `<div class="plot"><b>${a.name}</b> <span class="muted">pump · on for ${(s.physics.pumpOnFor[a.assetId] ?? 0)} s</span>
      <div class="row"><button class="${dr ? 'on' : ''}" onclick="toggle('dry_run',{assetId:'${a.assetId}'})">Dry run (current collapses)</button>${phases}</div>
      <div class="muted">Dry run needs a learned baseline: run a plot ≥ 15 s first. The cutoff fires after 5 s below 80 % of it.</div></div>`;
  }).join('');
  $('journal').innerHTML = (s.journal || []).slice().reverse().map(j => `<div><span class="t">${j.t.slice(11,19)}</span>${j.text}</div>`).join('');
}
refresh(); setInterval(refresh, 1000);
</script>
</body></html>
"""
