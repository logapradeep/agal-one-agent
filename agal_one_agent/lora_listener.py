"""Raw-LoRa star listener (SX127x / SX1276 / SX1278) — ADR-011 v1, DARK.

This is the **v1** LoRa path (ADR-011 §3 decision): a raw point-to-point star
where the Pi node is the single GATEWAY and N battery LEAVES speak only to it.
It is distinct from :mod:`agal_one_agent.lora_bridge`, which half-implements the
full LoRaWAN chain (ChirpStack) retained as the P4 scale-out seam.

**DARK BY DEFAULT (ADR-011 §10 "P1 approved to build DARK; no bench hardware
yet").** When no radio is present, or ``lora.mode != "raw_star"`` / not enabled
in config, this module is inert: :meth:`LoRaListener.start` returns immediately
and nothing touches the SPI bus. Hardware imports (``spidev``) are guarded
exactly like the BNO055 driver — ImportError / unsupported-platform ⇒ mock/dark.

Responsibilities when live (P1, post-launch — the SPI RX loop is scaffolded but
the register-level SX127x driver is a documented TODO gated on the P0 bench
spike):
  * Continuous-RX on the configured channel; on each frame, decode → decrypt →
    dedupe → feed readings into the *same* telemetry path as wired child-device
    readings, via the ``lora.<childShortId>.<portKey>`` sourceKey convention
    (ADR-011 §6 "Landing in the existing stack").
  * 60-second pairing window (§10.2): accept a join-hello from one childId only.
  * Placement-survey samples: stream ``lora_survey_sample`` ingress messages
    (rssi/snr) for the live signal meter (§10.2 / §10.7 item 4).
  * Per-frame rssi/snr stamped on readings; quality-word + link-state computed
    from the shared constants table (§10.3 / §10.4).

Frame format (uplink, ADR-011 §6 — target <=48 B on air):

    | 1 B ver+type | 4 B childShortId | 2 B seq | AES-128-CCM( CBOR body ) | 4 B MIC |

    CBOR body: { 1: [[portIdx, value], ...],   # readings; portIdx→portKey fixed at pairing
                 2: batt_mV, 3: fw_u16, 4: flags }

Crypto (§6): AES-128-CCM per child; ``K_child = HKDF-SHA256(K_farm, childId)``.
``K_farm`` is minted by the pairing callable per gateway node (a NEW secret, NOT
the node's MQTT authToken); it lives in Secret Manager + gateway config, only
its VERSION (``farmKeyId``) is on the node doc. Nonce = childShortId ‖ seq. Keys
never travel over RF — the child's factory ``K_child`` ships in its QR code. See
:data:`FARM_KEY_PLACEHOLDER` for the config wiring; the actual AES-CCM decode is
a documented TODO (needs a bench-validated implementation + CCM test vectors,
ADR-011 R6) and this scaffold decodes only the plaintext header today.

This module has NO hard dependency on radio libs — it is import-safe on any box.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# --- Guarded hardware import (mirrors sensors/bno055.py + handlers/spi_handler.py) ---
try:  # pragma: no cover — exercised only on a real Pi with spidev installed
    import spidev  # type: ignore

    _SPIDEV_AVAILABLE = True
except (ImportError, NotImplementedError, RuntimeError):
    _SPIDEV_AVAILABLE = False
    logger.debug("spidev not available — LoRa listener will stay dark / mock")


# --- Frame constants (ADR-011 §6) ---
FRAME_HEADER_LEN = 7          # 1 B ver+type + 4 B childShortId + 2 B seq
FRAME_MIC_LEN = 4             # trailing 4 B MIC
MAX_FRAME_LEN = 48            # target on-air max
FRAME_VERSION = 0x01          # high nibble of byte 0
FRAME_TYPE_UPLINK = 0x0       # low nibble: data uplink
FRAME_TYPE_JOIN = 0x1         # join-hello
FRAME_TYPE_SURVEY = 0x2       # placement-survey ping

#: Per-farm AES-128 key placeholder. The real key (``K_farm``) is minted per
#: gateway by the pairing callable and injected via gateway config / Secret
#: Manager; the node doc carries only ``farmKeyId`` (the version). NEVER the
#: node's MQTT authToken. 16 zero bytes here = "no key configured ⇒ dark".
FARM_KEY_PLACEHOLDER = b"\x00" * 16

PAIRING_WINDOW_SEC = 60       # §10.2: gateway listens for one childId's join

# --- Quality-word thresholds (ADR-011 §10.3, SF-independent, shared table) ---
# snrFloor and sensitivity by spreading factor @125 kHz (SX1276 datasheet).
SNR_FLOOR_BY_SF = {7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}
SENSITIVITY_BY_SF = {7: -123.0, 8: -126.0, 9: -129.0, 10: -132.0, 11: -134.5, 12: -137.0}
# Quality bands: (min rssi_headroom dB, min snr_margin dB) — worse of the two wins.
QUALITY_BANDS = [
    ("excellent", 25.0, 15.0),
    ("ok", 15.0, 8.0),
    ("weak", 6.0, 3.0),
]  # below "weak" ⇒ "none"

# --- Link-state freshness multipliers (ADR-011 §10.4) ---
LINK_LIVE_MULT = 1.5   # age <= 1.5 × expectedIntervalS ⇒ live
LINK_LOST_MULT = 4.0   # age  > 4   × expectedIntervalS ⇒ lost (else stale)


def link_quality_word(rssi_dbm: float, snr_db: float, sf: int = 9) -> str:
    """Map a gateway-side (rssi, snr) measurement to a quality word.

    SF-independent rule (§10.3): compute headroom above sensitivity and margin
    above the SNR floor for the SF, take the WORSE of the two, map to a band.
    Sized so "excellent" survives a 15 dB worst-monsoon fade with >=10 dB spare.
    """
    sens = SENSITIVITY_BY_SF.get(sf, SENSITIVITY_BY_SF[9])
    floor = SNR_FLOOR_BY_SF.get(sf, SNR_FLOOR_BY_SF[9])
    headroom = rssi_dbm - sens
    snr_margin = snr_db - floor
    for word, min_headroom, min_snr in QUALITY_BANDS:
        if headroom >= min_headroom and snr_margin >= min_snr:
            return word
    return "none"


def link_state(age_sec: float, expected_interval_sec: float) -> str:
    """Tri-state freshness (§10.4): live | stale | lost, parameterized by the
    leaf's expected uplink interval (the LoRa analog of the S-149 90 s pill)."""
    if age_sec <= LINK_LIVE_MULT * expected_interval_sec:
        return "live"
    if age_sec <= LINK_LOST_MULT * expected_interval_sec:
        return "stale"
    return "lost"


