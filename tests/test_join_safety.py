"""Join safety on the agent's side (contracts v1.14.2, nodes/README.md §10, SYNC.md v1.14.2 item 8 c).

The only production node — a Raspberry Pi 3B on agent 0.3.1, joined by the join command, no git on
it — reaches 0.3.4 by ONE last join; after it every update comes over the air (founder: never the
Pi's terminal after linking). A compatibility check of that join found three hazards. The agent's
part of each, tested here:

- H3: every status carries the version of the code that is running — the clean stop's offline
  status too, which said "0.1.0" (the reporters' default) and which the backend stored, so the app
  showed agent 0.1.0 during every restart.
- H1: the served script's guard must accept the config.yaml THIS agent leaves — ``update_pins``
  rewrites it with ``yaml.dump`` on every syncPinConfig, which writes the uid bare (or in single
  quotes). The vectors' ``guard.script`` runs here, through bash, on the file update_pins wrote,
  and nothing else in the agent reads the uid as text. Join-safety review: the guard reads only the
  node block's own uid keys — ``update_pins`` re-dumps what the cloud sent, and a pin label or a node
  name over several lines, a label folded at 80 columns or a sensor_params key named uid put other
  ``uid:`` lines in the file, which the first guard took for another node's.
- H2 (fixed by the backend's pending credential, command-signing.md §7): the agent holds one token,
  its config.yaml. Owed here: the first HTTPS request after the join's restart presents that token
  at once and without MQTT (it promotes the pending token, §7.3), and a command the backend signed
  with the old token in the seconds before is refused AND answered — the answer presents the new
  token, so it is handled after the promotion and the app shows the command failed (bad_signature)
  instead of waiting for its timeout.

No broker, no network: urllib.request.urlopen is a fake that answers per endpoint and refuses any
other host.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from email.message import Message
from unittest.mock import MagicMock

import pytest
import yaml

import agal_one_agent
from agal_one_agent import http_reporter as http_reporter_mod
from agal_one_agent import main as agent_main
from agal_one_agent import ota_updater
from agal_one_agent.command_acks import CommandAcks, wire_command_acks
from agal_one_agent.command_signing import sign_command, wire_command_gate
from agal_one_agent.config import AgentConfig, MqttConfig, TelemetryConfig
from agal_one_agent.heartbeat import HeartbeatPublisher
from agal_one_agent.http_reporter import HttpReporter
from agal_one_agent.mqtt_client import AgalOneMqttClient

import _contracts  # noqa: E402 — tests/ is on sys.path (pytest rootdir insertion)


def _load(path: str, what: str) -> dict:
    # Not skipped when missing: point AGAL_CONTRACTS_DIR at contracts >= 1.14.2 (join safety).
    assert os.path.exists(path), f"{what} not found at {path} (contracts >= 1.14.2 join safety; set AGAL_CONTRACTS_DIR)"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


J = _load(_contracts.JOIN_VECTORS, "join vectors")
SCHEMA = _load(_contracts.IOT_NODE_SCHEMA, "iot-node schema")
PLACEHOLDER = J["rules"]["placeholderVersion"]
OLD_TOKEN = J["credential"]["tokens"]["A"]  # the node's current token before the join
NEW_TOKEN = J["credential"]["tokens"]["B"]  # the join's token: pending until the board presents it
INGRESS = "https://ingress.example/telemetryIngress"
NODE = "Nd7Pq2Lk9Vb4Xc1Zt8Ra"
COMMAND_ID = "Xq3k9TzLmN2pRs7VbW1c"  # an /assetCommands auto-id: tracked, answered when refused


# ====================================================================== the fake backend

class _Answer:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = json.dumps(body).encode("utf-8")
        self.headers = Message()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Ingress:
    """urllib.request.urlopen for these tests: every request recorded (time, endpoint, Authorization,
    body), an answer per endpoint, nothing else reached — a request to any other host is recorded
    and refused (the test then fails on it)."""

    HOST = "https://ingress.example/"

    def __init__(self, answer=None):
        self.t0 = time.monotonic()
        self.requests: list[dict] = []
        self._cond = threading.Condition()
        self.answer = answer or (lambda endpoint, auth, body: (200, _default_answer(endpoint, body)))

    def __call__(self, req, timeout=None):
        url = req.full_url
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        endpoint = url[len(self.HOST):] if url.startswith(self.HOST) else url
        record = {"t": time.monotonic() - self.t0, "endpoint": endpoint,
                  "auth": req.get_header("Authorization"), "body": body}
        with self._cond:
            self.requests.append(record)
            self._cond.notify_all()
        if not url.startswith(self.HOST):
            raise urllib.error.URLError(f"not reached in a test: {url}")
        status, answer = self.answer(endpoint, record["auth"], body)
        if status != 200:
            raise urllib.error.HTTPError(url, status, "refused", Message(), None)
        return _Answer(status, answer)

    def wait_for(self, predicate, timeout: float = 10.0) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: any(predicate(r) for r in self.requests), timeout)

    def of(self, endpoint: str, type_: str | None = None) -> list[dict]:
        return [r for r in self.requests if r["endpoint"] == endpoint
                and (type_ is None or (r["body"] or {}).get("type") == type_)]


def _default_answer(endpoint: str, body) -> dict:
    if endpoint == "reportHardware":
        return {"boardMatch": "none", "portsVersion": 0}
    if endpoint == "getBootState":
        return {"commands": []}
    if endpoint == "getProgram":
        return {"version": 0, "unchanged": True}
    return {"success": True}


class _Paho:
    """What AgalOneMqttClient calls on its paho client: every publish recorded."""

    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload)))

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def statuses(self) -> list[dict]:
        return [p["payload"] for _, p in self.published if p.get("type") == "status"]


def _mqtt(connected=True) -> AgalOneMqttClient:
    client = AgalOneMqttClient(MqttConfig(broker="broker.example", username="shared-device-login",
                                          status_topic=f"agal/{NODE}/status", commands_topic=f"agal/{NODE}/commands"))
    client._client = _Paho()
    client._connected = connected
    return client


def _config() -> AgentConfig:
    cfg = MagicMock(spec=AgentConfig)
    cfg.telemetry = TelemetryConfig(heartbeat_seconds=3600)
    return cfg


# ====================================================================== H3: the version in every status

def _stored_firmware_version(status: dict):
    """The contract's rule for what a status writes onto the node (reference/join.mjs
    storedFirmwareVersion): only from an online status, only a version, never the placeholder."""
    if not isinstance(status, dict) or status.get("online") is not True:
        return None
    v = status.get("firmwareVersion")
    if not isinstance(v, str):
        return None
    s = v[1:] if v.startswith("v") else v
    return s if re.fullmatch(SCHEMA["properties"]["firmwareVersion"]["pattern"], s) and s != PLACEHOLDER else None


@pytest.mark.parametrize("case", J["status"]["cases"], ids=[c["name"] for c in J["status"]["cases"]])
def test_the_ported_version_rule_answers_every_vector(case):
    # The rule below is applied to what this agent sends; first, it is the contract's rule.
    assert _stored_firmware_version(case["payload"]) == case["expect"]


def test_the_running_version_is_one_the_backend_stores():
    v = agal_one_agent.__version__
    assert re.fullmatch(SCHEMA["properties"]["firmwareVersion"]["pattern"], v), v
    assert v != PLACEHOLDER


def test_a_checkout_without_an_install_reports_this_release():
    # __version__ comes from the installed package's metadata (a pip install of the release archive:
    # the code and its metadata together); a checkout run without one falls back to the literal in
    # __init__.py, which must be the release setup.py builds.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "setup.py"), encoding="utf-8") as f:
        built = re.search(r'^\s*version="([^"]+)"', f.read(), re.M).group(1)
    with open(os.path.join(here, "agal_one_agent", "__init__.py"), encoding="utf-8") as f:
        fallback = re.search(r'^\s*__version__ = "([^"]+)"', f.read(), re.M).group(1)
    assert fallback == built


def test_the_clean_stop_posts_offline_with_the_running_version_on_both_channels(monkeypatch):
    ingress = _Ingress()
    monkeypatch.setattr(urllib.request, "urlopen", ingress)
    mqtt = _mqtt()
    hb = HeartbeatPublisher(_config(), mqtt, http_reporter=HttpReporter(NODE, auth_token=NEW_TOKEN, base_url=INGRESS))

    hb.start()
    hb.stop()

    posted = [r["body"]["payload"] for r in ingress.of("telemetryIngress", "status")]
    assert [(p["online"], p.get("firmwareVersion")) for p in posted] == [
        (True, agal_one_agent.__version__), (False, agal_one_agent.__version__)]
    assert [(p["online"], p.get("firmwareVersion")) for p in mqtt._client.statuses()] == [
        (True, agal_one_agent.__version__), (False, agal_one_agent.__version__)]
    assert all(r["auth"] == f"Bearer {NEW_TOKEN}" for r in ingress.requests)
    # What the backend's rule makes of them: the heartbeat stores this version, the clean stop
    # leaves it (and neither is ever the placeholder).
    assert [_stored_firmware_version(p) for p in posted] == [agal_one_agent.__version__, None]
    assert PLACEHOLDER not in json.dumps([r["body"] for r in ingress.requests])


def test_the_version_is_resolved_once_not_read_again_from_the_installed_metadata(monkeypatch):
    # During a join pip replaces the package under the running agent; during an over-the-air update
    # the old process waits up to 30 minutes for a pump to stop with the new package on disk. Its
    # statuses keep naming the code that runs — never the metadata on disk, never a fallback.
    import importlib.metadata as metadata
    import agal_one_agent.heartbeat as heartbeat_mod
    assert not hasattr(heartbeat_mod, "_get_agent_version") and not hasattr(heartbeat_mod, "pkg_version")
    ingress = _Ingress()
    monkeypatch.setattr(urllib.request, "urlopen", ingress)
    mqtt = _mqtt()
    hb = HeartbeatPublisher(_config(), mqtt, http_reporter=HttpReporter(NODE, auth_token=NEW_TOKEN, base_url=INGRESS))
    monkeypatch.setattr(metadata, "version", lambda name: "9.9.9")  # pip installed another version
    hb.start()
    monkeypatch.setattr(metadata, "version", MagicMock(side_effect=metadata.PackageNotFoundError))  # mid-install
    hb._send_heartbeat()
    hb.stop()
    https = [r["body"]["payload"].get("firmwareVersion") for r in ingress.of("telemetryIngress", "status")]
    on_mqtt = [s.get("firmwareVersion") for s in mqtt._client.statuses()]
    assert https == on_mqtt == [agal_one_agent.__version__] * 3


def test_a_status_without_a_version_carries_none_never_a_placeholder(monkeypatch):
    for fn in (HttpReporter.report_status, AgalOneMqttClient.publish_status):
        assert inspect.signature(fn).parameters["firmware_version"].default is None, fn.__qualname__
    ingress = _Ingress()
    monkeypatch.setattr(urllib.request, "urlopen", ingress)
    HttpReporter(NODE, auth_token=NEW_TOKEN, base_url=INGRESS).report_status(online=False, uptime=5)
    mqtt = _mqtt()
    mqtt.publish_status(online=False, uptime=5)
    assert "firmwareVersion" not in ingress.of("telemetryIngress", "status")[0]["body"]["payload"]
    assert "firmwareVersion" not in mqtt._client.statuses()[0]


def test_the_https_offline_status_goes_even_when_the_mqtt_publish_raises(monkeypatch):
    ingress = _Ingress()
    monkeypatch.setattr(urllib.request, "urlopen", ingress)
    mqtt = _mqtt()
    mqtt._client.publish = MagicMock(side_effect=ValueError("the broker connection is gone"))
    hb = HeartbeatPublisher(_config(), mqtt, http_reporter=HttpReporter(NODE, auth_token=NEW_TOKEN, base_url=INGRESS))
    hb.start()
    hb.stop()
    assert [r["body"]["payload"]["online"] for r in ingress.of("telemetryIngress", "status")] == [True, False]


def test_the_last_will_names_the_running_version(monkeypatch):
    import paho.mqtt.client as paho
    seen = {}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def tls_set(self, **kw):
            pass

        def username_pw_set(self, *a):
            pass

        def will_set(self, topic, payload=None, qos=0, retain=False):
            seen["will"] = json.loads(payload)

        def connect(self, *a, **kw):
            pass

        def loop_start(self):
            pass

    monkeypatch.setattr(paho, "Client", _Client)
    AgalOneMqttClient(MqttConfig(broker="broker.example", status_topic=f"agal/{NODE}/status")).connect()
    assert seen["will"]["payload"] == {"nodeUid": "", "online": False, "uptime": 0,
                                       "firmwareVersion": agal_one_agent.__version__}


# ====================================================================== H1: the guard and update_pins

GUARD = J["guard"]
GUARD_UID = GUARD["nodeUid"]
DEFAULT_CONF_LINE = "CONF='/etc/agal-one-agent/config.yaml'"
BASH = shutil.which("bash")


def _guard_script(node_uid: str, conf: str) -> str:
    """The vectors' guard (joinGuardScript, as the served script must contain it) for this node and
    this config path. Only the id and CONF change; the vectors name the id three times."""
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", node_uid), "no join script is served for this nodeUid"
    assert "'" not in conf
    script = GUARD["script"]
    assert script.count(GUARD_UID) == 3 and script.count(DEFAULT_CONF_LINE) == 1
    return script.replace(GUARD_UID, node_uid).replace(DEFAULT_CONF_LINE, f"CONF='{conf}'")


def _run_guard(node_uid: str, conf: str) -> str:
    """proceed / refuse, as the served script would (set -euo pipefail, the guard, then the rest)."""
    assert BASH, "bash is needed to run the guard the join script serves"
    r = subprocess.run([BASH, "-c", f"set -euo pipefail\n{_guard_script(node_uid, conf)}echo GUARD_PROCEEDS\n"],
                       capture_output=True, text=True, timeout=30, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    if r.returncode == 0 and "GUARD_PROCEEDS" in r.stdout:
        return "proceed"
    if r.returncode == 1 and "already linked to another Agal One node" in r.stderr and "GUARD_PROCEEDS" not in r.stdout:
        return "refuse"
    return f"exit {r.returncode}: {r.stderr.strip()}"


def _old_guard_proceeds(node_uid: str, conf: str) -> bool:
    # The guard as served until contracts v1.14.2: grep -q 'uid: "<nodeUid>"' "$CONF".
    r = subprocess.run(["grep", "-q", f'uid: "{node_uid}"', conf], capture_output=True)
    return r.returncode == 0


@pytest.mark.parametrize("case", GUARD["cases"], ids=[c["name"] for c in GUARD["cases"]])
def test_the_vectors_guard_through_bash(case, tmp_path):
    # The agent's own run of the contract's guard cases (the contracts' validate.mjs and the backend's
    # test:join run them too): the bash behaves as the vectors say on this board's bash and grep.
    conf = tmp_path / "config.yaml"
    if case["config"] is not None:
        conf.write_bytes(case["config"].encode("utf-8"))
    assert _run_guard(case.get("nodeUid", GUARD_UID), str(conf)) == case["expect"]


def _join_config(node_uid: str, token: str = NEW_TOKEN, name: str = "Pump house") -> str:
    """config.yaml exactly as the join writes it (agal-one enroll.ts installerScript: every string a
    JSON string, which is a valid YAML scalar)."""
    q = json.dumps
    base = "https://asia-south1-agal-one-prod.cloudfunctions.net"
    return "\n".join([
        "node:", f"  uid: {q(node_uid)}", f"  name: {q(name)}", f"  auth_token: {q(token)}", "",
        "mqtt:", f"  broker: {q('broker.example.invalid')}", "  port: 8883", "  tls: true",
        f"  username: {q('device-login')}", f"  password: {q('device-password')}", "  topics:",
        f"    commands: {q(f'agal/{node_uid}/commands')}", f"    telemetry: {q(f'agal/{node_uid}/telemetry')}",
        f"    status: {q(f'agal/{node_uid}/status')}", "",
        "telemetry:", "  interval_seconds: 10", "  heartbeat_seconds: 30", f"  ingress_url: {q(base + '/telemetryIngress')}", "",
        "cloud:", f"  program_url: {q(base + '/getProgram')}", f"  hardware_url: {q(base + '/reportHardware')}", "",
        "# The port table lives in the cloud and arrives with the program bundle (ADR-024).", "pins: []", "",
    ])


#: What a syncPinConfig carries (backend toDaemonPinConfig): a relay, an analog sensor with free-form
#: sensor_params and protection — the parts yaml.dump writes nested.
SYNCED_PINS = [
    {"physical_pin": 11, "gpio_number": 17, "protocol": "gpio_output", "label": "Pump", "assigned_to": "asset_pump1234"},
    {"physical_pin": 3, "gpio_number": 2, "protocol": "i2c", "bus_id": "I2C1", "i2c_address": 72,
     "sensor_type": "current_acs758", "sensor_params": {"channel": 0, "sensitivity_mv_per_a": 40},
     "protection": {"cut_pin_label": "Pump", "threshold_pct": 20}},
]

#: Ids the backend may serve a script for ([A-Za-z0-9_-]{1,128}), among them every kind yaml.dump
#: writes in single quotes because it would read as another type (int, hex, octal, date, bool, null).
NODE_UIDS = [GUARD_UID, "0x1F2E3D4C5B6A798877", "12345678901234567890", "2026-10-15", "true", "No", "null",
             "1_000", "-1", "0777", "1e5", "-", "_", "Ab" * 64]


@pytest.mark.parametrize("node_uid", NODE_UIDS)
def test_the_join_guard_accepts_the_config_yaml_update_pins_writes(node_uid, tmp_path):
    """H1, the agent's side: after a syncPinConfig — every card link and unlink sends one — the
    board's config.yaml is the one config.update_pins wrote with yaml.dump, and the next join of the
    SAME node must go on. The old guard refused it (the uid is no longer in double quotes)."""
    conf = tmp_path / "config.yaml"
    conf.write_text(_join_config(node_uid), encoding="utf-8")
    assert _run_guard(node_uid, str(conf)) == "proceed"  # the file as the join wrote it

    cfg = AgentConfig.from_yaml(str(conf))
    cfg.update_pins(SYNCED_PINS, str(conf))
    written = conf.read_text(encoding="utf-8")

    uid_lines = [line for line in written.splitlines() if re.match(r"\s*uid\s*:", line)]
    assert uid_lines in ([f"  uid: {node_uid}"], [f"  uid: '{node_uid}'"]), uid_lines  # bare or single-quoted, once
    assert _run_guard(node_uid, str(conf)) == "proceed"
    assert not _old_guard_proceeds(node_uid, str(conf)), "H1: the old guard refuses the board's own config.yaml"
    other = "Hm3Wc8Rt1Yb6Pk2Qz9Lx"
    assert _run_guard(other, str(conf)) == "refuse", "another node's join is still refused"
    near = node_uid + "x" if len(node_uid) < 128 else "x" + node_uid[1:]  # an id this one is part of / differs by one
    assert _run_guard(near, str(conf)) == "refuse"


OTHER_UID = "Hm3Wc8Rt1Yb6Pk2Qz9Lx"

#: What a syncPinConfig may carry that yaml.dump writes as more `uid:` lines (join-safety review): a
#: label over several lines, a label folded at 80 columns right before "uid: …", a sensor_params key
#: named uid (a 1-Wire sensor's id), and a protection block with one. update_pins copies them verbatim.
UID_LIKE_PINS = {
    "a label over several lines": [{"physical_pin": 11, "gpio_number": 17, "protocol": "gpio_output",
                                    "label": f"Pump\n\nuid: {OTHER_UID}"}],
    "a label folded at 80 columns": [{"physical_pin": 11, "gpio_number": 17, "protocol": "gpio_output",
                                      "label": f"Pump house main line motor starter relay for the second borewell north-field-feeder uid: {OTHER_UID}"}],
    "a sensor's uid parameter": [{"physical_pin": 7, "gpio_number": 4, "protocol": "one_wire",
                                  "sensor_type": "temperature_ds18b20", "sensor_params": {"uid": "28-00000abc1234"}}],
    "a protection block with a uid key": [{"physical_pin": 3, "gpio_number": 2, "protocol": "i2c", "sensor_type": "current_acs758",
                                           "protection": {"cut_pin_label": "Pump", "uid": OTHER_UID}}],
}


def _first_guard_proceeds(node_uid: str, conf: str) -> bool:
    """The guard as first built for join safety (agal-one 457b134): every `uid:` line in the file."""
    first = "  UID_LINES=$(LC_ALL=C grep -E '^[[:space:]]*uid[[:space:]]*:' \"$CONF\" || true)"
    lines = _guard_script(node_uid, conf).split("\n")
    at = [i for i, line in enumerate(lines) if line.lstrip().startswith("UID_LINES=")]
    assert len(at) == 1
    lines[at[0]] = first
    r = subprocess.run([BASH, "-c", "set -euo pipefail\n" + "\n".join(lines) + "echo GUARD_PROCEEDS\n"],
                       capture_output=True, text=True, timeout=30, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    return r.returncode == 0 and "GUARD_PROCEEDS" in r.stdout


@pytest.mark.parametrize("node_uid", [GUARD_UID, "0x1F2E3D4C5B6A798877"])
@pytest.mark.parametrize("kind", list(UID_LIKE_PINS))
def test_the_guard_reads_only_the_node_block_of_what_update_pins_writes(kind, node_uid, tmp_path):
    """Join-safety review (README §10.1): what the cloud sends lands in config.yaml as yaml.dump
    writes it, and a pin's label or a sensor's parameters can put more `uid:` lines in the file —
    never at the node block's own indentation. The guard reads only those: this node's join goes
    on, another node's is refused, and the first guard (every `uid:` line) refused this node's."""
    conf = tmp_path / "config.yaml"
    conf.write_text(_join_config(node_uid), encoding="utf-8")
    cfg = AgentConfig.from_yaml(str(conf))
    cfg.update_pins(UID_LIKE_PINS[kind], str(conf))
    written = conf.read_text(encoding="utf-8")
    others = [line for line in written.splitlines() if re.match(r"\s*uid\s*:", line) and not re.fullmatch(rf"  uid: '?{re.escape(node_uid)}'?", line)]
    assert others and all(line.startswith("    ") for line in others), others  # deeper than the node block's keys
    assert _run_guard(node_uid, str(conf)) == "proceed"
    assert _run_guard(OTHER_UID, str(conf)) == "refuse"
    assert not _first_guard_proceeds(node_uid, str(conf)), "the first guard refused the board's own config.yaml"
    back = AgentConfig.from_yaml(str(conf))
    assert back.node.uid == node_uid and back.node.auth_token == NEW_TOKEN
    assert [(p.label, p.sensor_params, p.protection) for p in back.pins] == [
        (p.get("label", ""), p.get("sensor_params") or {}, p.get("protection")) for p in UID_LIKE_PINS[kind]]


def test_the_guard_reads_only_the_node_block_with_a_node_name_over_several_lines(tmp_path):
    """The node's own name, as the join writes it (one JSON-quoted line), then yaml.dump: a quoted
    scalar over several lines whose continuation reads `uid: <another node>` — indented under `name:`."""
    conf = tmp_path / "config.yaml"
    conf.write_text(_join_config(GUARD_UID, name=f"Pump house\nuid: {OTHER_UID}"), encoding="utf-8")
    assert _run_guard(GUARD_UID, str(conf)) == "proceed"  # as the join wrote it: the name on one line
    AgentConfig.from_yaml(str(conf)).update_pins(SYNCED_PINS, str(conf))
    written = conf.read_text(encoding="utf-8")
    assert f"\n    uid: {OTHER_UID}'\n" in written, written
    assert _run_guard(GUARD_UID, str(conf)) == "proceed"
    assert _run_guard(OTHER_UID, str(conf)) == "refuse"
    assert not _first_guard_proceeds(GUARD_UID, str(conf))
    assert AgentConfig.from_yaml(str(conf)).node.name == f"Pump house\nuid: {OTHER_UID}"


@pytest.mark.parametrize("node_uid", NODE_UIDS)
def test_nothing_else_in_the_agent_reads_the_uid_as_text(node_uid, tmp_path):
    """The agent reads config.yaml only through yaml.safe_load (AgentConfig.from_yaml — main, the OTA
    reports, and the balena render_config.py, which also writes with safe_dump): the uid is the same
    string, the same str, whether the join quoted it or yaml.dump did not, and so are the token and
    the topics every request and command depend on."""
    conf = tmp_path / "config.yaml"
    conf.write_text(_join_config(node_uid), encoding="utf-8")
    before = AgentConfig.from_yaml(str(conf))
    before.update_pins(SYNCED_PINS, str(conf))
    for _ in range(2):  # and again, as every later syncPinConfig does
        after = AgentConfig.from_yaml(str(conf))
        assert type(after.node.uid) is str and after.node.uid == node_uid
        assert after.node.auth_token == NEW_TOKEN and after.node.name == "Pump house"
        assert (after.mqtt.commands_topic, after.mqtt.status_topic, after.mqtt.username, after.mqtt.password) == (
            f"agal/{node_uid}/commands", f"agal/{node_uid}/status", "device-login", "device-password")
        assert after.telemetry.ingress_url == before.telemetry.ingress_url
        assert [p.physical_pin for p in after.pins] == [11, 3]
        after.update_pins(SYNCED_PINS, str(conf))
    # The only other places in the agent that name config.yaml do not parse it as text: the OTA
    # reports load it with AgentConfig.from_yaml, the units pass its path.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for root, _, names in os.walk(os.path.join(here, "agal_one_agent")):
        for n in names:
            if n.endswith(".py"):
                with open(os.path.join(root, n), encoding="utf-8") as f:
                    text = f.read()
                assert not re.search(r"""uid:\s*["']""", text), f"{n} matches the uid's quoting in config.yaml"


