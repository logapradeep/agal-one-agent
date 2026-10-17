"""A 0.4.0 agent that is handed a language-1 bundle behaves exactly as the released 0.3.4 does.

Agent 0.4.0 (bundle v3, ADR-029) is built on 0.3.4 — the release the production Pi runs — and reports
``capabilities.blockLanguage = 3``; until its node is migrated the cloud keeps serving it the v1 bundle,
which it must run as 0.3.4 does (contracts v1.15.0, SYNC.md item 5; programs/README.md §7.2). Proved
here by running the same scripted session through both agents — the released one taken from the tag
(``git archive v0.3.4``), and this tree — and comparing everything each did: every GPIO line opened,
written or let go of, every MQTT publish, every HTTPS request (acks, events, status, program acks,
port tests, the hardware report …), every OTA update started and what it named as ON
(tests/parity_drive.py has the session and the fakes; no broker, no network, no board).

The only differences allowed are the two that make 0.4.0 what it is: the version it names
(``firmwareVersion`` / ``agentVersion``) and ``capabilities.blockLanguage`` — 3 in every status of
0.4.0, absent from every status of 0.3.4. Both are checked, then taken out, and the rest must be equal.

Skipped where the released tree cannot be had (no git, or no tag v0.3.4 — a release archive on a board).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict

import pytest

import _contracts  # noqa: E402 — tests/ is on sys.path (pytest rootdir insertion)

HERE = os.path.dirname(os.path.abspath(__file__))
TREE = os.path.dirname(HERE)
DRIVER = os.path.join(HERE, "parity_drive.py")
RELEASED = "v0.3.4"
VOLATILE = {"t", "seq", "timestamp", "ts", "eventId", "uptime"}


def _released_tree(tmp_root: str) -> str:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    probe = subprocess.run(["git", "-C", TREE, "rev-parse", "--verify", "--quiet", f"{RELEASED}^{{commit}}"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip(f"tag {RELEASED} not in this checkout")
    dest = os.path.join(tmp_root, "released")
    os.makedirs(dest)
    archive = subprocess.run(["git", "-C", TREE, "archive", "--format=tar", RELEASED], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", dest], input=archive.stdout, check=True)
    return dest


def _drive(root: str, scenario: str, workdir: str) -> subprocess.Popen:
    env = dict(os.environ, AGENT_ROOT=root, PYTHONPATH=root, AGAL_CONTRACTS_DIR=_contracts.CONTRACTS)
    env.pop("SENTRY_DSN", None)
    os.makedirs(workdir, exist_ok=True)
    log = open(os.path.join(workdir, "agent.log"), "w", encoding="utf-8")
    return subprocess.Popen([sys.executable, DRIVER, scenario, os.path.join(workdir, "out.json"), os.path.join(workdir, "run")],
                            env=env, stdout=log, stderr=subprocess.STDOUT, cwd=workdir)


def _run_all(jobs: dict[str, tuple[str, str, str]]) -> dict[str, dict]:
    """name → (root, scenario, workdir), run in parallel; name → the driver's output."""
    procs = {name: _drive(*job) for name, job in jobs.items()}
    out = {}
    for name, p in procs.items():
        p.wait(timeout=600)
        workdir = jobs[name][2]
        with open(os.path.join(workdir, "agent.log"), encoding="utf-8") as f:
            log = f.read()
        assert p.returncode == 0, f"{name}: the parity drive failed\n{log[-4000:]}"
        with open(os.path.join(workdir, "out.json"), encoding="utf-8") as f:
            out[name] = json.load(f)
    return out


def _scrub(value, version: str):
    if isinstance(value, dict):
        return {k: _scrub(v, version) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_scrub(v, version) for v in value]
    if value == version:
        return "<agent version>"
    return value


