"""The node's trusted now (server_time.py; contracts v1.14.2 review, command-signing.md §5.4).

A command's age is checked on the BACKEND's clock: the Date of the backend's HTTPS answers plus the
monotonic time since; before the first answer an NTP / RTC wall clock; else no trusted now at all,
and the command gate refuses as stale. Also: blocks/clock.py probes on its first call (it answered
"trusted" unprobed for the first minute after boot), and node events take the backend's time too.
No network.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from agal_one_agent.blocks import clock as clock_mod
from agal_one_agent.blocks.clock import SystemClock
from agal_one_agent.http_reporter import HttpReporter
from agal_one_agent.node_events import NodeEvents, wire_node_events
from agal_one_agent.server_time import ServerTime, trusted_now

T = 1792042200000  # 2026-10-15T05:30:00Z
DATE = "Thu, 15 Oct 2026 05:30:00 GMT"


class _Mono:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def test_the_backends_date_plus_the_monotonic_time_since():
    mono = _Mono()
    st = ServerTime(monotonic=mono)
    assert st.now_ms() is None
    assert st.observe({"Date": DATE}, "https://asia-south1-x.cloudfunctions.net/telemetryIngress")
    assert st.now_ms() == T + 500  # the middle of the Date's second
    mono.t += 12.25
    assert st.now_ms() == T + 500 + 12250
    assert st.observe({"Date": "Thu, 15 Oct 2026 05:31:00 GMT"}, "https://x.example/getProgram")
    assert st.now_ms() == T + 60_000 + 500  # the latest answer replaces the reference
    assert st.observations == 2


@pytest.mark.parametrize("headers, url", [
    ({"Date": DATE}, "http://ingress.example/telemetryIngress"),  # not TLS: anyone could have written it
    ({}, "https://ingress.example/telemetryIngress"),
    ({"Date": "not a date"}, "https://ingress.example/telemetryIngress"),
    (None, "https://ingress.example/telemetryIngress"),
    (object(), "https://ingress.example/telemetryIngress"),
], ids=["http", "no Date", "unreadable Date", "no headers", "headers without get"])
def test_an_answer_that_is_not_tls_or_has_no_readable_date_is_ignored(headers, url):
    st = ServerTime()
    assert not st.observe(headers, url) and st.now_ms() is None


def test_trusted_now_prefers_the_backend_then_a_trusted_wall_clock_then_nothing():
    mono = _Mono()
    st = ServerTime(monotonic=mono)
    ok = [False]
    now = trusted_now(st, lambda: ok[0], wall_ms=lambda: 42)
    assert now() is None  # no answer yet, clock not trusted
    ok[0] = True
    assert now() == 42  # before the first answer: the NTP / RTC wall clock
    st.observe({"Date": DATE}, "https://x.example/telemetryIngress")
    assert now() == T + 500  # then always the backend's time, even with a "trusted" wall clock
    ok[0] = False
    assert now() == T + 500

    def broken():
        raise RuntimeError("timedatectl missing")
    assert trusted_now(None, broken)() is None


class _Resp:
    def __init__(self, status=200, date=DATE, body=b"{}"):
        self.status = status
        self.headers = {"Date": date} if date else {}
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_every_https_answer_to_the_reporter_feeds_the_reference(monkeypatch):
    st = ServerTime(monotonic=_Mono())
    rep = HttpReporter("node-1", auth_token="tok", base_url="https://ingress.example/telemetryIngress", server_time=st)
    calls = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        return _Resp(body=json.dumps({"commands": [], "version": 1, "unchanged": True}).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert rep._post_status({"type": "status", "payload": {"nodeUid": "node-1"}}) == 200
    assert rep.fetch_program() is not None
    assert rep.fetch_boot_state("tok") == []
    rep.report_hardware({})
    assert st.observations == 4 and st.now_ms() == T + 500
    assert calls[1].endswith("/getProgram") and calls[3].endswith("/reportHardware")


def test_an_error_answer_counts_too(monkeypatch):
    """A 401 or a 429 from the backend is still the backend's answer, its Date TLS-authenticated."""
    st = ServerTime(monotonic=_Mono())
    rep = HttpReporter("node-1", auth_token="tok", base_url="https://ingress.example/telemetryIngress", server_time=st)

    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Date": DATE}, io.BytesIO(b"{}"))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert rep._post_status({"type": "status", "payload": {}}) == 429
    assert st.now_ms() == T + 500


def test_a_reporter_without_a_reference_or_an_answer_without_headers_still_reports(monkeypatch):
    class Bare:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: Bare())
    assert HttpReporter("n", base_url="https://i.example/telemetryIngress")._post_status({"type": "status"}) == 200
    st = ServerTime()
    assert HttpReporter("n", base_url="https://i.example/telemetryIngress", server_time=st)._post_status({}) == 200
    assert st.now_ms() is None


def test_the_clock_is_probed_on_its_first_call(monkeypatch):
    """Agent 0.3.4 review: the last probe started at 0.0 on a clock that counts from boot, so for the
    first minute after boot clock_ok() said "trusted" without asking."""
    probes = []
    monkeypatch.setattr(clock_mod, "_probe_clock", lambda: probes.append(1) or False)
    monkeypatch.setattr(clock_mod.time, "monotonic", lambda: 12.0)  # 12 s after boot
    c = SystemClock()
    assert c.clock_ok() is False and probes == [1]
    assert c.clock_ok() is False and probes == [1]  # cached for the probe interval


def test_node_events_take_the_backends_time_when_the_clock_is_not_trusted():
    posted = []

    class Rep:
        def report_sensor_event(self, event, *, event_id, timestamp, timestamp_uncertain=False):
            posted.append((timestamp, timestamp_uncertain))
            return 200

    st = ServerTime(monotonic=_Mono())
    events = NodeEvents(Rep(), clock_ok=lambda: False, now_ms=lambda: 7, budget_per_hour=0,
                        trusted_now=trusted_now(st, lambda: False))
    events.submit({"type": "power_restored", "source": "node"})
    st.observe({"Date": DATE}, "https://i.example/telemetryIngress")
    events.submit({"type": "power_restored", "source": "node"})
    assert events.wait_idle(5)
    events.close()
    assert posted == [(7, True), (T + 500, False)]


def test_wire_node_events_uses_the_reference_it_is_given():
    class Mqtt:
        def set_event_sink(self, sink):
            self.sink = sink

    st = ServerTime(monotonic=_Mono())
    st.observe({"Date": DATE}, "https://i.example/telemetryIngress")
    events = wire_node_events(Mqtt(), object(), offline=False, clock_ok=lambda: False, server_time=st)
    assert events.trusted_now() == T + 500
    events.close(0.1)