# ====================================================================== config.yaml is never half written
# SYNC.md v1.14.2 item 8 f: update_pins rewrote config.yaml in place (open(..., "w") then yaml.dump),
# so a power cut or an error mid-write left it truncated — the agent cannot start on it (no update can
# reach it then) and the next join's guard refuses it: the terminal again.

def _written_config(tmp_path, node_uid=GUARD_UID):
    conf = tmp_path / "config.yaml"
    conf.write_text(_join_config(node_uid), encoding="utf-8")
    os.chmod(conf, 0o600)
    return conf


def test_a_write_that_fails_halfway_leaves_config_yaml_as_it_was(tmp_path, monkeypatch, caplog):
    import agal_one_agent.config as config_mod
    conf = _written_config(tmp_path)
    before = conf.read_bytes()
    cfg = AgentConfig.from_yaml(str(conf))

    def dump(*a, **kw):
        stream = a[1] if len(a) > 1 else kw.get("stream")
        if stream is not None:
            stream.write("node:\n  uid: ")  # as far as a streaming dump gets before the error
        raise yaml.representer.RepresenterError("cannot represent an object")

    monkeypatch.setattr(config_mod.yaml, "dump", dump)
    cfg.update_pins(SYNCED_PINS, str(conf))
    assert conf.read_bytes() == before
    assert os.listdir(tmp_path) == ["config.yaml"], "no temporary file left behind"
    assert any("Failed to persist pin config" in r.getMessage() for r in caplog.records)
    assert [p.physical_pin for p in cfg.pins] == [11, 3], "the pins still apply in memory"


