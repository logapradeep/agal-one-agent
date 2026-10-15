"""Every command is checked before it is carried out (command_signing.py, contracts v1.14.2).

One broker login is shared by every board, so whoever holds any node's config.yaml could publish
to any node's command topic. The backend now signs every message for one node with that node's
authToken; the node refuses a message that is unsigned, forged, replayed or stale. These tests
run the shared golden vectors (Agal/contracts/nodes/vectors/command-signing.json — the backend's
TypeScript and the reference signing.mjs answer the same), the persisted replay set, the clock
rule, the refusal acks and the wiring at the MQTT boundary and in main(). No broker, no network.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from unittest.mock import MagicMock

import pytest
import yaml

from agal_one_agent import command_signing as cs
from agal_one_agent import main as agent_main
from agal_one_agent.blocks.clock import SystemClock
from agal_one_agent.command_acks import CommandAcks
from agal_one_agent.command_signing import (CanonicalError, CommandGate, ReplayState, ReplayStore,
                                            canonicalize, sign_command, verify_command, wire_command_gate)
from agal_one_agent.config import MqttConfig
from agal_one_agent.http_reporter import HttpReporter
from agal_one_agent.mqtt_client import AgalOneMqttClient

import _contracts  # noqa: E402 — tests/ is on sys.path (pytest rootdir insertion)

VECTORS_PATH = _contracts.COMMAND_SIGNING_VECTORS


def _vectors() -> dict:
    # Not skipped when missing: a conformance check that quietly skips is how a forged command
    # would get through unnoticed. Point AGAL_CONTRACTS_DIR at contracts >= 1.14.2.
    assert os.path.exists(VECTORS_PATH), (
        f"command-signing vectors not found at {VECTORS_PATH} (contracts >= 1.14.2; set AGAL_CONTRACTS_DIR)")
    with open(VECTORS_PATH, encoding="utf-8") as f:
        return json.load(f)


V = _vectors()
TOKEN = V["tokens"]["node"]
OTHER = V["tokens"]["other"]


def _token(name):
    return V["tokens"][name] if name else None


# ======================================================================= the shared vectors

def test_the_vectors_algorithm_block_is_this_implementation():
    a = V["algorithm"]
    assert a["sigAlg"] == cs.SIG_ALG
    assert a["replayWindowMs"] == cs.REPLAY_WINDOW_MS
    assert a["maxAgeMs"] == cs.MAX_AGE_MS
    assert a["maxAheadMs"] == cs.MAX_AHEAD_MS
    assert a["replaySetMax"] == cs.REPLAY_SET_MAX
    assert tuple(a["refusalReasons"]) == cs.REFUSAL_REASONS


@pytest.mark.parametrize("v", V["signing"], ids=[v["name"] for v in V["signing"]])
def test_signing_vector(v):
    message, canonical = sign_command(v["message"], _token(v["token"]), v["sigTs"])
    assert canonical == v["canonical"]
    assert canonicalize({**v["message"], "sigAlg": cs.SIG_ALG, "sigTs": v["sigTs"]}) == v["canonical"]
    assert message["sig"] == v["sig"]
    # The node accepts what the backend signed, sent the way publishCommand sends it.
    verdict = verify_command(json.dumps(message, ensure_ascii=False), _token(v["token"]), ReplayState(),
                             clock_trusted=True, now_ms=v["sigTs"])
    assert verdict.accepted and verdict.verified


def _state(kind, tmp_path):
    return ReplayState() if kind == "memory" else ReplayStore(str(tmp_path / "state"))


@pytest.mark.parametrize("kind", ["memory", "persisted"])
@pytest.mark.parametrize("v", V["verifying"], ids=[v["name"] for v in V["verifying"]])
def test_verifying_vector(v, kind, tmp_path):
    state = _state(kind, tmp_path)
    for i, step in enumerate(v["steps"]):
        # A step's own clockTrusted / nowMs (vectors 1.1.0) replace the sequence's for that step.
        verdict = verify_command(step["wire"], _token(v["token"]), state,
                                 clock_trusted=step.get("clockTrusted", v["clockTrusted"]),
                                 now_ms=step.get("nowMs", v["nowMs"]))
        got = "accepted" if verdict.accepted else verdict.reason
        assert got == step["expect"], f"step {i}: {verdict!r}"
        if "verified" in step:
            assert verdict.verified is step["verified"]
        if "canonical" in step:
            assert cs.signed_text(cs.parse_json(step["wire"])) == step["canonical"]


def test_the_vectors_refuse_with_every_reason_and_accept():
    expects = {s["expect"] for v in V["verifying"] for s in v["steps"]}
    assert set(cs.REFUSAL_REASONS) | {"accepted"} <= expects


@pytest.mark.parametrize("kind", ["memory", "persisted"])
def test_the_10001st_id_evicts_the_oldest_and_raises_the_floor(kind, tmp_path):
    """validate.mjs §4's eviction check, here — and kept across a restart."""
    state = _state(kind, tmp_path)
    t0 = 1792022400000  # 2026-10-15T00:00:00Z
    for i in range(cs.REPLAY_SET_MAX):
        state.seen[f"seed{i}"] = t0 + i
    state.high_water = t0 + cs.REPLAY_SET_MAX - 1
    if kind == "persisted":
        state._compact()  # the 10,000 on the card, as 10,000 accepted commands would have left them

    def msg(cid, ts):
        return json.dumps(sign_command({"commandId": cid, "type": "syncProgram", "version": 1, "timestamp": t0},
                                       TOKEN, ts)[0])

    now = dict(clock_trusted=True, now_ms=t0 + cs.REPLAY_SET_MAX)  # all within 10 s: the set and its floor decide
    assert verify_command(msg("EvictNewest000000001", t0 + cs.REPLAY_SET_MAX), TOKEN, state, **now).accepted
    assert len(state.seen) == cs.REPLAY_SET_MAX and "seed0" not in state.seen
    assert state.evicted_floor == t0
    assert verify_command(msg("seed0", t0), TOKEN, state, **now).reason == "stale"
    assert verify_command(msg("seed1", t0 + 1), TOKEN, state, **now).reason == "replayed"
    if kind == "persisted":
        again = ReplayStore(str(tmp_path / "state"))
        assert again.evicted_floor == t0 and again.has("EvictNewest000000001") and not again.has("seed0")
        assert len(again.seen) == cs.REPLAY_SET_MAX
        assert verify_command(msg("seed0", t0), TOKEN, again, **now).reason == "stale"