@dataclass
class LoRaFrame:
    """A decoded leaf frame. ``readings`` map portIdx→value; the portIdx→portKey
    table is fixed at pairing time (§6), so the listener resolves keys against
    the paired child's config before emitting telemetry."""

    version: int
    frame_type: int
    child_short_id: int
    seq: int
    readings: list[tuple[int, float]] = field(default_factory=list)  # (portIdx, value)
    battery_mv: Optional[int] = None
    fw_u16: Optional[int] = None
    flags: int = 0
    rssi_dbm: float = 0.0
    snr_db: float = 0.0


def decode_frame_header(raw: bytes) -> Optional[LoRaFrame]:
    """Decode the plaintext frame header (ver+type, childShortId, seq).

    Returns None on a malformed/too-short frame. The encrypted CBOR body + MIC
    are NOT decoded here — that needs the bench-validated AES-128-CCM path
    (documented TODO, ADR-011 R6). This header decode is what dedupe + child
    lookup key off, and it is fully testable in mock mode.
    """
    if len(raw) < FRAME_HEADER_LEN + FRAME_MIC_LEN:
        logger.debug("LoRa frame too short: %d bytes", len(raw))
        return None
    if len(raw) > MAX_FRAME_LEN:
        logger.debug("LoRa frame over max on-air length: %d bytes", len(raw))
        return None
    ver_type = raw[0]
    version = (ver_type >> 4) & 0x0F
    frame_type = ver_type & 0x0F
    if version != FRAME_VERSION:
        logger.debug("Unknown LoRa frame version: %d", version)
        return None
    child_short_id = struct.unpack(">I", raw[1:5])[0]
    seq = struct.unpack(">H", raw[5:7])[0]
    return LoRaFrame(
        version=version,
        frame_type=frame_type,
        child_short_id=child_short_id,
        seq=seq,
    )


