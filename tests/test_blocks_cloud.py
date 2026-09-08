"""CloudSink fan-out, ProgramSync pull/ack, heartbeat extras and the new wire
envelopes (programAck / variables / alert) — no network, fake transports."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.cloud import CloudSink, capabilities_from_pins, heartbeat_extra
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import BlockRuntime
from agal_one_agent.blocks.simulate import build_bundle_from_defaults
from agal_one_agent.blocks.sync import ProgramStore, ProgramSync
from agal_one_agent.config import MqttConfig
from agal_one_agent.mqtt_client import AgalOneMqttClient

HERE = os.path.dirname(__file__)
DEFAULTS = os.path.normpath(os.path.join(HERE, "..", "..", "..", "contracts", "programs", "defaults"))


class FakeMqtt:
    def __init__(self, connected=True):
        self.connected = connected
        self.calls: list[tuple] = []

    def publish_variables(self, asset_id, values):
        self.calls.append(("variables", asset_id, values))
        return self.connected

    def publish_alert(self, text, severity, rule_id=None, asset_id=None):
        self.calls.append(("alert", text, severity, rule_id, asset_id))
        return self.connected

    def publish_event(self, event):
        self.calls.append(("event", event))

    def publish_telemetry(self, readings):
        self.calls.append(("telemetry", readings))

    def publish_program_ack(self, version, status, reason=None, firmware_version=None):
        self.calls.append(("programAck", version, status, reason))
        return self.connected


class FakeHttp:
    def __init__(self, program=None):
        self.calls: list[tuple] = []
        self.program = program

    def report_variables(self, asset_id, values):
        self.calls.append(("variables", asset_id, values))
        return True

    def report_alert(self, text, severity, rule_id=None, asset_id=None):
        self.calls.append(("alert", text, severity))
        return True

    def report_program_ack(self, version, status, reason=None, firmware_version=None):
        self.calls.append(("programAck", version, status, reason))
        return True

    def fetch_program(self, current_version=None):
        self.calls.append(("getProgram", current_version))
        return self.program


def test_cloud_sink_fans_out():
    mqtt, http = FakeMqtt(), FakeHttp()
    sink = CloudSink(mqtt, http, telemetry_buffer=None, firmware_version="0.2.0", http_async=False)
    sink.variables("pump-1", {"current": 4.2})
    sink.alert("Pump stopped", "critical", "dry_run_alert", None)
    sink.event("run_missed", {"scheduleId": "s1"})
    sink.reading("pump-1", "sensor.current", 4.2, "agal:reading/current/v1", "A")
    sink.program_ack(3, "applied")
    kinds = [c[0] for c in mqtt.calls]
    assert kinds == ["variables", "alert", "event", "telemetry", "programAck"]
    assert ("alert", "Pump stopped", "critical") in http.calls
    assert ("programAck", 3, "applied", None) in http.calls
    assert http.calls[0] == ("variables", "pump-1", {"current": 4.2})  # first HTTP snapshot
    assert sink.last_ack == {"version": 3, "status": "applied", "reason": None}


def test_cloud_sink_uses_http_when_mqtt_down():
    mqtt, http = FakeMqtt(connected=False), FakeHttp()
    sink = CloudSink(mqtt, http, http_async=False)
    sink.variables("pump-1", {"current": 4.2})
    sink.variables("pump-1", {"current": 4.3})
    assert [c for c in http.calls if c[0] == "variables"] == [("variables", "pump-1", {"current": 4.2}), ("variables", "pump-1", {"current": 4.3})]


def _bundle(version=1):
    b = build_bundle_from_defaults(DEFAULTS, "farm")
    b["version"] = version
    return b


def test_program_store_roundtrip(tmp_path):
    store = ProgramStore(str(tmp_path))
    assert store.load() is None
    store.save(_bundle(2))
    assert store.load()["version"] == 2


def test_program_sync_applies_new_version(tmp_path):
    io, clock = SimulatedIO(), SimClock()
    from agal_one_agent.blocks.runtime import RecordingSink
    sink = RecordingSink()
    rt = BlockRuntime(io, sink, clock=clock, state_dir=str(tmp_path))
    http = FakeHttp(program={"version": 5, "bundle": _bundle(5)})
    sync = ProgramSync(rt, ProgramStore(str(tmp_path)), http)
    sync._pull(expected_version=5)
    assert rt.version == 5
    assert ProgramStore(str(tmp_path)).load()["version"] == 5
    rt.step()
    assert sink.acks[-1] == {"version": 5, "status": "applied", "reason": None}


def test_program_sync_unchanged_and_rejected(tmp_path):
    from agal_one_agent.blocks.runtime import RecordingSink
    rt = BlockRuntime(SimulatedIO(), RecordingSink(), clock=SimClock(), state_dir=str(tmp_path))
    rt.compile(_bundle(1))
    http = FakeHttp(program={"version": 1, "unchanged": True})
    sync = ProgramSync(rt, ProgramStore(str(tmp_path)), http)
    sync._pull()
    assert rt.version == 1
    bad = _bundle(2)
    bad["nab"]["variables"][0]["asset"]["assetId"] = "not-here"
    http.program = {"version": 2, "bundle": bad}
    sync._pull(expected_version=2)
    assert rt.version == 1, "previous program stays on rejection"
    assert any(c[0] == "programAck" and c[1] == 2 and c[2] == "rejected" for c in http.calls)


def test_persisted_program_loads_on_boot(tmp_path):
    from agal_one_agent.blocks.runtime import RecordingSink
    ProgramStore(str(tmp_path)).save(_bundle(7))
    rt = BlockRuntime(SimulatedIO(), RecordingSink(), clock=SimClock(), state_dir=str(tmp_path))
    sync = ProgramSync(rt, ProgramStore(str(tmp_path)), FakeHttp())
    assert sync.load_persisted() is True
    assert rt.version == 7


@dataclass
class Pin:
    physical_pin: int
    gpio_number: Optional[int] = None
    protocol: str = "gpio_input"
    label: str = ""
    sensor_type: Optional[str] = None


@dataclass
class Cfg:
    pins: list = field(default_factory=list)


def test_capabilities_from_pins():
    cfg = Cfg(pins=[
        Pin(11, 17, "gpio_output", "device.power"),
        Pin(13, 27, "gpio_output", "device.valve1"),
        Pin(15, 22, "gpio_output", "device.valve2"),
        Pin(29, 5, "gpio_input", "sensor.flow1"),
        Pin(31, 6, "gpio_input", "sensor.mains.r"),
        Pin(12, None, "analog_input", "sensor.current", "current_acs758"),
    ])
    caps = capabilities_from_pins(cfg)
    assert caps["relayOutputs"] == 3 and caps["valveOutputs"] == 2
    assert caps["flowInputs"] == 1 and caps["mainsSenseInputs"] == 1 and caps["currentInputs"] == 1
    assert "hasRtc" in caps


def test_heartbeat_extra_reports_program():
    from agal_one_agent.blocks.runtime import RecordingSink
    rt = BlockRuntime(SimulatedIO(), RecordingSink(), clock=SimClock())
    rt.compile(_bundle(4))
    sink = CloudSink(FakeMqtt(), FakeHttp())
    sink.program_ack(4, "applied")
    extra = heartbeat_extra(rt, Cfg(pins=[]), sink)
    assert extra["programVersion"] == 4 and extra["programStatus"] == "applied"
    assert extra["capabilities"]["relayOutputs"] == 0


def test_mqtt_envelopes_for_program_messages():
    client = AgalOneMqttClient(MqttConfig(broker="x", username="node-1", password="t",
                                          status_topic="agal/node-1/status"))
    published = []

    class C:
        def publish(self, topic, payload, qos=1):
            published.append((topic, json.loads(payload), qos))
    client._client = C()
    client._connected = True
    assert client.publish_program_ack(3, "applied", firmware_version="0.2.0")
    assert client.publish_variables("pump-1", {"current": 4.2})
    assert client.publish_alert("Dry run", "critical", "dry_run_alert", "pump-1")
    types = [p[1]["type"] for p in published]
    assert types == ["programAck", "variables", "alert"]
    for topic, body, qos in published:
        assert topic == "agal/node-1/status" and qos == 1 and body["payload"]["nodeUid"] == "node-1"
    assert published[0][1]["payload"] == {"version": 3, "status": "applied", "timestamp": published[0][1]["payload"]["timestamp"], "firmwareVersion": "0.2.0", "nodeUid": "node-1"}
    assert published[2][1]["payload"]["ruleId"] == "dry_run_alert"


def test_variables_uploader_posts_every_change_in_order_and_coalesces_bursts():
    import time as _t
    mqtt, http = FakeMqtt(), FakeHttp()
    sink = CloudSink(mqtt, http, min_interval=0.05)
    sink.variables("valve-1", {"coil": True, "open": True})
    sink.variables("valve-1", {"coil": False, "open": False})  # a burst: the latest snapshot wins
    assert sink.flush(3.0)
    posts = [c for c in http.calls if c[0] == "variables"]
    assert posts[-1] == ("variables", "valve-1", {"coil": False, "open": False})
    assert len(posts) == 1, "both MQTT-fast-path calls collapse into the one HTTPS snapshot"
    _t.sleep(0.06)
    sink.variables("valve-1", {"coil": True, "open": True})
    sink.variables("pump-1", {"relay": True})
    assert sink.flush(3.0)
    posts = [c for c in http.calls if c[0] == "variables"]
    assert posts[-2:] == [("variables", "valve-1", {"coil": True, "open": True}), ("variables", "pump-1", {"relay": True})]
    assert [c[0] for c in mqtt.calls].count("variables") == 4, "MQTT still gets every change"
    sink.close()


def test_variables_uploader_retries_a_failed_post():
    class FlakyHttp(FakeHttp):
        def __init__(self):
            super().__init__()
            self.fail_next = 1

        def report_variables(self, asset_id, values):
            if self.fail_next:
                self.fail_next -= 1
                return False
            return super().report_variables(asset_id, values)

    http = FlakyHttp()
    sink = CloudSink(FakeMqtt(), http, min_interval=0.01)
    sink.uploader.retry_after = 0.05
    sink.variables("flow-1", {"flow": True})
    assert sink.flush(3.0)
    assert ("variables", "flow-1", {"flow": True}) in http.calls
    assert sink.uploader.failed == 1 and sink.uploader.posted == 1
    sink.close()


def test_runtime_reports_full_snapshots_and_everything_after_load():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from agal_one_agent.blocks.clock import SimClock

    class SnapSink:
        def __init__(self):
            self.snapshots = []

        def variables(self, asset_id, values):
            self.snapshots.append((asset_id, dict(values)))

        def alert(self, *a, **k): pass

        def event(self, *a, **k): pass

        def log(self, *a, **k): pass

        def reading(self, *a, **k): pass

        def program_ack(self, *a, **k): pass

    sink = SnapSink()
    clock = SimClock(datetime(2026, 9, 7, 5, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
    rt = BlockRuntime(SimulatedIO(), sink, clock=clock)
    rt.compile(build_bundle_from_defaults(DEFAULTS, "farm"))
    rt.step()
    first = {aid for aid, _ in sink.snapshots}
    assert {"pump-1", "valve-1", "valve-2", "flow-1", "flow-2"} <= first, "every card reported after the program loaded"
    valve_first = next(v for aid, v in sink.snapshots if aid == "valve-1")
    assert {"coil", "open", "open_since"} <= set(valve_first), "a snapshot carries every reportable variable"
    sink.snapshots.clear()
    rt.apply_command({"type": "runPlot", "plotId": "plot-1", "commandId": "c1"})
    for _ in range(3):
        rt.step()
        clock.advance(1.0)
    valve_after = [v for aid, v in sink.snapshots if aid == "valve-1"]
    assert valve_after and valve_after[-1]["coil"] is True and "open" in valve_after[-1] and "open_since" in valve_after[-1]


def test_runtime_does_not_report_timer_ticks_or_small_jitter():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from agal_one_agent.blocks.clock import SimClock

    class SnapSink:
        def __init__(self):
            self.snapshots = []

        def variables(self, asset_id, values):
            self.snapshots.append((asset_id, dict(values)))

        def alert(self, *a, **k): pass

        def event(self, *a, **k): pass

        def log(self, *a, **k): pass

        def reading(self, *a, **k): pass

        def program_ack(self, *a, **k): pass

    sink = SnapSink()
    clock = SimClock(datetime(2026, 9, 7, 5, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
    io = SimulatedIO()
    rt = BlockRuntime(io, sink, clock=clock)
    rt.compile(build_bundle_from_defaults(DEFAULTS, "farm"))
    rt.step()
    sink.snapshots.clear()
    # Idle for 20 s: the valves' open_since timers tick but nothing is reported.
    for _ in range(20):
        clock.advance(1.0)
        rt.step()
    assert [aid for aid, _ in sink.snapshots if aid.startswith("valve")] == []
    # Pump current jitter of 2 % does not report; a 10 % change does.
    io.set_input("pump-1", "sensor.current", 4.50)
    clock.advance(1.0); rt.step()
    sink.snapshots.clear()
    io.set_input("pump-1", "sensor.current", 4.55)
    clock.advance(1.0); rt.step()
    assert [aid for aid, _ in sink.snapshots if aid == "pump-1"] == []
    io.set_input("pump-1", "sensor.current", 5.2)
    clock.advance(1.0); rt.step()
    assert any(aid == "pump-1" and v.get("current") == 5.2 for aid, v in sink.snapshots)
    # A bool change reports at once.
    sink.snapshots.clear()
    rt.apply_command({"type": "runPlot", "plotId": "plot-1", "commandId": "c1"})
    clock.advance(1.0); rt.step()
    assert any(aid == "valve-1" and v.get("coil") is True for aid, v in sink.snapshots)
    # Keep-alive: with only timers ticking, one report per asset within 30 s.
    sink.snapshots.clear()
    for _ in range(31):
        clock.advance(1.0)
        rt.step()
    valve_reports = [aid for aid, _ in sink.snapshots if aid == "valve-1"]
    assert 1 <= len(valve_reports) <= 2