def test_a_cut_off_write_leaves_the_old_file_and_the_next_one_cleans_up(tmp_path, monkeypatch):
    import agal_one_agent.config as config_mod
    conf = _written_config(tmp_path)
    before = conf.read_bytes()
    cfg = AgentConfig.from_yaml(str(conf))

    def power_cut(src, dst):
        raise SystemExit("power cut between the write and the rename")

    monkeypatch.setattr(config_mod.os, "replace", power_cut)
    with pytest.raises(SystemExit):
        cfg.update_pins(SYNCED_PINS, str(conf))
    assert conf.read_bytes() == before
    # A real power cut leaves the temporary file (it holds the token, mode 600): the next write removes it.
    stale = tmp_path / ".config.yaml.pins-left"
    stale.write_text("node:\n  uid: Nd7", encoding="utf-8")
    monkeypatch.undo()
    cfg.update_pins(SYNCED_PINS, str(conf))
    assert sorted(os.listdir(tmp_path)) == ["config.yaml"]
    assert _run_guard(GUARD_UID, str(conf)) == "proceed"


def test_the_new_file_is_renamed_into_place_with_the_old_files_mode(tmp_path):
    conf = _written_config(tmp_path)
    inode = os.stat(conf).st_ino
    AgentConfig.from_yaml(str(conf)).update_pins(SYNCED_PINS, str(conf))
    st = os.stat(conf)
    assert st.st_ino != inode, "replaced by a rename, not rewritten in place"
    assert (st.st_mode & 0o777) == 0o600, "config.yaml holds the token: it stays 600"
    os.chmod(conf, 0o640)
    AgentConfig.from_yaml(str(conf)).update_pins(SYNCED_PINS[:1], str(conf))
    assert (os.stat(conf).st_mode & 0o777) == 0o640
    assert [p.physical_pin for p in AgentConfig.from_yaml(str(conf)).pins] == [11]