def _stream(r: dict) -> str:
    if r["kind"] == "https":
        return f"https {r['endpoint']} {(r.get('body') or {}).get('type')}"
    if r["kind"] == "mqtt":
        b = r.get("body")
        return f"mqtt {r['topic']} {b.get('type') if isinstance(b, dict) else None}"
    return r["kind"]


def _block_language_taken_out(records: list[dict], expected) -> list[str]:
    """Every status names `expected` as its blockLanguage (None: names none); taken out of the records."""
    seen = []
    for r in records:
        b = r.get("body")
        payload = b.get("payload") if isinstance(b, dict) else None
        if isinstance(b, dict) and b.get("type") == "status" and isinstance(payload, dict):
            caps = payload.get("capabilities")
            if isinstance(caps, dict):
                seen.append(caps.pop("blockLanguage", None))
    assert seen, "no status carried capabilities"
    assert all(v == expected for v in seen), f"blockLanguage {seen}, expected {expected} in every status"
    return seen


def _streams(data: dict) -> dict[str, list]:
    """Per stream (GPIO, each MQTT topic + type, each HTTPS endpoint + type, OTA, the steps), in order —
    each stream is written by one thread or in the order of the session's steps."""
    out: dict[str, list] = defaultdict(list)
    for r in data["records"]:
        if r["kind"] == "step" and r["label"].startswith("agent "):
            continue  # names the tree
        out[_stream(r)].append(_scrub(r, data["version"]))
    return dict(out)


def _gpio_by_step(data: dict) -> list[tuple[str, list]]:
    """What each step of the session did to the lines."""
    steps: list[tuple[str, list]] = []
    for r in data["records"]:
        if r["kind"] == "step":
            steps.append((r["label"] if not r["label"].startswith("agent ") else "start", []))
        elif r["kind"] == "gpio" and steps:
            steps[-1][1].append(_scrub(r, data["version"]))
    return steps


def _first_difference(a: list, b: list) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"#{i}:\n  0.3.4: {json.dumps(x)[:600]}\n  0.4.0: {json.dumps(y)[:600]}"
    return f"lengths {len(a)} vs {len(b)}"


def _assert_same(old: dict, new: dict) -> dict:
    assert old["version"] == "0.3.4", old["version"]
    assert new["version"] == "0.4.1", new["version"]  # the tree's own version (the driver ignores installed metadata)
    _block_language_taken_out(old["records"], None)
    _block_language_taken_out(new["records"], 3)
    so, sn = _streams(old), _streams(new)
    assert sorted(so) == sorted(sn), f"streams differ: {sorted(set(so) ^ set(sn))}"
    for name in so:
        assert so[name] == sn[name], f"{name} differs at {_first_difference(so[name], sn[name])}"
    go, gn = _gpio_by_step(old), _gpio_by_step(new)
    assert [s for s, _ in go] == [s for s, _ in gn]
    for (label, a), (_, b) in zip(go, gn):
        assert a == b, f"GPIO of step {label!r} differs: {_first_difference(a, b)}"
    return {name: len(v) for name, v in so.items()}


@pytest.fixture(scope="module")
def drives(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("parity"))
    released = _released_tree(root)
    jobs = {}
    for scenario in ("v1-light", "v1-switch"):
        jobs[f"0.3.4 {scenario}"] = (released, scenario, os.path.join(root, f"0.3.4-{scenario}"))
        jobs[f"0.4.0 {scenario}"] = (TREE, scenario, os.path.join(root, f"0.4.0-{scenario}"))
    jobs["0.4.0 v3-gate"] = (TREE, "v3-gate", os.path.join(root, "0.4.0-v3-gate"))
    return _run_all(jobs)