# ======================================================================= §3 canonical JSON

# String(JSON.parse(x)) from Node 22 for each literal (generated 2026-10-15; the ECMAScript
# Number::toString the backend's signer writes).
ES_NUMBERS = {
    "0": "0", "-0": "0", "1": "1", "-1": "-1", "1.0": "1", "2.50": "2.5", "1E-7": "1e-7", "1e-6": "0.000001",
    "0.000001": "0.000001", "0.0000010": "0.000001", "1e20": "100000000000000000000", "1e21": "1e+21",
    "1e-5": "0.00001", "123e-20": "1.23e-18", "1.5e300": "1.5e+300", "5e-324": "5e-324",
    "1.7976931348623157e308": "1.7976931348623157e+308", "0.1": "0.1",
    "0.30000000000000004": "0.30000000000000004", "-1.5": "-1.5",
    "123456789012345680000": "123456789012345680000", "9007199254740993": "9007199254740992",
    "1e16": "10000000000000000", "1e15": "1000000000000000", "100000000000000": "100000000000000",
    "12345.6789": "12345.6789", "4.35": "4.35", "0.5e-6": "5e-7",
    "1234567890123456789012": "1.2345678901234568e+21", "-0.0000001": "-1e-7", "2.5e-7": "2.5e-7",
    "33.333333333333336": "33.333333333333336", "1e-323": "1e-323", "4.9406564584124654e-324": "5e-324",
    "6.02214076e23": "6.02214076e+23",
}


@pytest.mark.parametrize("literal", list(ES_NUMBERS))
def test_numbers_are_written_as_ecmascript_writes_them(literal):
    assert canonicalize(json.loads(literal)) == ES_NUMBERS[literal]


def test_strings_are_escaped_as_json_stringify_escapes_them():
    s = 'q"b\\n\nt\tb\bf\fr\r\x00\x1f\x7f  தமிழ் ₹ 😀'
    assert canonicalize(s) == '"q\\"b\\\\n\\nt\\tb\\bf\\fr\\r\\u0000\\u001f\x7f  தமிழ் ₹ 😀"'


def test_keys_sort_by_utf16_code_units_at_every_level():
    # JavaScript sorts "😀" (D83D DE00) before U+FFFF; Python's own order would not.
    assert canonicalize({"￿": 1, "😀": 2, "a": 3, "é": {"z": [{"b": 1, "a": 2}], "y": None}}) == \
        '{"a":3,"é":{"y":null,"z":[{"a":2,"b":1}]},"😀":2,"￿":1}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "\ud800", {"\udc00": 1},
                                   (1, 2), {1, 2}, object(), b"bytes", 10 ** 400, {1: "int key"}])
def test_no_canonical_form_for_what_is_not_json(value):
    with pytest.raises(CanonicalError):
        canonicalize({"x": value})


def test_the_parser_reads_json_as_json_parse_does():
    for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', "﻿{}"):
        with pytest.raises(ValueError):
            cs.parse_json(text)
    assert verify_command('{"commandId":"Xk3vQ9pL2mN8rT5wY7zA","v":NaN}', TOKEN, ReplayState()).detail == "not JSON"
    assert verify_command(b"\xff\xfe", TOKEN, ReplayState()).detail == "not JSON"


def test_a_message_nested_past_pythons_limit_is_refused_not_a_crash():
    deep = '{"commandId":"Deep0000000000000001","sig":"' + "A" * 43 + '","sigAlg":"hs256-v1","sigTs":1,"x":' + "[" * 100000 + "]" * 100000 + "}"
    assert verify_command(deep, TOKEN, ReplayState()).reason == "bad_signature"
    nested: list = []
    for _ in range(5000):
        nested = [nested]
    msg = {"commandId": "Deep0000000000000002", "sig": "A" * 43, "sigAlg": "hs256-v1", "sigTs": 1, "x": nested}
    verdict = verify_command(msg, TOKEN, ReplayState())
    assert verdict.reason == "bad_signature" and verdict.command_id == "Deep0000000000000002"


def test_the_signer_refuses_what_the_backend_never_publishes():
    with pytest.raises(ValueError):
        sign_command({"commandId": "NoToken0000000000001", "type": "setPower"}, "", 1)
    with pytest.raises(ValueError):
        sign_command({"type": "setPower"}, TOKEN, 1)
    with pytest.raises(ValueError):
        sign_command({"commandId": "a/b", "type": "setPower"}, TOKEN, 1)
    with pytest.raises(ValueError):
        sign_command({"commandId": "Bad0000000000000ts01", "type": "setPower"}, TOKEN, 1.5)


