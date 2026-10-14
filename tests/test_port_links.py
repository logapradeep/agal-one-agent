"""RBAC-01 on the node: a switch command moves a port only for the card that port is linked to.

A card document names its node and ports itself, so a stranger's card can name somebody
else's node and pins. The node refuses setPower / setPortValue / testPort for a port that is
not linked to the command's card (ports/links.py), and the runtime no longer falls back to
another card's port when it does not know the card a command names. No hardware.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agal_one_agent import boot_reconciler
from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import BlockRuntime, CompileError, RecordingSink
from agal_one_agent.blocks.simulate import build_bundle_from_defaults
from agal_one_agent.ports.links import LinkGuard, handle_switch, refuse, refuse_port_test

import _contracts  # noqa: E402 — tests/ is on sys.path (pytest rootdir insertion)

DEFAULTS = _contracts.DEFAULTS
CHIP = "pinctrl-bcm2835"


def _farm(port_table: bool):
    """The default farm program; with ``port_table`` its ports are stamped the ADR-024 way
    (chip label + line, the way linkAssetToNode writes them from the node's port table)."""
    if not os.path.isdir(DEFAULTS):
        pytest.skip(f"defaults not found at {DEFAULTS}")
    bundle = build_bundle_from_defaults(DEFAULTS, "farm")
    if port_table:
        for a in bundle["assets"]:
            for t in a["ports"].values():
                if t.get("kind") == "gpio":
                    t.update(chip=CHIP, line=t.pop("gpioNumber"))
                    if t["direction"] == "out":
                        t["activeLow"] = True
    return bundle


def _runtime(bundle=None):
    rt = BlockRuntime(SimulatedIO(), RecordingSink(), clock=SimClock(datetime(2026, 9, 7, 5, 0, tzinfo=ZoneInfo("Asia/Kolkata"))))
    if bundle is not None:
        rt.compile(bundle)
    return rt


def _guard(rt, ports_version=0):
    g = LinkGuard()
    g.use_runtime(rt)
    g.note_ports_version(ports_version)
    return g


def _switch(asset_id, value=1, **more):
    return {"commandId": "c1", "type": "setPower", "sourceKey": "device.power", "value": value,
            **({"assetId": asset_id} if asset_id else {}), **more}


def _test_port(line, port_id="DO3", asset_id=None, direction="out"):
    cmd = {"commandId": "t1", "type": "testPort", "portId": port_id, "action": "pulse" if direction == "out" else "read",
           "transport": {"kind": "gpio", "chip": CHIP, "line": line, "direction": direction, "activeLow": True}}
    if asset_id:
        cmd["assetId"] = asset_id
    return cmd


# --------------------------------------------- the runtime names one card only

def test_the_runtime_no_longer_drives_another_cards_port_for_a_card_it_does_not_know():
    rt = _runtime(_farm(port_table=False))
    rt.step()
    assert rt.owns_port("pump-1", "device.power") is True
    assert rt.owns_port("someone-elses-card", "device.power") is False
    assert rt.owns_port(None, "device.power") is False
    answer = rt.apply_command(_switch("someone-elses-card"))
    assert answer == {"accepted": False, "reason": "port not bound to a program variable"}
    rt.step()
    assert rt.io.output("pump-1", "device.power") is False


def test_port_links_are_the_ports_of_the_running_program_with_their_cards():
    rt = _runtime(_farm(port_table=True))
    links = {(p.asset_id, p.source_key, p.transport.get("line"), p.transport.get("direction")) for p in rt.port_links()}
    assert ("pump-1", "device.power", 17, "out") in links
    assert ("flow-1", "sensor.flow", 5, "in") in links
    assert _runtime().port_links() == []


# --------------------------------------------------- a node with a port table

def test_on_a_port_table_node_only_the_linked_card_may_switch_its_port():
    g = _guard(_runtime(_farm(port_table=True)))
    assert g.runs_port_table() is True
    assert g.check_switch(_switch("pump-1")) is None
    assert g.check_switch(_switch("valve-2", value=0)) is None
    assert g.check_switch(_switch("intruder-1", gpioNumber=17)) == \
        "refused: device.power of card intruder-1 is not linked to a port of this node"
    assert g.check_switch(_switch(None)) == "refused: this node runs a port table and the command names no card"
    assert g.check_switch({**_switch("pump-1"), "sourceKey": "device.speed"}) == \
        "refused: device.speed of card pump-1 is not linked to a port of this node"
    assert g.check_switch({**_switch("flow-1"), "sourceKey": "sensor.flow"}) == \
        "refused: sensor.flow of card flow-1 is an input on this node"
    assert g.check_switch({**_switch("intruder-1"), "type": "setPortValue", "value": 40}) is not None


def test_a_reported_port_table_without_a_program_refuses_every_switch():
    g = _guard(_runtime(), ports_version=3)
    assert g.runs_port_table() is True
    assert g.check_switch(_switch("pump-1", gpioNumber=17)) == \
        "refused: device.power of card pump-1 is not linked to a port of this node"


def test_the_ports_version_only_goes_up_and_ignores_rubbish():
    g = LinkGuard()
    for v in (2, None, "x", 1, "4"):
        g.note_ports_version(v)
    assert g.ports_version == 4


# -------------------------------------------- a node without a port table

def test_without_a_port_table_the_legacy_gpio_path_stays_but_never_onto_a_program_output():
    g = _guard(_runtime(_farm(port_table=False)))
    assert g.runs_port_table() is False
    assert g.check_switch(_switch("pump-1", gpioNumber=17)) is None  # the card the program drives it for
    assert g.check_switch(_switch("intruder-1", gpioNumber=17)) == \
        "refused: GPIO 17 is driven by the running program for another card"
    assert g.check_switch(_switch("intruder-1"), gpio=27) == \
        "refused: GPIO 27 is driven by the running program for another card", "the GPIO the node's pins resolve"
    assert g.check_switch(_switch("legacy-card", gpioNumber=12)) is None, "a pin no program drives: legacy path"
    assert _guard(_runtime()).check_switch(_switch("any-card", gpioNumber=17)) is None, "no program, no table: as before"


def test_without_a_port_table_a_gpio_the_program_reads_is_never_driven_as_an_output():
    """GPIO 5 is flow-1's flow switch, read by the dry-run and flow rules. Before, a stranger's
    setPower (or a boot-reconcile entry) naming it set the sensor line up as a driven output."""
    g = _guard(_runtime(_farm(port_table=False)))
    assert g.check_switch(_switch("intruder-1", gpioNumber=5)) == "refused: GPIO 5 is read by the running program"
    assert g.check_switch(_switch("flow-1", gpioNumber=5)) == "refused: GPIO 5 is read by the running program"
    assert g.check_switch({**_switch("flow-1"), "sourceKey": "sensor.flow"}) == \
        "refused: sensor.flow of card flow-1 is an input on this node"
    assert g.check_boot_command({**_switch("intruder-1"), "gpioNumber": 5}) == "refused: GPIO 5 is read by the running program"


# ------------------------------------------------------------------ testPort

def test_a_port_test_naming_a_card_must_name_the_card_the_port_is_linked_to(caplog):
    g = _guard(_runtime(_farm(port_table=True)), ports_version=2)
    assert g.check_port_test(_test_port(17, "DO1", asset_id="pump-1")) is None
    assert g.check_port_test(_test_port(17, "DO1", asset_id="valve-1")) == "refused: DO1 is linked to another card on this node"
    assert g.check_port_test(_test_port(26, "DIO7", asset_id="pump-1")) == "refused: DIO7 is not linked to card pump-1 on this node"
    assert g.check_port_test(_test_port(26, "DIO7")) is None, "a free port is tested as before (the first check on a board)"
    assert g.check_port_test(_test_port(5, "DI1", direction="in", asset_id="flow-1")) is None, "direction is not identity"
    with caplog.at_level(logging.INFO, logger="agal_one_agent.ports.links"):
        assert g.check_port_test(_test_port(17, "DO1")) is None
    assert "DO1 names no card; the port is linked to pump-1" in caplog.text


def test_a_port_test_on_a_node_without_a_port_table_is_not_checked():
    g = _guard(_runtime())
    assert g.check_port_test(_test_port(17, "DIO1", asset_id="whatever")) is None


# ------------------------------------------------------- the ack in words

class FakeMqtt:
    def __init__(self):
        self.acks = []

    def publish_command_ack(self, command_id, status, applied_value=None, error=None):
        self.acks.append((command_id, status, error))


def test_a_refused_command_gets_a_failed_ack_that_says_why():
    mqtt = FakeMqtt()
    refuse(mqtt, _switch("intruder-1"), "refused: device.power of card intruder-1 is not linked to a port of this node")
    assert mqtt.acks == [("c1", "failed", "refused: device.power of card intruder-1 is not linked to a port of this node")]


def test_on_a_port_table_node_a_strangers_switch_is_refused_and_never_reaches_a_gpio(caplog):
    rt = _runtime(_farm(port_table=True))
    rt.step()
    mqtt, legacy = FakeMqtt(), []
    with caplog.at_level(logging.WARNING, logger="agal_one_agent.ports.links"):
        handle_switch(_switch("intruder-1", gpioNumber=17), _guard(rt), rt, mqtt, legacy=legacy.append, gpio=17)
    assert mqtt.acks == [("c1", "failed", "refused: device.power of card intruder-1 is not linked to a port of this node")]
    assert legacy == []
    rt.step()
    assert rt.io.output("pump-1", "device.power") is False
    assert "intruder-1" in caplog.text


def test_the_linked_card_switches_through_the_runtime():
    rt = _runtime(_farm(port_table=True))
    rt.step()
    mqtt, legacy = FakeMqtt(), []
    handle_switch(_switch("valve-1"), _guard(rt), rt, mqtt, legacy=legacy.append)
    assert [a[1] for a in mqtt.acks] == ["acknowledged", "completed"] and legacy == []
    rt.step()
    assert rt.io.output("valve-1", "device.power") is True


def test_without_a_port_table_a_pin_no_program_drives_goes_to_the_legacy_executor_and_is_logged(caplog):
    rt = _runtime(_farm(port_table=False))
    mqtt, legacy = FakeMqtt(), []
    with caplog.at_level(logging.WARNING, logger="agal_one_agent.ports.links"):
        handle_switch(_switch("legacy-card", gpioNumber=12), _guard(rt), rt, mqtt, legacy=legacy.append, gpio=12)
    assert [c["assetId"] for c in legacy] == ["legacy-card"] and mqtt.acks == []
    assert "legacy GPIO path: setPower for card legacy-card, GPIO 12 (this node has no port table)" in caplog.text
    handle_switch(_switch("intruder-1", gpioNumber=17), _guard(rt), rt, mqtt, legacy=legacy.append, gpio=17)
    assert mqtt.acks == [("c1", "failed", "refused: GPIO 17 is driven by the running program for another card")]
    assert len(legacy) == 1


def test_a_refused_port_test_is_reported_as_a_failed_test_and_acked_failed():
    class Reporter:
        def __init__(self):
            self.tests = []

        def report_port_test(self, command_id, result):
            self.tests.append((command_id, result))

    g, http, mqtt = _guard(_runtime(_farm(port_table=True))), Reporter(), FakeMqtt()
    assert refuse_port_test(_test_port(17, "DO1", asset_id="valve-1"), g, http, mqtt) is True
    assert http.tests == [("t1", {"portId": "DO1", "result": "failed", "detail": "refused: DO1 is linked to another card on this node"})]
    assert mqtt.acks == [("t1", "failed", "refused: DO1 is linked to another card on this node")]
    assert refuse_port_test(_test_port(26, "DIO7"), g, http, mqtt) is False
    assert len(http.tests) == 1


# ----------------------------------------------------- boot reconciliation

class _SyncThread:
    def __init__(self, target, name=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class _BootState:
    def __init__(self, commands):
        self._commands = commands

    def fetch_boot_state(self, token):
        return self._commands


class _Cfg:
    class node:
        auth_token = "t"


def test_boot_reconciliation_applies_a_card_only_for_its_own_port_and_never_behind_the_program(monkeypatch):
    applied = []
    monkeypatch.setattr(boot_reconciler, "execute", lambda config, mqtt, cmd: applied.append(cmd["assetId"]))
    monkeypatch.setattr(boot_reconciler.threading, "Thread", _SyncThread)
    g = _guard(_runtime(_farm(port_table=False)))
    commands = [
        {"assetId": "pump-1", "sourceKey": "device.power", "gpioNumber": 17, "value": 1},      # the program decides
        {"assetId": "intruder-1", "sourceKey": "device.power", "gpioNumber": 27, "value": 1},  # another card's output
        {"assetId": "legacy-card", "sourceKey": "device.power", "gpioNumber": 12, "value": 0},  # nobody else's: applied
    ]
    boot_reconciler.reconcile(_Cfg, FakeMqtt(), _BootState(commands), guard=lambda c: g.check_boot_command(c))
    assert applied == ["legacy-card"]
    assert g.check_boot_command({**commands[0], "type": "setPower"}) == "skipped: the running program drives this port"


def test_boot_reconciliation_without_a_guard_is_unchanged(monkeypatch):
    applied = []
    monkeypatch.setattr(boot_reconciler, "execute", lambda config, mqtt, cmd: applied.append(cmd["assetId"]))
    monkeypatch.setattr(boot_reconciler.threading, "Thread", _SyncThread)
    boot_reconciler.reconcile(_Cfg, FakeMqtt(), _BootState([{"assetId": "a", "gpioNumber": 12, "value": 0}]))
    assert applied == ["a"]


def test_the_agent_entry_point_still_imports():
    import agal_one_agent.main as main  # noqa: F401 — main() wires the guard; a NameError would show here only at run time
    assert callable(main.main)


# ------------------------------------------- the port table, before an answer

def test_a_node_that_once_ran_a_port_table_keeps_running_one_after_a_restart(tmp_path):
    """portsVersion was learnt only from one unretried hardware report per start: a node whose
    report was not answered fell back to the legacy path. It is kept in the state directory."""
    first = LinkGuard(state_dir=str(tmp_path))
    assert first.known is False
    first.note_ports_version(3)
    assert json.loads((tmp_path / "ports_version.json").read_text()) == {"portsVersion": 3}

    after_restart = LinkGuard(state_dir=str(tmp_path))  # no answer this time
    after_restart.use_runtime(_runtime(_farm(port_table=False)))
    assert after_restart.known and after_restart.ports_version == 3 and after_restart.runs_port_table()
    assert after_restart.check_switch(_switch("intruder-1", gpioNumber=12)) == \
        "refused: device.power of card intruder-1 is not linked to a port of this node"
    after_restart.note_ports_version(1)
    assert LinkGuard(state_dir=str(tmp_path)).ports_version == 3, "it only goes up"


def test_until_the_node_knows_whether_it_runs_a_port_table_the_legacy_path_is_refused():
    g = LinkGuard()
    g.use_runtime(_runtime(_farm(port_table=False)))
    assert g.known is False
    assert g.check_switch(_switch("legacy-card", gpioNumber=12)) == \
        "refused: this node does not know yet whether it runs a port table (its hardware report is not answered yet)"
    assert g.check_switch(_switch("pump-1", gpioNumber=17)) is None, "a card's own output in the program: the runtime drives it"
    g.note_ports_version(0)  # the answer: no port table
    assert g.known and g.check_switch(_switch("legacy-card", gpioNumber=12)) is None


def test_boot_reconciliation_waits_for_the_answer_to_the_hardware_report(monkeypatch):
    monkeypatch.setattr(boot_reconciler.threading, "Thread", _SyncThread)
    commands = [{"assetId": "legacy-card", "sourceKey": "device.power", "gpioNumber": 12, "value": 1}]

    applied = []
    monkeypatch.setattr(boot_reconciler, "execute", lambda config, mqtt, cmd: applied.append(cmd["assetId"]))
    g = LinkGuard()
    g.use_runtime(_runtime(_farm(port_table=False)))
    boot_reconciler.reconcile(_Cfg, FakeMqtt(), _BootState(commands), guard=lambda c: g.check_boot_command(c),
                              wait_for=lambda: None)  # never answered
    assert applied == [], "not before the node knows"
    boot_reconciler.reconcile(_Cfg, FakeMqtt(), _BootState(commands), guard=lambda c: g.check_boot_command(c),
                              wait_for=lambda: g.note_ports_version(0))  # answered while it waits
    assert applied == ["legacy-card"]

    applied.clear()
    table = LinkGuard()
    table.use_runtime(_runtime(_farm(port_table=False)))
    boot_reconciler.reconcile(_Cfg, FakeMqtt(), _BootState(commands), guard=lambda c: table.check_boot_command(c),
                              wait_for=lambda: table.note_ports_version(4))
    assert applied == [], "a port-table node never applies the boot state through the legacy executor"


# ------------------------------------------ the check and the dispatch, as one

def test_the_switch_check_and_its_dispatch_hold_the_runtime_lock():
    """A program pulled on its own thread could land between the check and the dispatch: the
    card's port gone, owns_port False, and the command went on to the legacy executor."""
    rt = _runtime(_farm(port_table=True))
    rt.step()
    seen = []

    def free(lock):
        got = []
        t = threading.Thread(target=lambda: got.append(lock.acquire(blocking=False)))
        t.start()
        t.join()
        if got[0]:
            lock.release()
        return got[0]

    class Watching(LinkGuard):
        def check_switch(self, command, gpio=None):
            seen.append(free(rt.lock))
            return super().check_switch(command, gpio)

    g = Watching()
    g.use_runtime(rt)
    g.note_ports_version(2)
    mqtt, legacy = FakeMqtt(), []
    handle_switch(_switch("valve-1"), g, rt, mqtt, legacy=legacy.append)
    assert seen == [False], "another thread could not take the lock during the check"
    assert [a[1] for a in mqtt.acks] == ["acknowledged", "completed"]


def test_a_port_table_node_never_hands_a_switch_to_the_legacy_executor():
    """Whatever let a command past the check (here a check that says yes to everything), a node
    that runs a port table refuses it rather than write the GPIO the command names."""
    rt = _runtime(_farm(port_table=True))

    class Lenient(LinkGuard):
        def check_switch(self, command, gpio=None):
            return None

    g = Lenient()
    g.use_runtime(rt)
    mqtt, legacy = FakeMqtt(), []
    handle_switch(_switch("intruder-1", gpioNumber=17), g, rt, mqtt, legacy=legacy.append, gpio=17)
    assert legacy == []
    assert mqtt.acks == [("c1", "failed", "refused: device.power of card intruder-1 is not linked to a port of this node")]


# ----------------------------------------------------- one port, one card

def _intruder(transport, default=None):
    aab = {"variables": [{"name": "relay", "kind": "node", "type": "bool", "settable": True, "exposed": True,
                          **({"default": default} if default is not None else {}),
                          "node": {"sourceKey": "device.power", "direction": "out"}}], "rules": []}
    return {"assetId": "intruder-1", "assetType": "motor_controller", "aab": aab, "ports": {"device.power": transport}}


def test_a_bundle_with_two_cards_on_one_output_line_is_refused():
    """The backend copies each card's own portBindings[].transport into the bundle, so a stranger
    could copy pump-1's chip label and line into a card naming this node; the node ran both."""
    bundle = _farm(port_table=True)
    bundle["assets"].append(_intruder({"kind": "gpio", "chip": CHIP, "line": 17, "direction": "out"}))
    with pytest.raises(CompileError) as e:
        _runtime(bundle)
    assert str(e.value) == ("line 17 of pinctrl-bcm2835 is claimed by device.power of card intruder-1 and "
                            "device.power of card pump-1: a port is linked to one card")


def test_a_reload_that_adds_a_second_card_on_a_running_line_is_rejected_and_writes_nothing():
    bundle = _farm(port_table=True)
    rt = _runtime(bundle)
    rt.step()
    mark = len(rt.io.writes)
    bad = dict(bundle, version=2, assets=bundle["assets"] + [_intruder({"kind": "gpio", "chip": CHIP, "line": 17,
                                                                        "direction": "out"}, default=True)])
    with pytest.raises(CompileError, match="a port is linked to one card"):
        rt.compile(bad)
    for _ in range(3):
        rt.step()
    assert rt.version == 1 and rt.io.writes[mark:] == []
    assert [p.asset_id for p in rt.port_links() if p.transport.get("line") == 17] == ["pump-1"]


def test_one_line_named_as_gpio_by_one_card_and_pwm_by_another_is_one_port():
    bundle = _farm(port_table=False)
    bundle["assets"].append(_intruder({"kind": "pwm", "gpioNumber": 17, "frequencyHz": 1000, "dutyCyclePct": 50}))
    with pytest.raises(CompileError, match="GPIO 17 is claimed by device.power of card intruder-1 and device.power of card pump-1"):
        _runtime(bundle)


def test_an_output_on_a_line_another_card_reads_is_refused_but_two_cards_may_read_one_input():
    bundle = _farm(port_table=False)
    bundle["assets"].append(_intruder({"kind": "gpio", "gpioNumber": 5, "direction": "out"}))
    with pytest.raises(CompileError, match="GPIO 5 is claimed by"):
        _runtime(bundle)
    shared = _farm(port_table=False)
    flow = next(a for a in shared["assets"] if a["assetId"] == "flow-1")
    shared["assets"].append(dict(flow, assetId="flow-1-copy"))
    assert _runtime(shared).version == 1
