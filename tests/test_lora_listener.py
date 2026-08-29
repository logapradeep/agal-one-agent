"""Raw-LoRa listener tests (ADR-011 v1) — mock mode, no radio, DARK path.

Covers: frame-header decode (ver/type/childShortId/seq), malformed rejection,
dedupe/replay window, survey-sample emission, child-status emission, sourceKey
convention, quality-word thresholds (§10.3), link-state freshness (§10.4), and
the dark-by-default gate.
"""

from __future__ import annotations

import struct

from agal_one_agent.config import LoRaConfig, LoRaRadioConfig
from agal_one_agent.lora_listener import (
    LoRaListener,
    decode_frame_header,
    link_quality_word,
    link_state,
    FRAME_VERSION,
    FRAME_TYPE_UPLINK,
    FRAME_TYPE_JOIN,
    FRAME_TYPE_SURVEY,
    FRAME_HEADER_LEN,
    FRAME_MIC_LEN,
    MAX_FRAME_LEN,
    PAIRING_WINDOW_SEC,
)


def _frame(frame_type: int, child: int, seq: int, body_len: int = 4) -> bytes:
    """Build a plaintext-header frame with a filler body + 4 B MIC."""
    ver_type = (FRAME_VERSION << 4) | (frame_type & 0x0F)
    header = bytes([ver_type]) + struct.pack(">I", child) + struct.pack(">H", seq)
    return header + b"\x00" * body_len + b"\x00" * FRAME_MIC_LEN


# ----- frame header decode ----------------------------------------------------


def test_decode_valid_uplink_header():
    f = decode_frame_header(_frame(FRAME_TYPE_UPLINK, 0xDEADBEEF, 42))
    assert f is not None
    assert f.version == FRAME_VERSION
    assert f.frame_type == FRAME_TYPE_UPLINK
    assert f.child_short_id == 0xDEADBEEF
    assert f.seq == 42


def test_decode_rejects_too_short():
    assert decode_frame_header(b"\x10\x00\x00") is None


def test_decode_rejects_over_max_length():
    too_long = _frame(FRAME_TYPE_UPLINK, 1, 1, body_len=MAX_FRAME_LEN)
    assert len(too_long) > MAX_FRAME_LEN
    assert decode_frame_header(too_long) is None


def test_decode_rejects_unknown_version():
    raw = bytearray(_frame(FRAME_TYPE_UPLINK, 1, 1))
    raw[0] = (0xF << 4) | FRAME_TYPE_UPLINK   # version 15
    assert decode_frame_header(bytes(raw)) is None


def test_min_length_boundary_accepts():
    # Exactly header + MIC (body_len=0) is the minimum valid frame.
    raw = _frame(FRAME_TYPE_UPLINK, 7, 1, body_len=0)
    assert len(raw) == FRAME_HEADER_LEN + FRAME_MIC_LEN
    assert decode_frame_header(raw) is not None


# ----- dedupe / replay window -------------------------------------------------


def test_dedupe_drops_duplicate_seq():
    lis = LoRaListener(LoRaConfig())
    assert lis._accept_seq(1, 10) is True
    assert lis._accept_seq(1, 10) is False   # exact duplicate
    assert lis._accept_seq(1, 11) is True    # forward


def test_replay_outside_window_dropped():
    lis = LoRaListener(LoRaConfig())
    lis._accept_seq(1, 1000)
    # A far-past seq (well behind, outside the 16-wide reorder window) is a replay.
    assert lis._accept_seq(1, 500) is False


def test_per_child_seq_independent():
    lis = LoRaListener(LoRaConfig())
    assert lis._accept_seq(1, 5) is True
    assert lis._accept_seq(2, 5) is True     # different child, independent


# ----- survey samples (§10.2) -------------------------------------------------


def test_survey_frame_emits_sample_with_quality():
    samples = []
    lis = LoRaListener(LoRaConfig(), on_survey_sample=samples.append)
    out = lis.handle_raw_frame(_frame(FRAME_TYPE_SURVEY, 0xAA, 1),
                               rssi_dbm=-104, snr_db=3, sf=9)
    assert out is None                       # survey doesn't emit child-status
    assert len(samples) == 1
    s = samples[0]
    assert s["childShortId"] == 0xAA
    assert s["rssi"] == -104
    assert s["snr"] == 3
    assert s["quality"] == "excellent"       # -104/+3 @SF9 ⇒ excellent (§10.3)
    assert "at" in s


# ----- child status + readings path -------------------------------------------


def test_uplink_emits_child_status():
    statuses = []
    lis = LoRaListener(LoRaConfig(), on_child_status=statuses.append)
    status = lis.handle_raw_frame(_frame(FRAME_TYPE_UPLINK, 0xBB, 1),
                                  rssi_dbm=-110, snr_db=-2, sf=9)
    assert status is not None
    assert status["childShortId"] == 0xBB
    assert status["quality"] == "ok"         # -110/-2 @SF9 ⇒ ok (headroom 19, snrM 10.5)
    assert len(statuses) == 1