def test_a_sigts_written_as_a_float_or_beyond_a_safe_integer_is_judged_as_javascript_would():
    signed, _ = sign_command({"commandId": "Ft0000000000000000ts", "type": "setPower", "value": 1}, TOKEN,
                             1792042200000)
    as_float = json.dumps(signed).replace('"sigTs": 1792042200000', '"sigTs": 1792042200000.0')
    assert verify_command(as_float, TOKEN, ReplayState(), clock_trusted=True, now_ms=1792042200000).accepted
    too_big = json.dumps({**signed, "sigTs": 2 ** 53})
    assert verify_command(too_big, TOKEN, ReplayState()).reason == "bad_signature"
    assert verify_command(json.dumps({**signed, "sigTs": True}), TOKEN, ReplayState()).reason == "bad_signature"


def test_a_command_id_is_measured_in_utf16_code_units():
    assert cs._command_id_ok("x" * 128) and not cs._command_id_ok("x" * 129)
    assert cs._command_id_ok("😀" * 64) and not cs._command_id_ok("😀" * 64 + "x")
    assert not cs._command_id_ok("") and not cs._command_id_ok("a/b") and not cs._command_id_ok(7)


NODE = shutil.which("node")
REFERENCE = os.path.join(_contracts.CONTRACTS, "nodes", "reference", "signing.mjs")


@pytest.mark.skipif(NODE is None or not os.path.exists(REFERENCE), reason="node or the reference signing.mjs not found")
def test_the_python_canonical_form_and_mac_agree_with_the_reference_on_random_messages(tmp_path):
    """Beyond the vectors: random nested messages (numbers of every size, strings with escapes and
    astral characters, keys in every order) canonicalised and MACed by the reference in Node."""
    import random
    rnd = random.Random(20261015)
    alphabet = ['a', 'Z', '0', '"', '\\', '\n', '\x01', ' ', 'த', '₹', '😀', '￿', ' ', '/']

    def rstr():
        return "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 6)))

    def rnum():
        return rnd.choice([rnd.randint(-10 ** 6, 10 ** 6), rnd.uniform(-1e6, 1e6), rnd.uniform(-1, 1) * 10 ** rnd.randint(-30, 30),
                           rnd.randint(0, 2 ** 60), 0.0, -0.0, 1e21, 1e-7])

    def rval(depth=0):
        k = rnd.randint(0, 6 if depth < 3 else 3)
        if k == 0:
            return rnum()
        if k == 1:
            return rstr()
        if k == 2:
            return rnd.choice([True, False, None])
        if k == 3:
            return rnum()
        if k == 4:
            return [rval(depth + 1) for _ in range(rnd.randint(0, 3))]
        return {rstr(): rval(depth + 1) for _ in range(rnd.randint(0, 4))}

    cases = [{"commandId": f"Rand{i:016d}", "type": "setPortValue", "x": rval()} for i in range(300)]
    wires = [json.dumps(c, ensure_ascii=False) for c in cases]  # Python's own number text: the node parses it
    (tmp_path / "in.json").write_text(json.dumps(wires), encoding="utf-8")
    script = tmp_path / "run.mjs"
    script.write_text(
        f"import {{ canonicalize, macOf }} from {json.dumps('file://' + REFERENCE)};\n"
        "import { readFileSync } from 'node:fs';\n"
        f"const wires = JSON.parse(readFileSync({json.dumps(str(tmp_path / 'in.json'))}, 'utf8'));\n"
        f"const out = wires.map((w) => {{ const c = canonicalize(JSON.parse(w)); return [c, macOf(c, {json.dumps(TOKEN)})]; }});\n"
        "process.stdout.write(JSON.stringify(out));\n", encoding="utf-8")
    run = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    theirs = json.loads(run.stdout)
    for wire, (canonical, mac) in zip(wires, theirs):
        mine = canonicalize(cs.parse_json(wire))
        assert mine == canonical, wire
        assert cs.mac_of(mine, TOKEN) == mac


# ======================================================================= §5.3 the persisted replay set

def _signed(cid="Xk3vQ9pL2mN8rT5wY7zA", ts=1792042200000, token=TOKEN, **fields):
    message = {"commandId": cid, "type": "setPower", "assetId": "asset_pump1234", "sourceKey": "device.power",
               "value": 1, "timestamp": ts, **fields}
    return json.dumps(sign_command(message, token, ts)[0])


def _at(ts_ms):
    """A trusted now (the backend's time, or an NTP clock) at ts_ms."""
    return dict(clock_trusted=True, now_ms=ts_ms)


NOW = _at(1792042200000)  # _signed()'s default sigTs


def test_an_accepted_id_is_still_refused_after_a_restart(tmp_path):
    d = str(tmp_path / "state")
    assert verify_command(_signed(), TOKEN, ReplayStore(d), **NOW).accepted
    after_restart = ReplayStore(d)
    assert verify_command(_signed(), TOKEN, after_restart, **NOW).reason == "replayed"
    assert verify_command(_signed(), TOKEN, after_restart, clock_trusted=False).reason == "replayed"  # whatever the time
    assert after_restart.high_water == 1792042200000


def test_the_floor_is_kept_across_a_restart_on_the_backends_clock(tmp_path):
    d = str(tmp_path / "state")
    day = 24 * 3600 * 1000
    t = 1792042200000
    assert verify_command(_signed("NewOne00000000000001", t), TOKEN, ReplayStore(d), **_at(t)).accepted
    store = ReplayStore(d)
    # Even with a "now" that would pass the 10-minute window (a clock that is days behind), 8 days
    # below the high-water mark — kept on the card — is stale: the floor is on the backend's clock.
    old = verify_command(_signed("OldOne00000000000001", t - 8 * day), TOKEN, store, **_at(t - 8 * day))
    assert old.reason == "stale" and old.detail == "older than the replay window" and not old.window
    assert verify_command(_signed("OldOne00000000000002", t - 6 * day), TOKEN, store, **_at(t - 6 * day)).accepted


