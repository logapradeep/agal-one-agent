"""Adaptive live-cadence tests (ADR-013 §9-D3) — deterministic clock, no timers.

Covers the 10 s-while-watched / 60 s-idle switch, TTL decay, explicit stop, and
the config default. Uses an injectable monotonic clock so no real sleeping.
"""

from __future__ import annotations

from agal_one_agent.live_cadence import LiveCadenceController


class _Clock:
    """Manually-advanced monotonic clock."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _ctrl(default_watching=False, **kw) -> tuple[LiveCadenceController, _Clock]:
    clk = _Clock()
    c = LiveCadenceController(
        watching_interval_sec=kw.get("watch", 10),
        idle_interval_sec=kw.get("idle", 60),
        watch_ttl_sec=kw.get("ttl", 90),
        default_watching=default_watching,
        _clock=clk,
    )
    return c, clk


def test_idle_by_default():
    c, _ = _ctrl()
    assert c.is_watching() is False
    assert c.current_interval() == 60


def test_watch_switches_to_fast_cadence():
    c, _ = _ctrl()
    c.set_watching(True)
    assert c.is_watching() is True
    assert c.current_interval() == 10


def test_watch_decays_after_ttl():
    c, clk = _ctrl(ttl=90)
    c.set_watching(True)
    assert c.current_interval() == 10
    clk.advance(89)
    assert c.current_interval() == 10   # still inside TTL
    clk.advance(2)                       # now 91 s since signal
    assert c.is_watching() is False
    assert c.current_interval() == 60    # decayed to idle


def test_watch_refresh_extends_ttl():
    c, clk = _ctrl(ttl=90)
    c.set_watching(True)
    clk.advance(80)
    c.set_watching(True)                 # refresh before expiry
    clk.advance(80)                      # 80 s since refresh (< 90)
    assert c.is_watching() is True
    assert c.current_interval() == 10


def test_explicit_stop_watching_drops_to_idle_immediately():
    c, _ = _ctrl()
    c.set_watching(True)
    assert c.current_interval() == 10
    c.set_watching(False)
    assert c.is_watching() is False
    assert c.current_interval() == 60


def test_default_watching_starts_fast():
    c, _ = _ctrl(default_watching=True)
    assert c.is_watching() is True
    assert c.current_interval() == 10


def test_custom_intervals_respected():
    c, _ = _ctrl(watch=5, idle=120)
    assert c.current_interval() == 120
    c.set_watching(True)
    assert c.current_interval() == 5