class LoRaListener:
    """Raw-LoRa star gateway listener. Inert unless a radio is present + enabled.

    Args:
        lora_config: the parsed ``lora`` config block (needs ``mode == 'raw_star'``
            and ``enabled`` truthy to go live).
        on_readings: callback(nodeUid_suffix, readings) — readings enter the same
            telemetry path as wired children (buffered + live), sourceKey
            ``lora.<childShortId>.<portKey>``.
        on_survey_sample: callback(sample_dict) — emits a ``lora_survey_sample``
            for the live signal meter (§10.2).
        on_child_status: callback(status_dict) — per-leaf health (battery,
            rssi/snr, linkState) → ``lora_child_status`` ingress type.
    """

    def __init__(
        self,
        lora_config,
        on_readings: Optional[Callable[[str, list[dict]], None]] = None,
        on_survey_sample: Optional[Callable[[dict], None]] = None,
        on_child_status: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.config = lora_config
        self._on_readings = on_readings
        self._on_survey_sample = on_survey_sample
        self._on_child_status = on_child_status
        self._spi = None
        self._running = False
        # Replay/dedupe: last accepted seq per childShortId, 16-wide reorder
        # window (§6). Purely in-memory — a gateway reboot re-syncs from the
        # first frame after boot.
        self._last_seq: dict[int, int] = {}
        self._reorder_window = 16
        # Pairing window state (§10.2).
        self._pairing_child: Optional[int] = None
        self._pairing_until: float = 0.0

    # ---- Enablement ---------------------------------------------------------

    @property
    def is_enabled(self) -> bool:
        """True only when a raw-star radio is configured AND enabled AND spidev
        is importable. This is the single gate that keeps the module dark."""
        cfg = self.config
        if cfg is None:
            return False
        mode = getattr(cfg, "mode", None) or (cfg.get("mode") if isinstance(cfg, dict) else None)
        enabled = getattr(cfg, "enabled", None)
        if enabled is None and isinstance(cfg, dict):
            enabled = cfg.get("enabled")
        return bool(mode == "raw_star" and enabled and _SPIDEV_AVAILABLE)

    def start(self) -> None:
        """Start continuous-RX if enabled; otherwise return immediately (dark)."""
        if not self.is_enabled:
            logger.info(
                "LoRa raw-star listener DARK (radio present=%s, mode/enabled gate not met) — "
                "no SPI activity", _SPIDEV_AVAILABLE,
            )
            return
        # --- LIVE path (P1, gated on the P0 bench spike) ---
        # The SX127x register-level driver (SPI init, RegOpMode → RXCONTINUOUS,
        # DIO0 IRQ on RxDone, FIFO read, RegPktSnrValue/RegPktRssiValue) is a
        # documented TODO — it must be validated against real hardware before it
        # can be trusted (ADR-011 R1: no LoRa hardware has ever been field-
        # verified in this stack). Scaffolded so wiring it up is additive.
        self._spi = spidev.SpiDev()
        self._running = True
        logger.warning(
            "LoRa raw-star listener enabled but SX127x driver is a scaffold — "
            "RX loop not yet wired to hardware (bench-spike gated, ADR-011 P0)",
        )

    def stop(self) -> None:
        self._running = False
        if self._spi is not None:
            try:
                self._spi.close()
            except Exception:  # noqa: BLE001
                pass
            self._spi = None

    # ---- Pairing (§10.2) ----------------------------------------------------

    def open_pairing_window(self, child_short_id: int,
                            duration_sec: int = PAIRING_WINDOW_SEC) -> None:
        """Open a time-boxed pairing window for exactly one childId (§10.2 step 3).

        During the window the listener accepts a join-hello from this child only.
        Called by the MQTT command handler when the backend forwards a
        ``pairLoraChild`` registration command."""
        self._pairing_child = child_short_id
        self._pairing_until = time.monotonic() + duration_sec
        logger.info("LoRa pairing window open for child %d (%ds)",
                    child_short_id, duration_sec)

    def is_pairing_open(self, child_short_id: Optional[int] = None) -> bool:
        if self._pairing_child is None or time.monotonic() > self._pairing_until:
            return False
        if child_short_id is None:
            return True
        return child_short_id == self._pairing_child

    # ---- Frame handling (testable without hardware) -------------------------

    def _accept_seq(self, child_short_id: int, seq: int) -> bool:
        """Replay/dedupe: accept strictly-increasing seq with a 16-wide reorder
        window; drop duplicates and stale replays (§6)."""
        last = self._last_seq.get(child_short_id)
        if last is None:
            self._last_seq[child_short_id] = seq
            return True
        # 16-bit seq wraps; treat within-window forward jumps as new.
        forward = (seq - last) & 0xFFFF
        if forward == 0:
            return False  # duplicate
        if forward <= self._reorder_window or forward < 0x8000:
            self._last_seq[child_short_id] = seq
            return True
        return False  # far-past replay outside the reorder window

    def handle_raw_frame(self, raw: bytes, rssi_dbm: float, snr_db: float,
                         sf: int = 9, child_ports: Optional[dict] = None) -> Optional[dict]:
        """Process one received frame end-to-end (minus AES-CCM body decode).

        ``child_ports`` maps ``childShortId -> {portIdx: {portKey, kindUri, unit}}``
        (the fixed-at-pairing table). Returns the emitted child-status dict (or
        None if the frame was dropped) — mainly for tests; the live effect is via
        the ``on_*`` callbacks.
        """
        frame = decode_frame_header(raw)
        if frame is None:
            return None
        frame.rssi_dbm = rssi_dbm
        frame.snr_db = snr_db

        # Survey pings: stream rssi/snr for the live signal meter; no dedupe.
        if frame.frame_type == FRAME_TYPE_SURVEY:
            sample = {
                "childShortId": frame.child_short_id,
                "rssi": rssi_dbm,
                "snr": snr_db,
                "sf": sf,
                "quality": link_quality_word(rssi_dbm, snr_db, sf),
                "at": int(time.time() * 1000),
            }
            if self._on_survey_sample:
                self._on_survey_sample(sample)
            return None

        # Join-hello: only honoured inside an open pairing window for this child.
        if frame.frame_type == FRAME_TYPE_JOIN:
            if not self.is_pairing_open(frame.child_short_id):
                logger.debug("Join from %d outside pairing window — ignored",
                             frame.child_short_id)
                return None
            self._last_seq[frame.child_short_id] = frame.seq
            logger.info("LoRa child %d joined", frame.child_short_id)
            # Fall through to status emission below.

        # Data uplink: dedupe on seq.
        if frame.frame_type == FRAME_TYPE_UPLINK:
            if not self._accept_seq(frame.child_short_id, frame.seq):
                logger.debug("Dropped duplicate/replay frame seq=%d child=%d",
                             frame.seq, frame.child_short_id)
                return None
            # NOTE: readings come from the AES-128-CCM(CBOR body) — decoded once
            # the crypto path lands. Header-only frames yield no readings today.
            readings = self._readings_to_telemetry(frame, child_ports)
            if readings and self._on_readings:
                self._on_readings(str(frame.child_short_id), readings)

        status = {
            "childShortId": frame.child_short_id,
            "seq": frame.seq,
            "rssi": rssi_dbm,
            "snr": snr_db,
            "quality": link_quality_word(rssi_dbm, snr_db, sf),
            "lastSeenAt": int(time.time() * 1000),
        }
        if frame.battery_mv is not None:
            status["batteryMv"] = frame.battery_mv
        if frame.fw_u16 is not None:
            status["fw"] = frame.fw_u16
        if self._on_child_status:
            self._on_child_status(status)
        return status

    @staticmethod
    def _readings_to_telemetry(frame: LoRaFrame, child_ports: Optional[dict]) -> list[dict]:
        """Resolve (portIdx, value) pairs to canonical telemetry rows using the
        pairing-time port map. sourceKey convention: ``lora.<childShortId>.<portKey>``
        (ADR-011 §6 / §10.1 — the path is always displayed)."""
        rows: list[dict] = []
        ports = (child_ports or {}).get(frame.child_short_id, {})
        for port_idx, value in frame.readings:
            meta = ports.get(port_idx, {})
            port_key = meta.get("portKey", f"p{port_idx}")
            row = {
                "sourceKey": f"lora.{frame.child_short_id}.{port_key}",
                "value": value,
                # rssi/snr ride along on the reading (§10.4 per-asset link info).
                "rssi": frame.rssi_dbm,
                "snr": frame.snr_db,
            }
            if meta.get("kindUri"):
                row["kindUri"] = meta["kindUri"]
            if meta.get("unit"):
                row["unit"] = meta["unit"]
            rows.append(row)
        return rows