def test_ids_more_than_seven_days_below_the_high_water_mark_leave_the_set(tmp_path):
    d = str(tmp_path / "state")
    day = 24 * 3600 * 1000
    t = 1792042200000
    store = ReplayStore(d)
    assert verify_command(_signed("First000000000000001", t), TOKEN, store, **_at(t)).accepted
    assert verify_command(_signed("Later000000000000001", t + 8 * day), TOKEN, store, **_at(t + 8 * day)).accepted
    assert not store.has("First000000000000001")
    assert not ReplayStore(d).has("First000000000000001")
    # Gone from the set, but below the floor: never carried out again (whatever "now" says).
    assert verify_command(_signed("First000000000000001", t), TOKEN, ReplayStore(d), **_at(t)).reason == "stale"


def test_the_id_is_on_the_card_before_the_command_is_carried_out(tmp_path):
    d = str(tmp_path / "state")
    seen_on_disk = []
    client = _client()
    client.set_command_gate(CommandGate(TOKEN, ReplayStore(d), ack=client.publish_command_ack,
                                        trusted_now=lambda: 1792042200000))
    client.set_command_handler(lambda c: seen_on_disk.append(ReplayStore(d).has(c["commandId"])))
    client._on_message(None, None, _Msg(_signed()))
    assert seen_on_disk == [True]


def test_a_torn_last_journal_line_is_ignored(tmp_path):
    d = tmp_path / "state"
    store = ReplayStore(str(d))
    assert verify_command(_signed("Kept0000000000000001"), TOKEN, store, **NOW).accepted
    with open(d / cs.REPLAY_JOURNAL_FILE, "a", encoding="utf-8") as f:
        f.write('{"id": "Torn00000000000')  # a power cut during the append: that command never ran
    again = ReplayStore(str(d))
    assert again.has("Kept0000000000000001") and not again.has("Torn0000000000000001")
    assert verify_command(_signed("Torn0000000000000001"), TOKEN, again, **NOW).accepted


def test_a_damaged_snapshot_is_kept_aside_and_the_set_starts_empty(tmp_path, caplog):
    d = tmp_path / "state"
    d.mkdir()
    (d / cs.REPLAY_SNAPSHOT_FILE).write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="agal_one_agent.command_signing"):
        store = ReplayStore(str(d))
    assert store.seen == {} and store.high_water == 0
    assert (d / (cs.REPLAY_SNAPSHOT_FILE + ".corrupt")).exists()
    assert any("unreadable" in r.getMessage() for r in caplog.records)
    assert verify_command(_signed(), TOKEN, store, **NOW).accepted
    # v1.14.2 review: with the set gone, the time check is what keeps an old capture out — it is
    # never skipped (no trusted now: stale; an hour-old command: stale).
    assert verify_command(_signed("OldCapture0000000001", 1792042200000 - 3600_000), TOKEN, store, **NOW).reason == "stale"
    assert verify_command(_signed("NoTime00000000000001"), TOKEN, store, clock_trusted=False).reason == "stale"