def test_readings_use_lora_sourcekey_convention():
    got = []
    lis = LoRaListener(LoRaConfig(), on_readings=lambda c, r: got.append((c, r)))
    # Inject a decoded reading directly (the CBOR body decode is a documented
    # TODO; exercise the sourceKey/port-map resolution path).
    rows = LoRaListener._readings_to_telemetry(
        type("F", (), {"child_short_id": 0xCC, "readings": [(0, 42.0)],
                       "rssi_dbm": -100, "snr_db": 6})(),
        {0xCC: {0: {"portKey": "soil_moisture", "kindUri": "soil_moisture", "unit": "pct"}}},
    )
    assert rows[0]["sourceKey"] == "lora.204.soil_moisture"   # 0xCC = 204
    assert rows[0]["value"] == 42.0
    assert rows[0]["kindUri"] == "soil_moisture"
    assert rows[0]["unit"] == "pct"
    assert rows[0]["rssi"] == -100 and rows[0]["snr"] == 6


def test_readings_fallback_portkey_when_unmapped():
    rows = LoRaListener._readings_to_telemetry(
        type("F", (), {"child_short_id": 5, "readings": [(3, 1.0)],
                       "rssi_dbm": 0, "snr_db": 0})(),
        None,
    )
    assert rows[0]["sourceKey"] == "lora.5.p3"


# ----- pairing window (§10.2) -------------------------------------------------


def test_pairing_window_gates_join():
    joins = []
    lis = LoRaListener(LoRaConfig(), on_child_status=joins.append)
    # Join outside a window is ignored.
    assert lis.handle_raw_frame(_frame(FRAME_TYPE_JOIN, 0x99, 1), -100, 5) is None
    assert joins == []
    # Open a window for this child; now the join is accepted.
    lis.open_pairing_window(0x99, PAIRING_WINDOW_SEC)
    assert lis.is_pairing_open(0x99) is True
    status = lis.handle_raw_frame(_frame(FRAME_TYPE_JOIN, 0x99, 5), -100, 5)
    assert status is not None
    assert status["childShortId"] == 0x99


def test_pairing_window_only_for_named_child():
    lis = LoRaListener(LoRaConfig())
    lis.open_pairing_window(0x01)
    assert lis.is_pairing_open(0x01) is True
    assert lis.is_pairing_open(0x02) is False


# ----- quality-word thresholds (§10.3 canonical) ------------------------------


def test_quality_word_bands_sf9():
    # Canonical §10.3 rule = worse of (rssi headroom vs sensitivity, snr margin
    # vs SNR floor). SF9: sensitivity -129, SNR floor -12.5. Values chosen
    # clearly inside each band (the ADR's SF9 "shortcut" column is an approximate
    # readout of this formula and can differ by <1 dB at the exact boundary —
    # the margin formula is the authoritative one every surface implements).
    assert link_quality_word(-104, 3, 9) == "excellent"   # headroom 25, snrM 15.5
    assert link_quality_word(-110, -2, 9) == "ok"         # headroom 19, snrM 10.5
    assert link_quality_word(-118, -8, 9) == "weak"       # headroom 11, snrM 4.5
    assert link_quality_word(-126, -14, 9) == "none"      # below weak


def test_quality_word_takes_worse_of_two():
    # Great RSSI but terrible SNR ⇒ dragged down (worse of the two wins).
    assert link_quality_word(-90, -15, 9) == "none"


def test_quality_word_sf_dependent():
    # SF12 sensitivity is much better (-137), so the same RSSI reads stronger.
    assert link_quality_word(-110, 2, 12) == "excellent"   # headroom 27, snrM 22
    assert link_quality_word(-118, 0, 9) != "excellent"    # headroom 11 @SF9


# ----- link-state freshness (§10.4) -------------------------------------------


def test_link_state_thresholds():
    interval = 900   # 15 min
    assert link_state(10, interval) == "live"
    assert link_state(1.5 * interval, interval) == "live"     # boundary
    assert link_state(1.5 * interval + 1, interval) == "stale"
    assert link_state(4 * interval, interval) == "stale"      # boundary
    assert link_state(4 * interval + 1, interval) == "lost"


# ----- dark-by-default gate ---------------------------------------------------


def test_listener_dark_without_spidev():
    # On a dev box spidev is absent ⇒ is_enabled must be False even when
    # mode=raw_star + enabled=True, and start() must not touch hardware.
    lis = LoRaListener(LoRaConfig(mode="raw_star", enabled=True,
                                  radio=LoRaRadioConfig()))
    assert lis.is_enabled is False
    lis.start()                # returns immediately, no exception
    assert lis._spi is None
    lis.stop()


def test_listener_dark_when_mode_not_raw_star():
    lis = LoRaListener(LoRaConfig(mode="none", enabled=True))
    assert lis.is_enabled is False


def test_listener_dark_when_disabled():
    lis = LoRaListener(LoRaConfig(mode="raw_star", enabled=False))
    assert lis.is_enabled is False


def test_listener_handles_none_config():
    lis = LoRaListener(None)
    assert lis.is_enabled is False
    lis.start()   # no crash
