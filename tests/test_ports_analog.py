"""AI ports read from their own wiring facts (contracts v1.9.1, ADR-024): a current sensor on a
mains motor is ``ac_rms`` + a scale on an ADC channel — no driver, no ``config.pins`` entry."""

import math
import unittest

from agal_one_agent.blocks.io import HardwareIO, PortRef
from agal_one_agent.ports.analog import AnalogReader, scaled, summarise
from agal_one_agent.ports.gpiochip import PortUnavailable
from agal_one_agent.ports.port_test import run_port_test


class FakeClock:
    """Each conversion takes 3.5 ms — what an ADS1115 manages in single-shot mode at 860 SPS."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeAdc:
    """A Hall sensor idling at `bias` volts with a 50 Hz current of `amps_rms` on it."""

    def __init__(self, clock, amps_rms=0.0, volts_per_amp=0.040, bias=2.5, dead=False):
        self.clock, self.amps, self.k, self.bias, self.dead = clock, amps_rms, volts_per_amp, bias, dead
        self.reads = 0

    def read_v(self, channel):
        self.reads += 1
        self.clock.t += 0.0035
        if self.dead:
            return float("nan")
        return self.bias + self.k * self.amps * math.sqrt(2) * math.sin(2 * math.pi * 50 * self.clock.t)

    def close(self):
        pass


def reader(adc, clock, **kw):
    return AnalogReader(adc_factory=lambda bus, addr: adc, clock=clock, **kw)


ACS758 = {"kind": "i2c", "busId": 1, "addr": 72, "channel": 0, "driver": "ads1115",
          "measure": {"mode": "ac_rms", "windowMs": 100}, "transform": {"scale": 25, "clampMin": 0}}


class SummariseTest(unittest.TestCase):
    def test_dc_is_the_mean(self):
        self.assertAlmostEqual(summarise([1.0, 2.0, 3.0], "dc"), 2.0)

    def test_failed_conversions_are_dropped(self):
        self.assertAlmostEqual(summarise([1.0, float("nan"), 3.0], "dc"), 2.0)
        self.assertIsNone(summarise([float("nan")] * 5, "dc"))

    def test_ac_rms_ignores_the_bias(self):
        wave = [2.5 + math.sin(2 * math.pi * i / 40) for i in range(200)]  # five whole cycles, 1 V peak
        self.assertAlmostEqual(summarise(wave, "ac_rms"), 1 / math.sqrt(2), places=3)
        self.assertAlmostEqual(summarise([v + 0.7 for v in wave], "ac_rms"), 1 / math.sqrt(2), places=3)

    def test_too_few_samples_say_nothing_about_an_alternating_signal(self):
        self.assertIsNone(summarise([2.5, 2.6, 2.4], "ac_rms"))

    def test_scale_offset_and_clamp(self):
        self.assertAlmostEqual(scaled(0.4, {"scale": 25}), 10.0)
        self.assertAlmostEqual(scaled(1.0, {"scale": 2.5, "offset": -2.5, "clampMin": 0}), 0.0)
        self.assertAlmostEqual(scaled(9.0, {"scale": 10, "clampMax": 50}), 50.0)
        self.assertAlmostEqual(scaled(1.25, None), 1.25)


class AnalogReaderTest(unittest.TestCase):
    def test_a_pump_current_comes_out_in_amps(self):
        clock = FakeClock()
        r = reader(FakeAdc(clock, amps_rms=12.0), clock)
        self.assertAlmostEqual(r.read(ACS758), 12.0, delta=0.4)

    def test_a_drifting_supply_is_not_read_as_current(self):
        clock = FakeClock()
        r = reader(FakeAdc(clock, amps_rms=0.0, bias=2.38), clock)
        self.assertAlmostEqual(r.read(ACS758), 0.0, places=6)

    def test_a_steady_value_is_the_mean_scaled(self):
        clock = FakeClock()
        r = reader(FakeAdc(clock, amps_rms=0.0, bias=1.65), clock)
        level = {**ACS758, "measure": {"mode": "dc"}, "transform": {"scale": 100 / 3.3}}
        self.assertAlmostEqual(r.read(level), 50.0, places=3)

    def test_one_program_pass_does_not_sample_a_channel_twice(self):
        clock = FakeClock()
        adc = FakeAdc(clock, amps_rms=5.0)
        r = reader(adc, clock)
        first = r.read(ACS758)
        n = adc.reads
        self.assertEqual(r.read(ACS758), first)
        self.assertEqual(adc.reads, n)
        clock.t += 1.0
        r.read(ACS758)
        self.assertGreater(adc.reads, n)

    def test_an_adc_that_does_not_answer_reads_none(self):
        clock = FakeClock()
        self.assertIsNone(reader(FakeAdc(clock, dead=True), clock).read(ACS758))

    def test_only_what_the_agent_can_read_is_supported(self):
        self.assertTrue(AnalogReader.supports(ACS758))
        self.assertFalse(AnalogReader.supports({**ACS758, "driver": "mcp3008"}))
        self.assertFalse(AnalogReader.supports({"kind": "gpio", "chip": "pinctrl-bcm2835", "line": 17}))
        with self.assertRaises(PortUnavailable):
            AnalogReader().read_volts({**ACS758, "driver": "mcp3008"})


class ProgramAndPortTest(unittest.TestCase):
    def test_the_program_reads_an_ai_port_without_a_sensor_in_config_pins(self):
        clock = FakeClock()
        io = HardwareIO(configured_gpios=None, sensor_by_key={}, lines=None, analog=reader(FakeAdc(clock, amps_rms=8.0), clock))
        port = PortRef("pump1", "sensor.current", {**ACS758, "direction": "in"})
        self.assertTrue(io.has_port(port))
        self.assertAlmostEqual(io.read_input(port), 8.0, delta=0.3)

    def test_without_a_reader_the_port_reads_none_not_zero(self):
        io = HardwareIO(configured_gpios=None, sensor_by_key={}, lines=None)
        self.assertIsNone(io.read_input(PortRef("pump1", "sensor.current", {**ACS758, "direction": "in"})))

    def test_watching_an_ai_port_answers_in_the_ports_unit(self):
        clock = FakeClock()
        out = run_port_test({"portId": "AI1", "action": "read", "seconds": 2, "unit": "A", "transport": ACS758},
                            lines=None, sleep=lambda s: None, analog=reader(FakeAdc(clock, amps_rms=12.0), clock))
        self.assertEqual(out["result"], "passed")
        self.assertAlmostEqual(out["value"], 12.0, delta=0.4)
        self.assertIn(" A now", out["detail"])
        self.assertIn("AC RMS", out["detail"])

    def test_an_analog_input_is_not_pulsed(self):
        clock = FakeClock()
        out = run_port_test({"portId": "AI1", "action": "pulse", "transport": ACS758}, lines=None,
                            sleep=lambda s: None, analog=reader(FakeAdc(clock), clock))
        self.assertEqual(out["result"], "failed")
        self.assertIn("watched, not pulsed", out["detail"])

    def test_a_silent_adc_fails_the_test_in_words(self):
        clock = FakeClock()
        out = run_port_test({"portId": "AI1", "action": "read", "seconds": 1, "transport": ACS758}, lines=None,
                            sleep=lambda s: None, analog=reader(FakeAdc(clock, dead=True), clock))
        self.assertEqual(out["result"], "failed")
        self.assertIn("nothing answered at 0x48 on I2C-1", out["detail"])


if __name__ == "__main__":
    unittest.main()