def test_where_the_rename_is_refused_it_writes_in_place(tmp_path, monkeypatch, caplog):
    import errno
    import agal_one_agent.config as config_mod
    conf = _written_config(tmp_path)

    def busy(src, dst):
        raise OSError(errno.EBUSY, "Device or resource busy")  # config.yaml bind-mounted on its own

    monkeypatch.setattr(config_mod.os, "replace", busy)
    AgentConfig.from_yaml(str(conf)).update_pins(SYNCED_PINS, str(conf))
    assert [p.physical_pin for p in AgentConfig.from_yaml(str(conf)).pins] == [11, 3]
    assert os.listdir(tmp_path) == ["config.yaml"]
    assert any("writing it in place" in r.getMessage() for r in caplog.records)


# ====================================================================== H2: the token at boot

class _Stop(Exception):
    pass


def _node_config_file(tmp_path, token: str = NEW_TOKEN) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({
        "node": {"uid": NODE, "name": "Pump house", "auth_token": token},
        "mqtt": {"broker": "broker.example", "username": "device-login", "password": "device-password"},
        "telemetry": {"ingress_url": INGRESS, "buffer_db_path": str(tmp_path / "buffer.db")},
        "blocks": {"state_dir": str(tmp_path / "state")},
    }), encoding="utf-8")
    return str(path)