@pytest.mark.parametrize("scenario", ["v1-light", "v1-switch"])
def test_a_v1_bundle_runs_on_0_4_0_exactly_as_on_0_3_4(drives, scenario):
    old, new = drives[f"0.3.4 {scenario}"], drives[f"0.4.0 {scenario}"]
    counts = _assert_same(old, new)
    total = sum(counts.values())
    print(f"\n{scenario}: {total} records identical in {len(counts)} streams "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


def test_the_light_session_ran_every_command_type_and_the_gate(drives):
    """The session is what it claims to be: every command type answered, every refusal of the gate seen,
    the light switched, two OTA updates started (the light named ON, then nothing), a reload acked."""
    recs = drives["0.4.0 v1-light"]["records"]
    acks = {(r["body"]["payload"]["commandId"], r["body"]["payload"]["status"], r["body"]["payload"].get("reason"))
            for r in recs if r["kind"] == "https" and (r.get("body") or {}).get("type") == "commandAck"}
    steps = [r["label"] for r in recs if r["kind"] == "step"]
    for t in ("setPower on", "setPortValue 1", "setVariable on true", "runPlot", "stopPlot", "resetValve (wave 1)",
              "pauseAutomation (wave 1)", "resumeAutomation (wave 1)", "liveWatch", "loraPairingWindow", "loraDownlink",
              "readSensor", "an unknown type", "scanBus", "syncPorts", "syncProgram v6 (a reload)", "syncPinConfig (no pins)",
              "unsigned setPower", "forged setPower (value changed after signing)", "replay of the first setPower",
              "stale setPower (signed an hour ago)", "stale firmwareUpdate", "setPower signed with another node's token",
              "boot B", "stopped B"):
        assert t in steps, t
    assert any(s == "failed" and r == "bad_signature" for _, s, r in acks)
    assert any(s == "failed" and r == "stale" for _, s, r in acks)
    ota = [r["outputsOn"] for r in recs if r["kind"] == "ota"]
    assert ota == [["Light (device.power)"], []], ota
    writes = [r["level"] for r in recs if r["kind"] == "gpio" and r.get("op") == "write" and r["line"] == 17]
    assert True in writes and False in writes  # active low: the light switched on (LOW) and off (HIGH)
    # The NAB's alert (the second 0.4.0 review: the alert path moved into the outbox had no 0.3.4 comparison): raised
    # when the light first came on in each boot, posted over HTTPS and copied on the status topic — so the streams
    # `https telemetryIngress alert` and `mqtt …/status alert` exist and are compared above.
    alerts = [r["body"]["payload"] for r in recs if r["kind"] == "https" and (r.get("body") or {}).get("type") == "alert"]
    assert [(a["text"], a["severity"], a.get("ruleId")) for a in alerts] == [("The light is on", "info", "light_on")] * 2, alerts
    copies = [r for r in recs if r["kind"] == "mqtt" and isinstance(r.get("body"), dict) and r["body"].get("type") == "alert"]
    assert len(copies) == 2, copies


# ------------------------------------------------------------------ the wave-1 commands behind the gate

def _acks(recs, cid):
    return [(r["body"]["payload"]["status"], r["body"]["payload"].get("reason"), r["body"]["payload"].get("error"))
            for r in recs if r["kind"] == "https" and (r.get("body") or {}).get("type") == "commandAck"
            and r["body"]["payload"]["commandId"] == cid]


def _between(recs, start_label, end_label=None):
    i = next(i for i, r in enumerate(recs) if r["kind"] == "step" and r["label"] == start_label)
    j = next((k for k in range(i + 1, len(recs)) if recs[k]["kind"] == "step"), len(recs)) if end_label is None else \
        next(k for k in range(i + 1, len(recs)) if recs[k]["kind"] == "step" and recs[k]["label"] == end_label)
    return recs[i + 1:j]


def test_the_wave_1_commands_reach_a_v3_program_only_through_the_signed_command_gate(drives):
    """Bundle v3 on 0.4.0's main(): resetValve, pauseAutomation and resumeAutomation are carried out only when
    signed for this node, fresh and new — an unsigned, forged, stale or replayed copy moves no coil and changes
    nothing; the refusals of tracked ids are answered over HTTPS with their reason; the v3 events (source
    node) go through the 0.3.4 event queue with an eventId; every status names 0.4.0 and blockLanguage 3."""
    data = drives["0.4.0 v3-gate"]
    recs = data["records"]
    assert data["version"] == "0.4.1"
    https = [r for r in recs if r["kind"] == "https"]

    # The cold start of the pump house: Valve 2 (latching) cannot be vouched for — FAULT, posted as a node event.
    faults = [r["body"]["payload"] for r in https if (r.get("body") or {}).get("type") == "sensor_event"
              and r["body"]["payload"]["event"]["type"] == "valve_fault"]
    assert faults and faults[0]["event"]["source"] == "node" and faults[0]["event"]["sourceKey"] == "asset_valve002"
    assert faults[0]["eventId"] and faults[0]["nodeUid"] == "PumpHouseNode0000001"

    coils = (22, 23)  # Valve 2's open and close coils

    def coil_writes(label):
        return [(r["line"], r["level"]) for r in _between(recs, label) if r["kind"] == "gpio"
                and r.get("op") == "write" and r["line"] in coils]

    for label in ("unsigned resetValve", "forged resetValve", "stale resetValve", "resetValve signed with another node's token",
                  "replay of the signed resetValve"):
        assert coil_writes(label) == [], label
    assert coil_writes("signed resetValve") == [(23, True), (23, False)], "one close pulse"

    # The commandIds the session used are in order: 100 unsigned reset, 101 forged, 102 stale, 103 other token, 104 signed …
    assert _acks(recs, "cmd00000000000000100") == []                       # unsigned: only logged
    assert ("failed", "bad_signature") in [a[:2] for a in _acks(recs, "cmd00000000000000101")]
    assert ("failed", "stale") in [a[:2] for a in _acks(recs, "cmd00000000000000102")]
    assert ("failed", "bad_signature") in [a[:2] for a in _acks(recs, "cmd00000000000000103")]
    assert [a[0] for a in _acks(recs, "cmd00000000000000104")][-1] == "completed"
    assert _acks(recs, "cmd00000000000000105") == []                       # unsigned pause
    assert ("failed", "stale") in [a[:2] for a in _acks(recs, "cmd00000000000000106")]
    assert [a[0] for a in _acks(recs, "cmd00000000000000107")][-1] == "completed"
    assert _acks(recs, "cmd00000000000000108") == []                       # unsigned resume
    assert [a[0] for a in _acks(recs, "cmd00000000000000109")][-1] == "completed"

    events = [r["body"]["payload"]["event"] for r in https if (r.get("body") or {}).get("type") == "sensor_event"]
    types = [e["type"] for e in events]
    assert types.count("valve_reset") == 1 and types.count("automation_paused") == 1 and types.count("automation_resumed") == 1
    reset = next(e for e in events if e["type"] == "valve_reset")
    assert reset["source"] == "node" and reset["sourceKey"] == "asset_valve002"
    assert all(r["body"]["payload"].get("eventId") for r in https if (r.get("body") or {}).get("type") == "sensor_event")

    statuses = [r["body"]["payload"] for r in https if (r.get("body") or {}).get("type") == "status"]
    assert statuses and all(s["firmwareVersion"] == "0.4.1" for s in statuses)  # the clean stop's offline one too
    online = [s for s in statuses if s.get("online")]
    assert online and all(s["capabilities"]["blockLanguage"] == 3 for s in online)
    # After the restart the program is loaded before MQTT: its first status carries the built-ins.
    warm = [s for s in online if s.get("programVersion") == 7]
    assert warm and {"assetId": "asset_valve002", "state": "closed"} in warm[0]["valves"], warm
    assert all(r["auth"] == "Bearer node-token-for-the-parity-drive-0000000000000" for r in https
               if r["endpoint"] != "getBootState")
    ota = [r for r in recs if r["kind"] == "ota"]
    assert len(ota) == 1 and ota[0]["outputsOn"] == [], ota  # Valve 2 closed by its reset, nothing on
