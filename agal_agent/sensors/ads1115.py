"""ADS1115 16-bit I2C ADC driver.

Single-shot conversion mode with configurable PGA and channel. We don't use
continuous mode because we want deterministic reads and we sample at multiple
channels off one ADC.

Datasheet: https://www.ti.com/lit/ds/symlink/ads1115.pdf

Wiring (single-ended, channel 0):
    ADS1115 VDD  → RPi 3.3V (or 5V — both work, but 3.3V matches RPi logic)
    ADS1115 GND  → RPi GND
    ADS1115 SDA  → RPi GPIO 2 (SDA1, pin 3)
    ADS1115 SCL  → RPi GPIO 3 (SCL1, pin 5)
    ADS1115 ADDR → GND  (sets I2C address 0x48; tie to VDD for 0x49)
    ADS1115 A0   → ACS758 V_OUT
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import smbus2

    _SMBUS_AVAILABLE = True
except ImportError:
    _SMBUS_AVAILABLE = False
    logger.warning("smbus2 not available — ADS1115 reads will be simulated")


# ---- ADS1115 register addresses -------------------------------------------
_REG_CONVERSION = 0x00
_REG_CONFIG = 0x01

# ---- Config register bit fields (16-bit) ----------------------------------
_CFG_OS_SINGLE = 0x8000  # bit 15: start single-shot
_CFG_MUX_SINGLE = {0: 0x4000, 1: 0x5000, 2: 0x6000, 3: 0x7000}  # bits 14..12
_CFG_PGA = {
    6.144: 0x0000,
    4.096: 0x0200,
    2.048: 0x0400,
    1.024: 0x0600,
    0.512: 0x0800,
    0.256: 0x0A00,
}
_CFG_MODE_SINGLE = 0x0100  # bit 8: single-shot
_CFG_DR = {
    8: 0x0000,
    16: 0x0020,
    32: 0x0040,
    64: 0x0060,
    128: 0x0080,
    250: 0x00A0,
    475: 0x00C0,
    860: 0x00E0,
}
_CFG_COMP_DISABLE = 0x0003

# Time to wait for a conversion at each data rate. Conservative
# overestimate (1.5×) to allow for I2C latency.
_CONVERSION_DELAY_S = {
    8: 0.190,
    16: 0.095,
    32: 0.048,
    64: 0.024,
    128: 0.012,
    250: 0.006,
    475: 0.004,
    860: 0.002,
}


class Ads1115:
    """Minimal single-shot ADS1115 driver.

    Reuse one instance per (bus, address) combination. Reads via `.read_v(channel)`
    return the voltage at the channel in volts. Simulation mode returns 0.0.
    """

    def __init__(
        self,
        bus_number: int = 1,
        address: int = 0x48,
        gain_v: float = 4.096,
        data_rate_sps: int = 860,
    ) -> None:
        if gain_v not in _CFG_PGA:
            raise ValueError(f"Unsupported ADS1115 gain: {gain_v}V")
        if data_rate_sps not in _CFG_DR:
            raise ValueError(f"Unsupported ADS1115 data rate: {data_rate_sps} SPS")

        self.address = address
        self.gain_v = gain_v
        self.data_rate_sps = data_rate_sps
        self._lsb_v = (2 * gain_v) / 65536.0  # signed 16-bit full scale
        self._conv_delay_s = _CONVERSION_DELAY_S[data_rate_sps]

        if _SMBUS_AVAILABLE:
            self._bus = smbus2.SMBus(bus_number)
        else:
            self._bus = None

    def read_v(self, channel: int) -> float:
        """Read voltage at the given channel (0..3) in volts."""
        if channel not in (0, 1, 2, 3):
            raise ValueError(f"channel must be 0..3, got {channel}")

        if not _SMBUS_AVAILABLE or self._bus is None:
            return 0.0  # simulation fallback

        cfg = (
            _CFG_OS_SINGLE
            | _CFG_MUX_SINGLE[channel]
            | _CFG_PGA[self.gain_v]
            | _CFG_MODE_SINGLE
            | _CFG_DR[self.data_rate_sps]
            | _CFG_COMP_DISABLE
        )
        # Write config (big-endian; smbus2 expects little-endian byte order in
        # write_word_data, so swap).
        cfg_swapped = ((cfg & 0xFF) << 8) | ((cfg >> 8) & 0xFF)
        try:
            self._bus.write_word_data(self.address, _REG_CONFIG, cfg_swapped)
            time.sleep(self._conv_delay_s)
            raw = self._bus.read_word_data(self.address, _REG_CONVERSION)
            # Swap back from little-endian to host
            raw = ((raw & 0xFF) << 8) | ((raw >> 8) & 0xFF)
            # Signed 16-bit
            if raw & 0x8000:
                raw -= 1 << 16
            return raw * self._lsb_v
        except Exception as e:  # noqa: BLE001 — bus errors are common on noisy field hardware
            logger.warning("ADS1115 read failed (addr=0x%02x ch=%d): %s", self.address, channel, e)
            return float("nan")

    def close(self) -> None:
        if _SMBUS_AVAILABLE and self._bus is not None:
            try:
                self._bus.close()
            except Exception:  # noqa: BLE001
                pass
            self._bus = None


def is_hardware_available() -> bool:
    """True iff smbus2 is importable. Useful for tests that want to skip on dev box."""
    return _SMBUS_AVAILABLE