def _boot(monkeypatch, tmp_path, ingress, connect):
    """main() as systemd starts it after the join's `mv` + `systemctl restart`, up to wherever the
    patched pieces stop it. Nothing outside the test: the OTA state is not read, no signal handler is
    installed, getBootState's fixed production URL is pointed at the fake host."""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", ingress)
    monkeypatch.setattr(http_reporter_mod, "BOOT_STATE_URL", "https://ingress.example/getBootState")
    monkeypatch.setattr(ota_updater, "settle_interrupted", lambda node_uid="": False)
    monkeypatch.setattr(ota_updater, "begin_probation", lambda version: False)
    monkeypatch.setattr(agent_main.signal, "signal", lambda *a, **kw: None)
    monkeypatch.setattr(AgalOneMqttClient, "connect", connect)
    monkeypatch.setattr("sys.argv", ["agal-one-agent", "--config", _node_config_file(tmp_path)])
    before = set(threading.enumerate())
    t0 = time.monotonic()
    ingress.t0 = t0
    try:
        with pytest.raises(_Stop):
            agent_main.main()
    finally:
        # The background requests THIS start made (the hardware report, boot reconciliation) end
        # while the fake is still in place: none of them may reach a real host after the test.
        for t in set(threading.enumerate()) - before:
            if t.name in ("hardware-report", "boot-reconcile"):
                t.join(timeout=35)
                assert not t.is_alive(), f"{t.name} still running"
    return t0