def test_the_journal_is_folded_into_the_snapshot(tmp_path):
    d = tmp_path / "state"
    store = ReplayStore(str(d), compact_every=3)
    ids = [f"Fold{i:016d}" for i in range(7)]
    for i, cid in enumerate(ids):
        assert verify_command(_signed(cid, 1792042200000 + i), TOKEN, store, **NOW).accepted
    lines = (d / cs.REPLAY_JOURNAL_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # 7 = 3 + 3 folded, 1 in the journal
    snap = json.loads((d / cs.REPLAY_SNAPSHOT_FILE).read_text(encoding="utf-8"))
    assert [cid for cid, _ in snap["seen"]] == ids[:6]
    again = ReplayStore(str(d))
    assert all(again.has(cid) for cid in ids) and list(again.seen) == ids
    assert (d / cs.REPLAY_JOURNAL_FILE).read_text(encoding="utf-8") == ""  # folded at start


def test_a_card_that_cannot_be_written_still_carries_the_command_out(tmp_path, caplog):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    store = ReplayStore(str(blocker / "state"))
    with caplog.at_level(logging.ERROR, logger="agal_one_agent.command_signing"):
        verdict = verify_command(_signed(), TOKEN, store, **NOW)
    assert verdict.accepted and store.write_failures == 1
    assert any("could not be written" in r.getMessage() for r in caplog.records)
    assert verify_command(_signed(), TOKEN, store, **NOW).reason == "replayed"  # still in memory


# ======================================================================= the MQTT boundary

class _Msg:
    def __init__(self, payload):
        self.payload = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.topic = "agal/node-1/commands"


def _client(connected=True) -> AgalOneMqttClient:
    client = AgalOneMqttClient(MqttConfig(broker="broker.example", username="shared-device-login",
                                          status_topic="agal/node-1/status", commands_topic="agal/node-1/commands"))
    client._client = MagicMock()
    client._connected = connected
    return client


class _Rig:
    """An MQTT client with a gate, a handler that records what it was given, and an ack sink."""

    def __init__(self, tmp_path, token=TOKEN, clock_ok=True, now_ms=1792042205000, firmware=None, monotonic=None):
        self.client = _client()
        self.carried_out: list[dict] = []
        self.acks: list[tuple] = []
        self.firmware: list[dict] = []
        self.client.set_ack_sink(lambda *a, **kw: self.acks.append((*a, kw.get("reason"))))
        self.now = now_ms
        self.gate = CommandGate(token, ReplayStore(str(tmp_path / "state")) if token else ReplayState(),
                                ack=self.client.publish_command_ack,
                                firmware_report=firmware or (lambda **kw: self.firmware.append(kw)),
                                clock_ok=lambda: clock_ok, now_ms=lambda: self.now,
                                monotonic=monotonic or time.monotonic, firmware_report_async=False)
        self.client.set_command_gate(self.gate)
        self.client.set_command_handler(self.carried_out.append)

    def send(self, payload):
        self.client._on_message(None, None, _Msg(payload))

    def mqtt_acks(self):
        out = []
        for call in self.client._client.publish.call_args_list:
            body = json.loads(call.args[1])
            if body.get("type") == "commandAck":
                out.append(body["payload"])
        return out


def test_a_signed_command_is_carried_out_and_the_handler_gets_the_object_that_was_verified(tmp_path, monkeypatch):
    rig = _Rig(tmp_path)
    verified = []
    real = cs.verify_command

    def spy(*a, **kw):
        verdict = real(*a, **kw)
        verified.append(verdict.command)
        return verdict

    monkeypatch.setattr(cs, "verify_command", spy)
    rig.send(_signed())
    assert len(rig.carried_out) == 1 and rig.carried_out[0] is verified[0]
    assert rig.carried_out[0]["value"] == 1 and rig.acks == []


def test_an_unsigned_command_is_refused_and_only_logged(tmp_path, caplog):
    """v1.14.2 review: an unsigned refusal is never posted — the backend moves no command for it, and
    answering let anyone with the shared login spend the node's answers with made-up ids."""
    rig = _Rig(tmp_path)
    unsigned = json.dumps({"commandId": "Xk3vQ9pL2mN8rT5wY7zA", "type": "setPower", "assetId": "asset_pump1234",
                           "sourceKey": "device.power", "value": 1, "timestamp": 1792042200000})
    with caplog.at_level(logging.WARNING, logger="agal_one_agent.command_signing"):
        rig.send(unsigned)
    assert rig.carried_out == []
    assert rig.acks == [] and rig.mqtt_acks() == []
    assert rig.gate.refused["unsigned"] == 1 and rig.gate.refusal_acks_sent == 0
    assert [r.getMessage() for r in caplog.records if "command refused" in r.getMessage()] == [
        "command refused: unsigned — commandId Xk3vQ9pL2mN8rT5wY7zA — no sig (not carried out)"]


@pytest.mark.parametrize("forge, detail", [
    (lambda m: {**m, "value": 0}, "the signature does not match"),            # edited after signing
    (lambda m: {**m, "assetId": "asset_other"}, "the signature does not match"),  # sent to another card
    (lambda m: {**m, "sig": m["sig"][:-1]}, "sig is not 43 base64url characters"),
    (lambda m: {**m, "sigAlg": "none"}, "sigAlg is not hs256-v1"),
], ids=["edited", "another card", "short sig", "another alg"])
def test_a_forged_command_is_refused_bad_signature(tmp_path, forge, detail):
    rig = _Rig(tmp_path)
    rig.send(json.dumps(forge(json.loads(_signed()))))
    assert rig.carried_out == []
    assert rig.acks == [("Xk3vQ9pL2mN8rT5wY7zA", "failed", None, f"refused: bad_signature — {detail}", "bad_signature")]


def test_a_command_signed_for_another_node_is_refused(tmp_path):
    rig = _Rig(tmp_path)
    rig.send(_signed(token=OTHER))
    assert rig.carried_out == [] and rig.acks[0][4] == "bad_signature"


def test_a_replayed_command_is_carried_out_once_and_the_replay_is_only_logged(tmp_path, caplog):
    rig = _Rig(tmp_path)
    rig.send(_signed())
    with caplog.at_level(logging.WARNING, logger="agal_one_agent.command_signing"):
        rig.send(_signed())
    assert len(rig.carried_out) == 1
    assert rig.acks == [] and rig.gate.refused["replayed"] == 1  # the original has its own acks
    assert any("command refused: replayed setPower" in r.getMessage() for r in caplog.records)


def test_a_forged_copy_first_does_not_stop_the_real_command(tmp_path):
    rig = _Rig(tmp_path)
    rig.send(json.dumps({**json.loads(_signed()), "value": 0}))
    rig.send(_signed())
    assert [c["value"] for c in rig.carried_out] == [1]


@pytest.mark.parametrize("clock_ok, now_offset_ms, expect", [
    (True, 11 * 60 * 1000, "stale"),      # trusted clock: signed 11 minutes ago
    (True, -3 * 60 * 1000, "stale"),      # trusted clock: signed 3 minutes in the future
    (True, 9 * 60 * 1000, "accepted"),
    # v1.14.2 review: no trusted now (no backend answer yet, no NTP / RTC) is stale — never skipped.
    (False, 3 * 24 * 3600 * 1000, "stale"),  # a capture 3 days old, at the next boot: not carried out
    (False, 5000, "stale"),                  # even a fresh one, until the backend has answered once
], ids=["11 min old", "3 min ahead", "9 min old", "no trusted now, 3 days", "no trusted now, fresh"])
def test_freshness_is_always_checked_against_a_trusted_now(tmp_path, clock_ok, now_offset_ms, expect):
    rig = _Rig(tmp_path, clock_ok=clock_ok, now_ms=1792042200000 + now_offset_ms)
    rig.send(_signed())
    got = "accepted" if rig.carried_out else rig.acks[0][4]
    assert got == expect
    if expect == "stale" and not clock_ok:
        assert rig.acks[0][3] == "refused: stale — no trusted time yet"


def test_the_gate_asks_the_agents_clock_by_default():
    gate = CommandGate(TOKEN, ReplayState())
    assert isinstance(gate.clock_ok.__self__, SystemClock)


@pytest.mark.parametrize("command_id", ["sync_program_3_aB3dE6gH", "sync_ports_7_Zz9Yy8Xx", "scan_bus_i2c-1_Qq1Ww2Ee",
                                        "test_port_p4_Rr5Tt6Yy", "sync_1792042200000_Uu7Ii8Oo", "not-an-auto-id"])
def test_a_refused_id_the_backend_keeps_no_document_for_is_only_logged(tmp_path, command_id, caplog):
    rig = _Rig(tmp_path)
    with caplog.at_level(logging.WARNING, logger="agal_one_agent.command_signing"):
        rig.send(json.dumps({"commandId": command_id, "type": "syncProgram", "version": 3, "timestamp": 1}))
    assert rig.carried_out == [] and rig.acks == [] and rig.mqtt_acks() == []
    assert any(command_id in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("raw", [b"setPower 1", b"\xff\xfe\x00", b"[1, 2]", b"42", b'"text"', b"{}"],
                         ids=["not JSON", "not UTF-8", "a list", "a number", "a string", "empty object"])
def test_a_message_that_is_not_a_command_is_dropped_and_logged(tmp_path, raw, caplog):
    rig = _Rig(tmp_path)
    with caplog.at_level(logging.WARNING, logger="agal_one_agent.command_signing"):
        rig.send(raw)
    assert rig.carried_out == [] and rig.acks == []
    assert any("command refused" in r.getMessage() for r in caplog.records)


def _forged(cid):
    return json.dumps({"commandId": cid, "type": "setPower", "value": 1, "timestamp": 1,
                       "sigAlg": "hs256-v1", "sigTs": 1792042200000, "sig": "A" * 43})


def test_every_refusal_is_logged_and_answers_are_budgeted_per_reason(tmp_path, caplog):
    """At most 5 bad_signature and 20 stale answers in any 10 minutes, counted apart: a flood of forged
    messages (and of unsigned junk, never answered) cannot crowd out a real command's stale answer."""
    clock = [1000.0]
    rig = _Rig(tmp_path, monotonic=lambda: clock[0], now_ms=1792042200000 + 11 * 60 * 1000)
    with caplog.at_level(logging.WARNING, logger="agal_one_agent.command_signing"):
        for i in range(30):
            rig.send(json.dumps({"commandId": f"Junk{i:016d}", "type": "setPower", "value": 1, "timestamp": 1}))
        for i in range(8):
            rig.send(_forged(f"Forged{i:014d}"))
    assert [a[4] for a in rig.acks] == ["bad_signature"] * 5 and rig.gate.refusal_acks_withheld == 3
    assert len([r for r in caplog.records if r.getMessage().startswith("command refused: unsigned")]) == 30
    # A real command that arrives 11 minutes late during the flood is still answered (its own budget).
    for i in range(22):
        rig.send(_signed(f"Late{i:016d}"))
    stale = [a for a in rig.acks if a[4] == "stale"]
    assert len(stale) == 20 and stale[0][3] == "refused: stale — signed more than 10 minutes ago"
    clock[0] += 599.0
    rig.send(_forged("ForgedLate0000000001"))
    assert [a[4] for a in rig.acks].count("bad_signature") == 5
    clock[0] += 1.0  # the first answers are 10 minutes old now
    rig.send(_forged("ForgedLate0000000002"))
    assert rig.acks[-1][0] == "ForgedLate0000000002" and rig.acks[-1][4] == "bad_signature"


def _firmware(cid="Nc8Vb6Xm4Zl2Kj0Hg8Fd", ts=1792042200000, token=TOKEN):
    message = {"commandId": cid, "type": "firmwareUpdate", "version": "0.3.4", "rolloutId": "ro_1",
               "source": "https://github.com/logapradeep/agal-one-agent/archive/refs/tags/v0.3.4.tar.gz",
               "sha256": "0" * 64, "timestamp": ts}
    return json.dumps(sign_command(message, token, ts)[0])


def test_a_stale_firmware_update_is_also_reported_as_a_failed_update(tmp_path):
    rig = _Rig(tmp_path, now_ms=1792042200000 + 11 * 60 * 1000)
    rig.send(_firmware())
    assert rig.carried_out == []
    error = "refused: stale — signed more than 10 minutes ago"
    assert rig.firmware == [{"command_id": "Nc8Vb6Xm4Zl2Kj0Hg8Fd", "to_version": "0.3.4", "rollout_id": "ro_1",
                             "error": error}]
    # Its ack names a /nodeCommands doc, which commandAck does not track: MQTT only, as every
    # firmwareUpdate ack (main.handle_firmware_update).
    assert rig.acks == []
    assert rig.mqtt_acks() == [{"nodeUid": "shared-device-login", "commandId": "Nc8Vb6Xm4Zl2Kj0Hg8Fd",
                                "status": "failed", "error": error, "reason": "stale"}]
    rig.send(_firmware())  # the same stale copy again: reported once
    assert len(rig.firmware) == 1


def test_a_firmware_update_refused_with_no_trusted_now_is_reported(tmp_path):
    rig = _Rig(tmp_path, clock_ok=False)
    rig.send(_firmware())
    assert rig.carried_out == []
    assert rig.firmware == [{"command_id": "Nc8Vb6Xm4Zl2Kj0Hg8Fd", "to_version": "0.3.4", "rollout_id": "ro_1",
                             "error": "refused: stale — no trusted time yet"}]


def test_a_firmware_update_below_the_replay_floor_is_not_reported(tmp_path):
    """v1.14.2 review: a captured firmwareUpdate signed more than 7 days before a command the node already
    carried out is a replay — refused stale by the floor, but never reported as this node's update failing
    (it would overwrite the node's otaStatus and a finished rollout's record)."""
    day = 24 * 3600 * 1000
    t = 1792042200000
    rig = _Rig(tmp_path, now_ms=t)
    rig.send(_signed("Recent00000000000001", t))
    assert len(rig.carried_out) == 1
    rig.now = t - 8 * day + 1000  # even a clock that would pass the window
    rig.send(_firmware("OldFw000000000000001", ts=t - 8 * day))
    assert rig.carried_out[1:] == [] and rig.firmware == []
    assert rig.gate.refused["stale"] == 1


def test_a_forged_firmware_update_is_not_reported_as_an_update(tmp_path):
    rig = _Rig(tmp_path)
    rig.send(json.dumps({**json.loads(_firmware()), "version": "0.0.1"}))
    assert rig.carried_out == [] and rig.firmware == []
    # Its type could be anyone's: answered as any refused tracked id.
    assert rig.acks == [("Nc8Vb6Xm4Zl2Kj0Hg8Fd", "failed", None, "refused: bad_signature — the signature does not match",
                         "bad_signature")]


def test_the_firmware_report_goes_through_the_http_reporter(monkeypatch, tmp_path):
    seen = []
    rep = HttpReporter("node-1", auth_token=TOKEN, base_url="https://ingress.example/telemetryIngress")
    monkeypatch.setattr(rep, "_post", lambda data: seen.append(data) or True)
    client = _client()
    gate = wire_command_gate(client, rep, token=TOKEN, state_dir=str(tmp_path / "state"), offline=False,
                             clock_ok=lambda: True)
    gate.now_ms = lambda: 1792042200000 + 11 * 60 * 1000
    gate.firmware_report_async = False
    client._on_message(None, None, _Msg(_firmware()))
    assert len(seen) == 1 and seen[0]["type"] == "firmware_update"
    p = seen[0]["payload"]
    assert {k: p[k] for k in ("nodeUid", "phase", "commandId", "toVersion", "rolloutId", "error")} == {
        "nodeUid": "node-1", "phase": "failed", "commandId": "Nc8Vb6Xm4Zl2Kj0Hg8Fd", "toVersion": "0.3.4",
        "rolloutId": "ro_1", "error": "refused: stale — signed more than 10 minutes ago"}


def test_a_node_without_a_token_carries_out_nothing_and_says_so_every_minute(tmp_path, caplog):
    """v1.14.2 review (§5.5): a node that can check nothing carries out nothing — the backend never
    publishes to a node it holds no token for, so whatever arrives is someone else's."""
    clock = [5000.0]
    with caplog.at_level(logging.ERROR, logger="agal_one_agent.command_signing"):
        rig = _Rig(tmp_path, token="", monotonic=lambda: clock[0])
    unsigned = json.dumps({"commandId": "Xk3vQ9pL2mN8rT5wY7zA", "type": "setPower", "value": 1, "timestamp": 1})
    with caplog.at_level(logging.WARNING, logger="agal_one_agent.command_signing"):
        rig.send(unsigned)
        rig.send(_signed())  # signed with a token the node does not hold: refused too
        clock[0] += 61.0
        rig.send(unsigned)
    assert rig.carried_out == [] and rig.acks == [] and rig.mqtt_acks() == []
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 2  # at start, then once a minute while messages keep arriving
    assert "carries out NOTHING" in errors[0] and "join this node again" in errors[0]
    assert "no token: join this node again" in errors[1]
    assert rig.gate.refused["bad_signature"] == 3 and not rig.gate.protected


def test_the_gate_prefers_the_backends_time_to_a_wrong_wall_clock(tmp_path):
    """A Pi 5 with /dev/rtc0 but no battery, after a power cut: clock_ok() says trusted, the wall clock
    is 3 days behind. Once the backend has answered, its time decides: a fresh command is carried out,
    a 3-day-old capture is not."""
    from agal_one_agent.server_time import ServerTime, trusted_now
    mono = [100.0]
    st = ServerTime(monotonic=lambda: mono[0])
    t = 1792042200000
    wall = [t - 3 * 24 * 3600 * 1000]
    gate = CommandGate(TOKEN, ReplayStore(str(tmp_path / "state")), ack=lambda *a, **k: None,
                       trusted_now=trusted_now(st, lambda: True, wall_ms=lambda: wall[0]))
    # Before any answer: the (wrong) wall clock — the fresh command looks 3 days ahead: stale.
    assert gate.admit(_signed("Fresh000000000000001", t)) is None
    assert st.observe({"Date": "Thu, 15 Oct 2026 05:30:00 GMT"}, "https://ingress.example/telemetryIngress")
    mono[0] += 4.0
    assert gate.admit(_signed("Fresh000000000000001", t)) is not None  # the refused copy burnt nothing
    assert gate.admit(_signed("Capture0000000000001", t - 3 * 24 * 3600 * 1000)) is None
    assert gate.refused["stale"] == 2


def test_without_a_gate_the_client_logs_that_commands_are_unchecked(caplog):
    client = _client()
    got = []
    client.set_command_handler(got.append)
    with caplog.at_level(logging.ERROR, logger="agal_one_agent.mqtt_client"):
        client._on_message(None, None, _Msg('{"type": "setPower"}'))
        client._on_message(None, None, _Msg('{"type": "setPower"}'))
    assert len(got) == 2
    assert sum("UNCHECKED" in r.getMessage() for r in caplog.records) == 1


# ======================================================================= the refusal ack over HTTPS

class _Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_refusal_ack_reaches_the_ingress_with_its_reason_and_the_node_token(monkeypatch, tmp_path):
    bodies = []

    def urlopen(req, timeout):
        bodies.append((req.get_header("Authorization"), json.loads(req.data)))
        return _Response(200)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    rep = HttpReporter("node-1", auth_token=TOKEN, base_url="https://ingress.example/telemetryIngress")
    client = _client(connected=False)  # MQTT down: the answer does not depend on it
    acks = CommandAcks(rep, coalesce_s=0.0)
    client.set_ack_sink(acks.submit)
    wire_command_gate(client, rep, token=TOKEN, state_dir=str(tmp_path / "state"), offline=False,
                      clock_ok=lambda: False)
    client._on_message(None, None, _Msg(json.dumps({**json.loads(_signed()), "value": 0})))
    assert acks.wait_idle(5)
    acks.close()
    assert bodies == [(f"Bearer {TOKEN}", {"type": "commandAck", "payload": {
        "nodeUid": "node-1", "commandId": "Xk3vQ9pL2mN8rT5wY7zA", "status": "failed",
        "error": "refused: bad_signature — the signature does not match", "reason": "bad_signature"}})]


def test_a_refusal_does_not_supersede_the_real_commands_own_acknowledged():
    posted = []

    class Rep:
        def report_command_ack(self, command_id, status, applied_value=None, error=None, reason=None):
            posted.append((command_id, status, reason))
            return 200

    acks = CommandAcks(Rep(), coalesce_s=0.2)
    acks.submit("Xk3vQ9pL2mN8rT5wY7zA", "acknowledged")
    acks.submit("Xk3vQ9pL2mN8rT5wY7zA", "failed", error="refused: replayed — …", reason="replayed")
    assert acks.wait_idle(5)
    acks.close()
    assert posted == [("Xk3vQ9pL2mN8rT5wY7zA", "acknowledged", None), ("Xk3vQ9pL2mN8rT5wY7zA", "failed", "replayed")]


def test_report_command_ack_sends_reason_only_with_failed(monkeypatch):
    rep = HttpReporter("node-1", auth_token="tok", base_url="https://ingress.example/telemetryIngress")
    sent = []
    monkeypatch.setattr(rep, "_post_status", lambda data: sent.append(data) or 200)
    rep.report_command_ack("Xk3vQ9pL2mN8rT5wY7zA", "failed", error="refused: stale — x", reason="stale")
    rep.report_command_ack("Xk3vQ9pL2mN8rT5wY7zA", "completed", reason="stale")
    assert sent[0]["payload"]["reason"] == "stale" and "reason" not in sent[1]["payload"]


# ======================================================================= the wiring in main()

def _node_config(tmp_path, token="tok"):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"node": {"uid": "node-1", "name": "Bench", "auth_token": token},
                               "mqtt": {"broker": "broker.example", "username": "shared-device-login"},
                               "blocks": {"state_dir": str(tmp_path / "state")},
                               "telemetry": {"ingress_url": "https://ingress.example/telemetryIngress"}}))
    return str(path)


