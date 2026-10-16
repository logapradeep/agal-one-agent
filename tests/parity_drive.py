#!/usr/bin/env python3
"""Drive a whole agent — ``main()`` — through a scripted session with fakes, and write down everything it
did: every GPIO line it opened, wrote or let go of, every MQTT publish, every HTTPS request, every OTA
update it started (and what it named as ON then). No broker, no network, no board.

Run by tests/test_v1_parity.py as a subprocess, once with the agent of THIS tree and once with the
released 0.3.4 (``git archive v0.3.4``), each on ``PYTHONPATH``:

    AGENT_ROOT=<tree> PYTHONPATH=<tree> python tests/parity_drive.py <scenario> <out.json> <workdir>

Scenarios:

``v1-light``  The production node as it is today (no prod read — rebuilt from the contracts): one card,
              "Light" (aab_lighting_circuit, the contracts' default block), ``device.power`` on DO1 =
              line 17 of pinctrl-bcm2835, active low, no other port linked, the empty NAB, a port-table
              node. Two boots on one state directory: a cold one that pulls the language-1 bundle and
              is sent every command type this agent knows (signed, as the backend sends them) plus every
              refusal of the gate (unsigned, forged, replayed, stale, a stale firmwareUpdate, another
              node's token), a program reload, two OTA updates (the light on, then off), a clean stop;
              then a warm one that loads the persisted program, switches the light on and stops.
``v3-gate``   (0.4.0 only) The pump house of the golden vectors (two-node-layout.json, bundle v3) on the
              same fakes: the wave-1 commands — resetValve, pauseAutomation, resumeAutomation — unsigned,
              forged, stale and replayed, then signed; an OTA update; a restart.

The output is a list of records in the order they happened (``seq``), each with its ``kind``
(``gpio`` | ``mqtt`` | ``https`` | ``ota`` | ``step``). Nothing is normalised here: the test does it.
"""

from __future__ import annotations

import email.utils
import importlib.metadata as _md
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from email.message import Message

# Each tree reports the version its own source says (the __init__.py fallback): the venv's editable
# install would otherwise answer with its own metadata for both.
_orig_version = _md.version


def _version(name):
    if name == "agal-one-agent":
        raise _md.PackageNotFoundError(name)
    return _orig_version(name)


_md.version = _version

import agal_one_agent  # noqa: E402

ROOT = os.path.realpath(os.environ["AGENT_ROOT"])
assert os.path.realpath(agal_one_agent.__file__).startswith(ROOT + os.sep), \
    f"imported {agal_one_agent.__file__}, not the agent of {ROOT}"

from agal_one_agent import http_reporter as http_reporter_mod  # noqa: E402
from agal_one_agent import main as agent_main  # noqa: E402
from agal_one_agent import ota_updater  # noqa: E402
from agal_one_agent.command_signing import sign_command  # noqa: E402
from agal_one_agent.mqtt_client import AgalOneMqttClient  # noqa: E402
from agal_one_agent.ports import gpiochip, inventory  # noqa: E402

NODE = "PumpHouseNode0000001"   # not the production uid: nothing here is the real node
TOKEN = "node-token-for-the-parity-drive-0000000000000"
OTHER_TOKEN = "another-node-token-000000000000000000000000"
LIGHT = "lightCard00000000001"
STRANGER = "strangerCard00000001"
CHIP = "pinctrl-bcm2835"
HOST = "https://ingress.example/"
INGRESS = HOST + "telemetryIngress"
TICK = 0.25

RECORDS: list[dict] = []
_LOCK = threading.RLock()  # a predicate given to wait_for may read the records again
_COND = threading.Condition(_LOCK)
STATE: dict = {}
HANDLERS: dict = {}
LEVELS: dict[int, bool] = {}     # line -> the physical level a read gets (default low)
LOG_READS = {"on": True}


def record(kind: str, **fields) -> None:
    with _COND:
        RECORDS.append({"seq": len(RECORDS), "kind": kind, "t": round(time.monotonic(), 3), **fields})
        _COND.notify_all()


