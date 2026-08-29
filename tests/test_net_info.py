"""Tests for MAC detection (net_info) + that status reports carry the MAC."""
import json
from unittest.mock import MagicMock

import pytest

from agal_one_agent import net_info
from agal_one_agent.config import MqttConfig
from agal_one_agent.mqtt_client import AgalOneMqttClient


@pytest.fixture(autouse=True)
def _reset_mac_cache():
    net_info._cached = None
    yield
    net_info._cached = None


class _NoSysNet:
    """Stand-in for _SYS_NET so the /sys scan is deterministic off-Linux + on CI."""
    def is_dir(self):
        return False


def test_get_primary_mac_prefers_eth0(monkeypatch):
    monkeypatch.setattr(net_info, "_read_iface_mac",
                        lambda iface: "aa:bb:cc:dd:ee:ff" if iface == "eth0" else None)
    assert net_info.get_primary_mac() == "aa:bb:cc:dd:ee:ff"


def test_get_primary_mac_uuid_fallback(monkeypatch):
    monkeypatch.setattr(net_info, "_read_iface_mac", lambda iface: None)
    monkeypatch.setattr(net_info, "_SYS_NET", _NoSysNet())
    monkeypatch.setattr(net_info, "_from_uuid_getnode", lambda: "12:34:56:78:9a:bc")
    assert net_info.get_primary_mac() == "12:34:56:78:9a:bc"


def test_get_primary_mac_caches(monkeypatch):
    calls = []

    def fake_read(iface):
        calls.append(iface)
        return "aa:bb:cc:dd:ee:ff" if iface == "eth0" else None

    monkeypatch.setattr(net_info, "_read_iface_mac", fake_read)
    assert net_info.get_primary_mac() == "aa:bb:cc:dd:ee:ff"
    n = len(calls)
    net_info.get_primary_mac()  # cached — must not re-scan
    assert len(calls) == n


def test_publish_status_includes_mac(monkeypatch):
    monkeypatch.setattr(net_info, "get_primary_mac", lambda: "b8:27:eb:01:02:03")
    client = AgalOneMqttClient(MqttConfig(
        broker="b", username="node-1", status_topic="agal/node-1/status",
    ))
    client._client = MagicMock()
    client._connected = True
    client.publish_status(online=True, uptime=5)

    call = client._client.publish.call_args
    payload = json.loads(call.args[1])
    assert payload["payload"]["mac"] == "b8:27:eb:01:02:03"
    assert payload["payload"]["nodeUid"] == "node-1"