class _WiredHere(Exception):
    pass


@pytest.mark.parametrize("offline", [False, True], ids=["cloud", "offline"])
def test_main_wires_the_command_gate_with_the_nodes_token_and_state_dir(tmp_path, monkeypatch, offline):
    seen = {}

    def wire(mqtt_client, reporter, *, token, state_dir, offline, clock_ok=None, server_time=None):
        seen.update(mqtt=mqtt_client, reporter=reporter, token=token, state_dir=state_dir, offline=offline,
                    clock_ok=clock_ok, ack_sink=mqtt_client._ack_sink, server_time=server_time)
        raise _WiredHere

    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(agent_main, "wire_command_gate", wire)
    monkeypatch.setattr("sys.argv", ["agal-one-agent", "--config", _node_config(tmp_path)]
                        + (["--offline"] if offline else []))
    with pytest.raises(_WiredHere):
        agent_main.main()
    assert isinstance(seen["mqtt"], AgalOneMqttClient) and isinstance(seen["reporter"], HttpReporter)
    assert seen["token"] == "tok" and seen["state_dir"] == str(tmp_path / "state")
    assert seen["offline"] is offline
    assert isinstance(seen["clock_ok"].__self__, SystemClock)
    from agal_one_agent.server_time import ServerTime
    # The backend's time: the same reference the reporter feeds from every HTTPS answer.
    assert isinstance(seen["server_time"], ServerTime) and seen["reporter"].server_time is seen["server_time"]
    assert (seen["ack_sink"] is not None) is (not offline)  # the refusal answers have their queue


def test_wire_command_gate_sets_the_gate_on_the_client_and_offline_wires_none(tmp_path):
    client = _client()
    gate = wire_command_gate(client, MagicMock(), token=TOKEN, state_dir=str(tmp_path), offline=False)
    assert client._gate is gate and isinstance(gate.state, ReplayStore) and gate.protected
    other = _client()
    assert wire_command_gate(other, MagicMock(), token=TOKEN, state_dir=str(tmp_path), offline=True) is None
    assert other._gate is None