def test_the_first_https_request_at_boot_presents_the_new_token_at_once_without_mqtt(tmp_path, monkeypatch):
    """§7.3: the first request the board makes with the join's token promotes it. Agent 0.3.4 sends
    its hardware report from a thread started before anything else, MQTT included: the promotion
    does not wait for the broker (with the broker unreachable the agent exits after 30 s and systemd
    starts it again, 10 s later — each start sends the report first)."""
    ingress = _Ingress()
    when = {}

    def connect(self):
        # The broker never answers: wait until the agent has made its first request, then stop.
        when["connect"] = time.monotonic() - ingress.t0
        ingress.wait_for(lambda r: True, timeout=10)
        raise _Stop

    _boot(monkeypatch, tmp_path, ingress, connect)

    assert ingress.requests, "no HTTPS request before MQTT"
    first = ingress.requests[0]
    assert first["endpoint"] == "reportHardware"  # a door that reads the credential fresh (§7.3)
    assert first["auth"] == f"Bearer {NEW_TOKEN}"
    assert first["body"]["nodeUid"] == NODE and first["body"]["hardware"]["agentVersion"] == agal_one_agent.__version__
    assert first["t"] < 5.0, f"the first request left {first['t']:.3f} s after start"
    assert all(r["auth"] == f"Bearer {NEW_TOKEN}" for r in ingress.requests)
    assert all(r["endpoint"] in ("reportHardware", "telemetryIngress") for r in ingress.requests)
    print(f"\nfirst HTTPS request (reportHardware, Bearer = config token) {first['t'] * 1000:.0f} ms after main() "
          f"started; MQTT connect called at {when['connect'] * 1000:.0f} ms")


