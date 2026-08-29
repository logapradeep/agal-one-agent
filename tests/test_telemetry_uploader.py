"""Uploader tests (ADR-013 P0 §5.3) — mock reporter, no network.

Covers: successful drain marks rows sent + prunes; failed send leaves rows
pending and applies exponential backoff; backoff resets on success; drain wakes.
"""

from __future__ import annotations

from agal_one_agent.telemetry_buffer import TelemetryBuffer, TelemetryBatchConfig
from agal_one_agent.telemetry_uploader import (
    TelemetryUploader,
    BACKOFF_BASE_SEC,
    BACKOFF_CAP_SEC,
)


class _FakeReporter:
    """Stands in for HttpReporter.report_telemetry_batch."""

    def __init__(self, succeed: bool = True):
        self.succeed = succeed
        self.calls = []

    def report_telemetry_batch(self, batch_id, readings, boot_session_id=""):
        self.calls.append((batch_id, list(readings), boot_session_id))
        return self.succeed


def _buf_with(n: int, **cfg) -> TelemetryBuffer:
    b = TelemetryBuffer(db_path=":memory:", config=TelemetryBatchConfig(**cfg))
    b.append([{"sourceKey": f"p{i}", "value": float(i)} for i in range(n)])
    return b


def test_successful_drain_marks_sent_and_empties():
    b = _buf_with(5, retention_hours=0)   # sent rows prune immediately
    rep = _FakeReporter(succeed=True)
    up = TelemetryUploader(b, rep)
    sent = up.drain_all()
    assert sent == 5
    assert len(rep.calls) == 1
    assert b.pending_count() == 0


def test_batch_carries_boot_session_id():
    b = _buf_with(2)
    rep = _FakeReporter(succeed=True)
    TelemetryUploader(b, rep).drain_all()
    _batch_id, _readings, boot_session = rep.calls[0]
    assert boot_session == b.boot_session_id


def test_failed_send_leaves_rows_pending_and_backs_off():
    b = _buf_with(3)
    rep = _FakeReporter(succeed=False)
    up = TelemetryUploader(b, rep)
    assert up._backoff == BACKOFF_BASE_SEC
    sent = up.drain_all()
    assert sent == 0
    assert b.pending_count() == 3          # nothing marked sent on failure
    assert up._backoff == BACKOFF_BASE_SEC * 2   # doubled once


def test_backoff_grows_exponentially_and_caps():
    b = _buf_with(1)
    up = TelemetryUploader(b, _FakeReporter(succeed=False))
    prev = up._backoff
    for _ in range(20):
        up.drain_all()
        assert up._backoff >= prev
        prev = up._backoff
    assert up._backoff == BACKOFF_CAP_SEC   # clamped at 5 min


def test_backoff_resets_after_success():
    b = _buf_with(2)
    rep = _FakeReporter(succeed=False)
    up = TelemetryUploader(b, rep)
    up.drain_all()
    assert up._backoff > BACKOFF_BASE_SEC
    rep.succeed = True
    up.drain_all()
    assert up._backoff == BACKOFF_BASE_SEC


def test_outage_then_recovery_delivers_everything():
    # Simulate: readings buffered during an outage, then the network returns.
    b = _buf_with(7, retention_hours=0)
    rep = _FakeReporter(succeed=False)
    up = TelemetryUploader(b, rep)
    up.drain_all()                 # outage: nothing lands
    assert b.pending_count() == 7
    rep.succeed = True
    up.drain_all()                 # recovery: full backlog drains
    assert b.pending_count() == 0
    delivered = sum(len(readings) for _, readings, _ in rep.calls if _)
    # The final successful call carried all 7.
    assert rep.calls[-1][1] and len(rep.calls[-1][1]) == 7


def test_current_backoff_jitter_within_bounds():
    b = _buf_with(1)
    up = TelemetryUploader(b, _FakeReporter(succeed=False))
    up.drain_all()   # backoff now 2s
    for _ in range(50):
        j = up.current_backoff
        assert 0.0 <= j <= up._backoff