def wait_for(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    with _COND:
        while True:
            if any(pred(r) for r in RECORDS):
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            _COND.wait(left)


def quiet(seconds: float, timeout: float = 30.0) -> None:
    """Until nothing new was recorded for ``seconds`` (the queues drained, the passes settled)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _COND:
            n = len(RECORDS)
        time.sleep(seconds)
        with _COND:
            if len(RECORDS) == n:
                return


# ---------------------------------------------------------------------------------------- fake GPIO

class FakeLine:
    def __init__(self, path, line, direction, bias=None):
        self.line, self.direction = int(line), direction
        record("gpio", op="open", chip=path, line=self.line, direction=direction, bias=bias)

    def write(self, level):
        record("gpio", op="write", line=self.line, level=bool(level))

    def read(self):
        v = bool(LEVELS.get(self.line, False))
        if LOG_READS["on"]:
            record("gpio", op="read", line=self.line, level=v)
        return v

    def close(self):
        record("gpio", op="close", line=self.line)


def fake_open(path, line, direction, **kw):
    return FakeLine(path, line, direction, bias=kw.get("bias"))


def fake_chips(*a, **kw):
    return [gpiochip.ChipInfo(path="/dev/gpiochip0", name="gpiochip0", label=CHIP, lines=54)]


# ---------------------------------------------------------------------------------------- fake MQTT

class FakePaho:
    def publish(self, topic, payload, qos=0, retain=False):
        try:
            body = json.loads(payload)
        except Exception:  # noqa: BLE001
            body = str(payload)
        record("mqtt", topic=topic, qos=qos, retain=retain, body=body)

    def loop_stop(self):
        pass

    def disconnect(self):
        record("mqtt", topic="(disconnect)", body=None)

    def subscribe(self, *a, **kw):
        pass


def fake_connect(self):
    STATE["mqtt"] = self
    self._client = FakePaho()
    self._connected = True


class Msg:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.topic = f"agal/{NODE}/commands"


def send(raw: bytes) -> None:
    STATE["mqtt"]._on_message(None, None, Msg(raw))


def signed(message: dict, token: str = TOKEN, age_ms: int = 0) -> bytes:
    now_ms = int(time.time() * 1000)
    return json.dumps(sign_command(message, token, now_ms - age_ms)[0]).encode("utf-8")


# ---------------------------------------------------------------------------------------- fake ingress

class Answer:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = json.dumps(body).encode("utf-8")
        self.headers = Message()
        self.headers["Date"] = email.utils.formatdate(usegmt=True)

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(req, timeout=None):
    url = req.full_url
    body = json.loads(req.data.decode("utf-8")) if req.data else None
    endpoint = url[len(HOST):] if url.startswith(HOST) else url
    record("https", endpoint=endpoint, auth=req.get_header("Authorization"), body=body)
    if not url.startswith(HOST):
        raise urllib.error.URLError(f"not reached in the parity drive: {url}")
    if endpoint == "reportHardware":
        return Answer(200, {"boardMatch": "matched", "boardProfileId": "rpi-3b", "portsVersion": STATE.get("portsVersion", 1)})
    if endpoint == "getBootState":
        return Answer(200, {"commands": []})
    if endpoint == "getProgram":
        bundle = STATE.get("bundle")
        if bundle is None:
            return Answer(200, {"version": 0, "unchanged": True})
        current = (body or {}).get("currentVersion")
        if current == bundle["version"]:
            return Answer(200, {"version": bundle["version"], "unchanged": True})
        return Answer(200, {"version": bundle["version"], "bundle": bundle})
    return Answer(200, {"success": True})


# ---------------------------------------------------------------------------------------- fake OTA

def fake_perform_update(target_version, command_id="", rollout_id="", node_uid="", source=None, sha256=None,
                        outputs_on=None, **kw):
    record("ota", version=target_version, commandId=command_id, rolloutId=rollout_id, nodeUid=node_uid,
           source=source, sha256=sha256, outputsOn=outputs_on() if outputs_on is not None else None)
    return "parity drive: no install"


# ---------------------------------------------------------------------------------------- the agent

def install_fakes() -> None:
    gpiochip.LineIO._periphery_open = staticmethod(fake_open)
    gpiochip.list_chips = fake_chips
    inventory.list_chips = fake_chips
    urllib.request.urlopen = fake_urlopen
    http_reporter_mod.BOOT_STATE_URL = HOST + "getBootState"
    ota_updater.settle_interrupted = lambda node_uid="", **kw: False
    ota_updater.begin_probation = lambda version, **kw: False
    ota_updater.perform_update = fake_perform_update
    agent_main.signal.signal = lambda signum, handler: HANDLERS.__setitem__(signum, handler)
    AgalOneMqttClient.connect = fake_connect


def write_config(workdir: str) -> str:
    import yaml
    path = os.path.join(workdir, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump({
            "node": {"uid": NODE, "name": "Pump house", "auth_token": TOKEN},
            "mqtt": {"broker": "broker.example", "username": "shared-device-login", "password": "shared-device-password"},
            "telemetry": {"ingress_url": INGRESS, "buffer_db_path": os.path.join(workdir, "buffer.db"),
                          "heartbeat_seconds": 3600},
            "blocks": {"state_dir": os.path.join(workdir, "state"), "tick_seconds": TICK},
        }, f)
    return path


def boot(config_path: str, label: str) -> threading.Thread:
    STATE.pop("mqtt", None)
    HANDLERS.clear()
    sys.argv = ["agal-one-agent", "--config", config_path]
    record("step", label=f"boot {label}")
    t = threading.Thread(target=agent_main.main, name=f"agent-{label}", daemon=True)
    t.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (STATE.get("mqtt") is not None and STATE["mqtt"].is_connected and HANDLERS):
        time.sleep(0.05)
    assert STATE.get("mqtt") is not None, "the agent never connected"
    return t


def stop(t: threading.Thread, label: str) -> None:
    quiet(1.0)
    record("step", label=f"stop {label}")
    import signal as _signal
    HANDLERS[_signal.SIGTERM](_signal.SIGTERM, None)
    t.join(timeout=60)
    assert not t.is_alive(), "the agent did not stop"
    record("step", label=f"stopped {label}")


def final_ack(cid: str):
    def pred(r):
        b = r.get("body") if r["kind"] == "mqtt" else None
        p = (b or {}).get("payload") if isinstance(b, dict) else None
        return isinstance(p, dict) and p.get("commandId") == cid and p.get("status") in ("completed", "failed")
    return pred


def step(label: str, raw: bytes, cid: str = None, settle: float = 0.6, ack_timeout: float = 8.0) -> None:
    """One message on the command topic; then its final ack (when it gets one), then until nothing more happens
    for ``settle`` s — the pass that carries it out, the HTTPS posts that follow — so that what a step caused is
    recorded before the next step, on a slow machine too."""
    record("step", label=label)
    send(raw)
    if cid is not None:
        wait_for(final_ack(cid), ack_timeout)
    time.sleep(TICK)
    quiet(settle)


def cid(n: int) -> str:
    return f"cmd{n:017d}"  # 20 characters: an /assetCommands auto-id, tracked by the backend


# ---------------------------------------------------------------------------------------- scenarios

def light_bundle(version: int, name: str = "Lighting circuit", wall_switch: bool = False) -> dict:
    """The language-1 bundle the backend's compileNodeProgram builds for the Light card (compile.ts): the
    contracts' default block (aab_lighting_circuit), the linked ports' transports (the port's own: DO1 active
    low), the empty NAB, the node's timezone.

    The production card has one port linked, DO1. The backend compiles a block only when every `node`
    variable's port is linked (validateAab), and the node refuses one that is not ("port … is not resolved
    for this node"), so with DO1 alone the block is the default one without the wall switch and the rule
    that reads it. ``wall_switch=True`` is the whole default block, with the wall switch linked to DI1
    (line 26) — the rule and an input exercised too."""
    contracts = os.environ.get("AGAL_CONTRACTS_DIR") or os.path.join(ROOT, "..", "..", "contracts")
    with open(os.path.join(contracts, "programs", "defaults", "aab_lighting_circuit.json"), encoding="utf-8") as f:
        aab = json.load(f)
    aab = {k: aab[k] for k in ("templateId", "templateVersion", "name", "variables", "rules", "settings", "version", "notes") if k in aab}
    aab["name"] = name
    ports = {"device.power": {"kind": "gpio", "chip": CHIP, "line": 17, "direction": "out", "activeLow": True}}
    if wall_switch:
        ports["sensor.wall_switch"] = {"kind": "gpio", "chip": CHIP, "line": 26, "direction": "in"}
    else:
        aab["variables"] = [v for v in aab["variables"] if v["name"] != "wall_switch"]
        aab["rules"] = [r for r in aab["rules"] if "wall_switch" not in json.dumps(r)]
    return {
        "version": version,
        "compiledAt": "2026-10-16T10:00:00.000Z",
        "timezone": "Asia/Kolkata",
        "nab": {"variables": [], "schedules": [], "rules": []},
        "assets": [{"assetId": LIGHT, "aab": aab, "ports": ports, "assetType": "lighting_circuit", "name": "Light"}],
    }


def program_applied(version: int):
    def pred(r):
        b = r.get("body") if r["kind"] == "https" else None
        return isinstance(b, dict) and b.get("type") == "programAck" and (b.get("payload") or {}).get("version") == version
    return pred


def must(ok: bool, what: str) -> None:
    if not ok:
        record("step", label=f"FAILED: {what}")
        raise AssertionError(what)


ACTOR = {"uid": "ownerUid000000000001", "displayName": "Owner", "via": "app"}


def scenario_v1_light(workdir: str) -> None:
    config_path = write_config(workdir)
    STATE["bundle"] = light_bundle(5)
    STATE["portsVersion"] = 1

    # ---- boot A: cold — the program arrives by getProgram
    t = boot(config_path, "A")
    must(wait_for(program_applied(5), 20), "program v5 acked")
    must(wait_for(lambda r: r["kind"] == "gpio" and r.get("op") == "open" and r.get("line") == 17, 10), "line 17 opened")
    quiet(1.0)

    n = iter(range(1, 1000))
    on = lambda v, k=None: {"type": "setPower", "commandId": k, "assetId": LIGHT, "sourceKey": "device.power", "value": v}  # noqa: E731

    k = cid(next(n)); first = signed(on(True, k)); step("setPower on", first, k)
    k = cid(next(n)); step("setPower off", signed(on(False, k)), k)
    k = cid(next(n)); step("setPortValue 1", signed({**on(1, k), "type": "setPortValue"}), k)
    k = cid(next(n)); step("setPortValue 0", signed({**on(0, k), "type": "setPortValue"}), k)
    k = cid(next(n)); step("setVariable on true", signed({"type": "setVariable", "commandId": k, "assetId": LIGHT, "variable": "on", "value": True}), k)
    k = cid(next(n)); step("setVariable on false", signed({"type": "setVariable", "commandId": k, "assetId": LIGHT, "variable": "on", "value": False}), k)
    k = cid(next(n)); step("setVariable relay (settable)", signed({"type": "setVariable", "commandId": k, "assetId": LIGHT, "variable": "relay", "value": True}), k)
    k = cid(next(n)); step("setVariable not settable", signed({"type": "setVariable", "commandId": k, "assetId": LIGHT, "variable": "wall_switch", "value": True}), k)
    k = cid(next(n)); step("setPower off again", signed(on(False, k)), k)
    k = cid(next(n)); step("runPlot", signed({"type": "runPlot", "commandId": k, "plotId": "plot_north01", "minutes": 5}), k)
    k = cid(next(n)); step("stopPlot", signed({"type": "stopPlot", "commandId": k, "plotId": "plot_north01"}), k)
    k = cid(next(n)); step("resetValve (wave 1)", signed({"type": "resetValve", "commandId": k, "assetId": LIGHT, "actor": ACTOR}), k)
    k = cid(next(n)); step("pauseAutomation (wave 1)", signed({"type": "pauseAutomation", "commandId": k, "stopAll": False, "actor": ACTOR}), k)
    k = cid(next(n)); step("resumeAutomation (wave 1)", signed({"type": "resumeAutomation", "commandId": k, "actor": ACTOR}), k)
    k = cid(next(n)); step("liveWatch", signed({"type": "liveWatch", "commandId": k, "watching": True}), k)
    k = cid(next(n)); step("loraPairingWindow", signed({"type": "loraPairingWindow", "commandId": k, "childShortId": 7, "durationSec": 5}), k)
    k = cid(next(n)); step("loraDownlink", signed({"type": "loraDownlink", "commandId": k, "devEUI": "0011223344556677", "payload": "01", "port": 1}), k)
    k = cid(next(n)); step("readSensor", signed({"type": "readSensor", "commandId": k, "gpioNumber": 4}), k)
    k = cid(next(n)); step("an unknown type", signed({"type": "frobnicate", "commandId": k}), k)
    k = cid(next(n)); step("setPower for a stranger's card", signed({**on(True, k), "assetId": STRANGER}), k)
    k = cid(next(n)); step("setPower by GPIO number only", signed({"type": "setPower", "commandId": k, "gpioNumber": 17, "value": True}), k)
    port = lambda line, direction="out", al=False: {"kind": "gpio", "chip": CHIP, "line": line, "direction": direction, **({"activeLow": True} if al else {})}  # noqa: E731
    k = "test_port_" + cid(next(n))
    step("testPort pulse DO1 (the Light's line, through the program)",
         signed({"type": "testPort", "commandId": k, "assetId": LIGHT, "portId": "DO1", "action": "pulse", "seconds": 1, "transport": port(17, al=True)}), k)
    k = "test_port_" + cid(next(n))
    step("testPort pulse DO4 (a free line)", signed({"type": "testPort", "commandId": k, "portId": "DO4", "action": "pulse", "seconds": 1, "transport": port(24)}), k)
    k = "test_port_" + cid(next(n))
    step("testPort read DI1 (a free line)", signed({"type": "testPort", "commandId": k, "portId": "DI1", "action": "read", "seconds": 1, "transport": port(26, "in")}), k)
    k = "test_port_" + cid(next(n))
    step("testPort naming the Light on a line it is not linked to",
         signed({"type": "testPort", "commandId": k, "assetId": LIGHT, "portId": "DO4", "action": "pulse", "seconds": 1, "transport": port(24)}), k)
    k = "scan_bus_" + cid(next(n))
    step("scanBus", signed({"type": "scanBus", "commandId": k, "bus": {"id": "I2C1", "kind": "i2c", "device": "/dev/i2c-1"}}), k)
    STATE["portsVersion"] = 2
    k = "sync_" + cid(next(n))
    step("syncPorts", signed({"type": "syncPorts", "commandId": k, "portsVersion": 2}), k, settle=1.5)
    STATE["bundle"] = light_bundle(6, "Light")
    k = "sync_" + cid(next(n))
    step("syncProgram v6 (a reload)", signed({"type": "syncProgram", "commandId": k, "version": 6}), k, settle=1.0)
    must(wait_for(program_applied(6), 10), "program v6 acked")
    quiet(1.0)
    k = cid(next(n)); step("setPower on after the reload", signed(on(True, k)), k)
    fw = lambda k: {"type": "firmwareUpdate", "commandId": k, "version": "v0.9.9", "rolloutId": "rollout0001",  # noqa: E731
                    "source": "https://github.com/thuvagai/agal-one-agent/archive/refs/tags/v0.9.9.tar.gz", "sha256": "ab" * 32}
    k = "fwCmd" + cid(next(n))[5:]
    step("firmwareUpdate while the light is ON", signed(fw(k)), None, settle=1.0)
    k = cid(next(n)); step("setPower off", signed(on(False, k)), k)
    k = "fwCmd" + cid(next(n))[5:]
    step("firmwareUpdate while the light is off", signed(fw(k)), None, settle=1.0)
    k = "sync_" + cid(next(n))
    step("syncPinConfig (no pins)", signed({"type": "syncPinConfig", "commandId": k, "pins": []}), k)

    # ---- the gate's refusals
    k = cid(next(n))
    step("unsigned setPower", json.dumps(on(True, k)).encode("utf-8"), None)
    k = cid(next(n))
    forged = json.loads(signed(on(False, k)))
    forged["value"] = True
    step("forged setPower (value changed after signing)", json.dumps(forged).encode("utf-8"), k)
    step("replay of the first setPower", first, None)
    k = cid(next(n))
    step("stale setPower (signed an hour ago)", signed(on(True, k), age_ms=3600 * 1000), k)
    k = "fwCmd" + cid(next(n))[5:]
    step("stale firmwareUpdate", signed(fw(k), age_ms=3600 * 1000), None, settle=1.0)
    k = cid(next(n))
    step("setPower signed with another node's token", signed(on(True, k), token=OTHER_TOKEN), k)
    step("not a command", b"{not json", None)
    stop(t, "A")

    # ---- boot B: warm — the persisted program loads before MQTT connects
    t = boot(config_path, "B")
    since = _boot_seq("B")
    must(wait_for(lambda r: r["kind"] == "gpio" and r.get("op") == "open" and r.get("line") == 17
                  and r["seq"] > since, 10), "line 17 opened after the restart")
    quiet(1.0)
    k = cid(next(n)); step("setPower on after the restart", signed(on(True, k)), k)
    stop(t, "B")


def _boot_seq(label: str) -> int:
    with _COND:
        return max(r["seq"] for r in RECORDS if r["kind"] == "step" and r["label"] == f"boot {label}")


def scenario_v1_switch(workdir: str) -> None:
    """The whole default lighting block, its wall switch on DI1: the rule toggles the light."""
    LOG_READS["on"] = False  # the sampler reads the switch every 50 ms
    config_path = write_config(workdir)
    STATE["bundle"] = light_bundle(3, wall_switch=True)
    STATE["portsVersion"] = 1
    t = boot(config_path, "A")
    must(wait_for(program_applied(3), 20), "program v3 acked")
    must(wait_for(lambda r: r["kind"] == "gpio" and r.get("op") == "open" and r.get("line") == 17, 10), "line 17 opened")
    quiet(1.0)
    for i, level in enumerate((True, False, True)):
        before = _writes(17)
        record("step", label=f"wall switch {'up' if level else 'down'}")
        LEVELS[26] = level
        must(_wait_writes(17, before), f"toggle {i + 1} switched the light")
        quiet(0.8)
    k = cid(1)
    step("setPower off", signed({"type": "setPower", "commandId": k, "assetId": LIGHT, "sourceKey": "device.power", "value": False}), k)
    STATE["bundle"] = light_bundle(4, "Light", wall_switch=True)
    k = "sync_" + cid(2)
    step("syncProgram v4 (a reload)", signed({"type": "syncProgram", "commandId": k, "version": 4}), k, settle=1.0)
    must(wait_for(program_applied(4), 10), "program v4 acked")
    quiet(1.0)
    record("step", label="wall switch down")
    LEVELS[26] = False
    quiet(1.5)
    stop(t, "A")


def _writes(line: int) -> int:
    with _COND:
        return sum(1 for r in RECORDS if r["kind"] == "gpio" and r.get("op") == "write" and r.get("line") == line)


def _wait_writes(line: int, before: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _writes(line) > before:
            return True
        time.sleep(0.05)
    return False


def scenario_v3_gate(workdir: str) -> None:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    import v3_bench  # noqa: PLC0415 — the golden vectors' pump house
    b = v3_bench.pumphouse(7)
    LOG_READS["on"] = False  # the v3 sampler reads every input every 100 ms
    # The pump house is healthy: the E-stop's NC contact closed (active low: the line is LOW), its reset
    # released (HIGH), mains and UPS present (active high).
    LEVELS.update({5: False, 19: True, 6: True, 13: True})
    config_path = write_config(workdir)
    STATE["bundle"] = b
    STATE["portsVersion"] = 1
    t = boot(config_path, "A")
    wait_for(lambda r: r["kind"] == "https" and (r.get("body") or {}).get("type") == "programAck", 20)
    quiet(1.5)
    V2 = v3_bench.V2
    reset = lambda k: {"type": "resetValve", "commandId": k, "assetId": V2, "actor": ACTOR}  # noqa: E731
    pause = lambda k: {"type": "pauseAutomation", "commandId": k, "stopAll": False, "actor": ACTOR}  # noqa: E731
    resume = lambda k: {"type": "resumeAutomation", "commandId": k, "actor": ACTOR}  # noqa: E731
    n = iter(range(100, 1000))

    k = cid(next(n)); step("unsigned resetValve", json.dumps(reset(k)).encode("utf-8"), None)
    k = cid(next(n)); forged = json.loads(signed(reset(k))); forged["assetId"] = v3_bench.V1
    step("forged resetValve", json.dumps(forged).encode("utf-8"), k)
    k = cid(next(n)); step("stale resetValve", signed(reset(k), age_ms=3600 * 1000), k)
    k = cid(next(n)); step("resetValve signed with another node's token", signed(reset(k), token=OTHER_TOKEN), k)
    k = cid(next(n)); ok = signed(reset(k)); step("signed resetValve", ok, k, settle=1.5)
    step("replay of the signed resetValve", ok, None, settle=1.0)
    k = cid(next(n)); step("unsigned pauseAutomation", json.dumps(pause(k)).encode("utf-8"), None)
    k = cid(next(n)); step("stale pauseAutomation", signed(pause(k), age_ms=3600 * 1000), k)
    k = cid(next(n)); step("signed pauseAutomation", signed(pause(k)), k, settle=1.0)
    k = cid(next(n)); step("unsigned resumeAutomation", json.dumps(resume(k)).encode("utf-8"), None)
    k = cid(next(n)); step("signed resumeAutomation", signed(resume(k)), k, settle=1.0)
    k = "fwCmd" + cid(next(n))[5:]
    step("firmwareUpdate", signed({"type": "firmwareUpdate", "commandId": k, "version": "v0.9.9", "rolloutId": "rollout0002",
                                   "source": "https://github.com/thuvagai/agal-one-agent/archive/refs/tags/v0.9.9.tar.gz",
                                   "sha256": "cd" * 32}), None, settle=1.0)
    stop(t, "A")

    # A restart after the clean stop: the persisted program loads before MQTT, so the first status carries the
    # built-ins — Valve 2 closed by its reset, vouched for across the clean stop.
    t = boot(config_path, "B")
    quiet(1.5)
    stop(t, "B")


SCENARIOS = {"v1-light": scenario_v1_light, "v1-switch": scenario_v1_switch, "v3-gate": scenario_v3_gate}


def main() -> None:
    scenario, out, workdir = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(workdir, exist_ok=True)
    install_fakes()
    os.environ.pop("SENTRY_DSN", None)
    record("step", label=f"agent {agal_one_agent.__version__} from {ROOT}")
    try:
        SCENARIOS[scenario](workdir)
    finally:
        with _COND:
            data = {"version": agal_one_agent.__version__, "records": list(RECORDS)}
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, default=str)


if __name__ == "__main__":
    main()