def test_a_full_boot_presents_the_token_on_every_request_and_stops_with_its_version(tmp_path, monkeypatch):
    """The whole start as far as the main loop, then a clean stop: every request — the hardware
    report, the first heartbeat (posted as soon as MQTT is up), boot reconciliation — carries the
    config's token; the first heartbeat and the clean stop's offline status name this version."""
    ingress = _Ingress()
    paho = _Paho()

    def connect(self):
        self._client = paho
        self._connected = True

    def pull(self, expected_version=None):
        raise _Stop  # the last step of the start (main.py: after boot reconciliation): stop here

    from agal_one_agent.blocks.sync import ProgramSync
    monkeypatch.setattr(ProgramSync, "pull", pull)
    _boot(monkeypatch, tmp_path, ingress, connect)

    statuses = ingress.of("telemetryIngress", "status")
    assert [(s["body"]["payload"]["online"], s["body"]["payload"].get("firmwareVersion")) for s in statuses] == [
        (True, agal_one_agent.__version__), (False, agal_one_agent.__version__)]
    assert statuses[0]["t"] < 5.0
    assert ingress.of("reportHardware") and ingress.of("getBootState")
    for r in ingress.requests:
        assert r["auth"] == f"Bearer {NEW_TOKEN}", r["endpoint"]
    assert ingress.of("getBootState")[0]["body"]["authToken"] == NEW_TOKEN  # the legacy door reads it from the body
    assert [(s["online"], s.get("firmwareVersion")) for s in paho.statuses()] == [
        (True, agal_one_agent.__version__), (False, agal_one_agent.__version__)]
    print(f"\nfirst heartbeat {statuses[0]['t'] * 1000:.0f} ms after main() started")


# ====================================================================== H2: a command signed just before

class _Backend:
    """Just enough of command-signing.md §7.3 to watch the agent's answers land: the node's current
    token and the join's pending one; the first request with the pending token promotes it before it
    is handled (or answers 503 when the promotion cannot commit — ``fail_promotions`` times); an
    unknown token is 403. commandAck moves the command's status as the backend's ackTransition does
    for the cases here (a refusal fails a command the node has not answered)."""

    def __init__(self, current: str, pending: str, fail_promotions: int = 0):
        self.current = current
        self.pending = pending
        self.fail_promotions = fail_promotions
        self.promoted_by: list[str] = []
        self.commands: dict[str, dict] = {}

    def sign(self, message: dict) -> bytes:
        """publishCommand: signed with the CURRENT token, never the pending one (§4.2)."""
        return json.dumps(sign_command(message, self.current, int(time.time() * 1000))[0]).encode("utf-8")

    def __call__(self, endpoint: str, auth, body):
        token = (auth or "").removeprefix("Bearer ")
        if token != self.current:
            if token != self.pending:
                return 403, {"error": "unknown token"}
            if self.fail_promotions > 0:
                self.fail_promotions -= 1
                return 503, {"error": "the promotion could not be committed"}
            self.current, self.pending = self.pending, None
            self.promoted_by.append(f"{endpoint}:{(body or {}).get('type')}")
        if endpoint == "telemetryIngress" and body.get("type") == "commandAck":
            p = body["payload"]
            cmd = self.commands.setdefault(p["commandId"], {"status": "sent"})
            if p.get("reason") and cmd["status"] not in ("sent", "queued", "timeout"):
                return 200, {"success": True, "ack": "stale"}
            cmd.update(status=p["status"], refusal=p.get("reason"), error=p.get("error"))
        return 200, {"success": True}


def _node(tmp_path, monkeypatch, backend, backoff_s=None):
    """The node's command path as main() wires it: the MQTT boundary, its gate with the token from
    config.yaml (the join's), the ack queue and the HTTPS reporter with the same token. ``backoff_s``:
    the ack queue's retry back-off (a short one, so a retry does not take a second)."""
    monkeypatch.setattr(urllib.request, "urlopen", _Ingress(backend))
    reporter = HttpReporter(NODE, auth_token=NEW_TOKEN, base_url=INGRESS)
    mqtt = _mqtt()
    if backoff_s is None:
        acks = wire_command_acks(mqtt, reporter, offline=False)
    else:
        acks = CommandAcks(reporter, backoff_s=backoff_s)
        mqtt.set_ack_sink(acks.submit)
    wire_command_gate(mqtt, reporter, token=NEW_TOKEN, state_dir=str(tmp_path / "state"), offline=False,
                      clock_ok=lambda: True)
    carried_out = []

    def on_command(command):
        carried_out.append(command)
        mqtt.publish_command_ack(command["commandId"], "completed", applied_value=command.get("value"))

    mqtt.set_command_handler(on_command)
    return mqtt, acks, carried_out


class _Msg:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.topic = f"agal/{NODE}/commands"


def _switch(command_id: str, value: int = 1) -> dict:
    return {"commandId": command_id, "type": "setPower", "assetId": "asset_pump1234", "sourceKey": "device.power",
            "value": value, "timestamp": int(time.time() * 1000)}


def test_a_command_signed_with_the_old_token_before_the_promotion_is_refused_and_shown_failed(tmp_path, monkeypatch):
    backend = _Backend(current=OLD_TOKEN, pending=NEW_TOKEN)
    mqtt, acks, carried_out = _node(tmp_path, monkeypatch, backend)

    # The app switches the pump in the seconds after the restart, before the board's first request:
    # the backend still signs with the old token.
    mqtt._on_message(None, None, _Msg(backend.sign(_switch(COMMAND_ID))))
    assert acks.wait_idle(5)

    assert carried_out == [], "never carried out under the wrong token"
    assert backend.commands[COMMAND_ID]["status"] == "failed"
    assert backend.commands[COMMAND_ID]["refusal"] == "bad_signature"
    assert backend.commands[COMMAND_ID]["error"].startswith("refused: bad_signature")
    # The answer itself presented the join's token: it promoted it, then was handled.
    assert backend.promoted_by == ["telemetryIngress:commandAck"] and backend.current == NEW_TOKEN

    # From the promotion on, commands are signed with the new token and carried out.
    again = "Yr4m8UaKnP3qSt6WcX2d"
    mqtt._on_message(None, None, _Msg(backend.sign(_switch(again))))
    assert acks.wait_idle(5)
    assert [c["commandId"] for c in carried_out] == [again]
    assert backend.commands[again] == {"status": "completed", "refusal": None, "error": None}
    acks.close()


def test_the_refusal_answer_is_tried_again_when_the_promotion_cannot_commit(tmp_path, monkeypatch):
    # §7.3: a pending token is accepted only with its promotion; a promotion that cannot commit is
    # 503 and nothing is handled. The refusal answer is a final ack: tried again until it lands.
    backend = _Backend(current=OLD_TOKEN, pending=NEW_TOKEN, fail_promotions=2)
    mqtt, acks, carried_out = _node(tmp_path, monkeypatch, backend, backoff_s=(0.05,))

    mqtt._on_message(None, None, _Msg(backend.sign(_switch(COMMAND_ID))))
    assert acks.wait_idle(5)

    assert carried_out == []
    assert backend.commands[COMMAND_ID]["status"] == "failed" and backend.commands[COMMAND_ID]["refusal"] == "bad_signature"
    assert backend.fail_promotions == 0 and backend.promoted_by == ["telemetryIngress:commandAck"]
    assert acks.posted == 1 and acks.given_up == 0
    acks.close()
