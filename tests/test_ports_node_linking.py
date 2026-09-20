"""Node linking on the node (ADR-024): the character-device GPIO path by chip LABEL + line,
the hardware report, the port test and the bus scan — all against fakes, so the rules are
proven on a machine with no GPIO. The first check on a real board is an LED on DIO1
(header pin 11 = pinctrl-bcm2835 line 17 on a Raspberry Pi 3)."""

from __future__ import annotations

import errno
import os
import struct

import pytest

from agal_one_agent.blocks.io import HardwareIO, PortRef
from agal_one_agent.ports import bus_scan
from agal_one_agent.ports.gpiochip import (
    GPIO_GET_CHIPINFO_IOCTL, GPIO_GET_LINEINFO_IOCTL, GPIO_V2_GET_LINEINFO_IOCTL, ChipInfo, LineIO, PortUnavailable, list_chips,
)
from agal_one_agent.ports.inventory import hardware_report, identity, inventory
from agal_one_agent.ports.port_test import run_port_test


# ------------------------------------------------------------------ fakes

class FakeLine:
    def __init__(self, log, chip, line, direction):
        self.log, self.key, self.direction, self.level = log, (chip, line), direction, False
        log.append(("open", chip, line, direction))

    def write(self, level):
        self.level = bool(level)
        self.log.append(("write", *self.key, bool(level)))

    def read(self):
        return self.level

    def close(self):
        self.log.append(("close", *self.key))


def fake_lines(busy=(), inputs=None):
    log: list = []
    chips = [ChipInfo("/dev/gpiochip0", "gpiochip0", "pinctrl-bcm2835", 54), ChipInfo("/dev/gpiochip1", "gpiochip1", "raspberrypi-exp-gpio", 8)]

    def opener(path, line, direction):
        if (path, line) in busy:
            raise OSError(errno.EBUSY, "Device or resource busy")
        handle = FakeLine(log, path, line, direction)
        if inputs and (path, line) in inputs:
            handle.level = inputs[(path, line)]
        return handle

    return LineIO(chips=lambda: chips, opener=opener), log


def fake_ioctl(v2=True):
    chips = {"/dev/gpiochip0": ("gpiochip0", "pinctrl-bcm2835", 4, {2: "SDA1", 3: "SCL1"}, {2, 3})}

    def ioctl(path, request, buf):
        name, label, lines, names, used = chips[path]
        if request == GPIO_GET_CHIPINFO_IOCTL:
            buf[0:len(name)] = name.encode()
            buf[32:32 + len(label)] = label.encode()
            struct.pack_into("=I", buf, 64, lines)
        elif request == GPIO_V2_GET_LINEINFO_IOCTL:
            if not v2:
                raise OSError(errno.EINVAL, "no v2 ABI")
            offset = struct.unpack_from("=I", buf, 64)[0]
            n = names.get(offset, "").encode()
            buf[0:len(n)] = n
            struct.pack_into("=Q", buf, 72, 1 if offset in used else 0)
        elif request == GPIO_GET_LINEINFO_IOCTL:
            offset = struct.unpack_from("=I", buf, 0)[0]
            struct.pack_into("=I", buf, 4, 1 if offset in used else 0)
            n = names.get(offset, "").encode()
            buf[8:8 + len(n)] = n
    return ioctl


# ------------------------------------------------------------ chips + lines

@pytest.mark.parametrize("v2", [True, False])
def test_chip_label_lines_and_kernel_held_lines_are_read_with_either_abi(tmp_path, v2):
    (tmp_path / "gpiochip0").write_text("")
    chips = list_chips(str(tmp_path / "gpiochip*"), ioctl=lambda p, r, b: fake_ioctl(v2)("/dev/gpiochip0", r, b))
    assert [(c.label, c.lines, c.used_lines) for c in chips] == [("pinctrl-bcm2835", 4, [2, 3])]
    assert chips[0].line_names == ["", "", "SDA1", "SCL1"]


def test_a_line_is_addressed_by_chip_LABEL_never_by_chip_number():
    lines, log = fake_lines()
    lines.write("raspberrypi-exp-gpio", 2, True)
    assert log == [("open", "/dev/gpiochip1", 2, "out"), ("write", "/dev/gpiochip1", 2, True)]


def test_active_low_is_a_wiring_fact_applied_at_the_line():
    lines, log = fake_lines()
    lines.write("pinctrl-bcm2835", 17, True, active_low=True)
    assert log[-1] == ("write", "/dev/gpiochip0", 17, False)


def test_a_line_stays_open_between_writes_and_is_reopened_when_its_direction_changes():
    lines, log = fake_lines()
    lines.write("pinctrl-bcm2835", 17, True)
    lines.write("pinctrl-bcm2835", 17, False)
    assert [e[0] for e in log] == ["open", "write", "write"]
    lines.read("pinctrl-bcm2835", 17)
    assert [e[0] for e in log][-2:] == ["close", "open"]


def test_a_chip_label_this_board_does_not_have_and_a_kernel_held_line_are_refused_in_words():
    lines, _ = fake_lines(busy={("/dev/gpiochip0", 2)})
    with pytest.raises(PortUnavailable, match="no GPIO chip labelled gpio0"):
        lines.write("gpio0", 1, True)
    with pytest.raises(PortUnavailable, match="line 2 cannot be opened"):
        lines.write("pinctrl-bcm2835", 2, True)


# --------------------------------------------------------------- port test

def test_the_led_check_pulses_DIO1_and_leaves_the_line_as_it_found_it():
    lines, log = fake_lines()
    slept = []
    r = run_port_test({"portId": "DIO1", "action": "pulse", "seconds": 2,
                       "transport": {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": 17, "direction": "out"}}, lines, sleep=slept.append)
    assert r == {"portId": "DIO1", "result": "passed", "detail": "on for 2 s, then off"}
    assert slept == [2]
    assert log == [("open", "/dev/gpiochip0", 17, "out"), ("write", "/dev/gpiochip0", 17, True),
                   ("write", "/dev/gpiochip0", 17, False), ("close", "/dev/gpiochip0", 17)]


def test_a_pulse_is_never_longer_than_five_seconds_and_goes_off_even_when_the_wait_fails():
    lines, log = fake_lines()
    slept = []
    run_port_test({"portId": "DO1", "action": "pulse", "seconds": 600,
                   "transport": {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": 17}}, lines, sleep=slept.append)
    assert slept == [5]

    def boom(_):
        raise RuntimeError("interrupted")
    r = run_port_test({"portId": "DO1", "action": "pulse", "transport": {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": 27}}, lines, sleep=boom)
    assert r["result"] == "failed"
    assert ("write", "/dev/gpiochip0", 27, False) in log and log[-1] == ("close", "/dev/gpiochip0", 27)


def test_an_active_low_relay_is_pulsed_LOW_and_released_HIGH():
    lines, log = fake_lines()
    run_port_test({"portId": "DO1", "action": "pulse", "activeLow": True,
                   "transport": {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": 17}}, lines, sleep=lambda s: None)
    assert [e[3] for e in log if e[0] == "write"] == [False, True]


def test_watching_an_input_reports_its_state():
    lines, _ = fake_lines(inputs={("/dev/gpiochip0", 19): True})
    r = run_port_test({"portId": "DI1", "action": "read", "seconds": 1,
                       "transport": {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": 19, "direction": "in"}}, lines, sleep=lambda s: None)
    assert r["result"] == "passed" and r["value"] is True and "on after 1 s" in r["detail"]


def test_a_port_the_program_is_driving_a_missing_chip_and_a_bus_port_fail_with_a_reason():
    lines, log = fake_lines()
    gpio = {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": 17}
    assert "pause automation first" in run_port_test({"portId": "DO1", "action": "pulse", "transport": gpio}, lines, busy=lambda c, l: True)["detail"]
    assert log == []
    assert "no GPIO chip labelled gpio9" in run_port_test({"portId": "DO1", "action": "pulse", "transport": {**gpio, "chip": "gpio9"}}, lines)["detail"]
    assert "header pins only" in run_port_test({"portId": "DO9", "action": "pulse", "transport": {"kind": "modbus_rtu"}}, lines)["detail"]
    assert run_port_test({"portId": "DO1", "action": "toggle", "transport": gpio}, lines)["result"] == "failed"


# ---------------------------------------------------- the block runtime's I/O

def test_the_block_runtime_drives_a_port_named_by_chip_label_and_line():
    lines, log = fake_lines()
    io = HardwareIO(configured_gpios=None, sensor_by_key={}, lines=lines)
    port = PortRef("valve-1", "device.power", {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": 18, "direction": "out", "activeLow": True})
    assert io.has_port(port) is True
    io.write_output(port, True)
    assert log[-1] == ("write", "/dev/gpiochip0", 18, False)
    other_board = PortRef("valve-1", "device.power", {"kind": "gpio", "chip": "gpio0", "line": 3, "direction": "out"})
    assert io.has_port(other_board) is False, "a bundle naming a chip this board does not have is rejected at compile time"


def test_a_legacy_port_with_a_broadcom_number_is_still_accepted():
    io = HardwareIO(configured_gpios={17}, sensor_by_key={}, lines=None)
    assert io.has_port(PortRef("pump-1", "device.power", {"kind": "gpio", "gpioNumber": 17, "direction": "out"})) is True


# ------------------------------------------------------------ hardware report

def fake_root(tmp_path):
    def put(path, text):
        f = tmp_path / path.lstrip("/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    put("/proc/device-tree/model", "Raspberry Pi 3 Model B Rev 1.2\0")
    put("/proc/device-tree/serial-number", "00000000a1b2c3d4\0")
    put("/proc/cpuinfo", "processor\t: 0\nHardware\t: BCM2835\nSerial\t\t: 00000000a1b2c3d4\nModel\t\t: Raspberry Pi 3 Model B Rev 1.2\n")
    put("/etc/os-release", 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nVERSION_ID="12"\n')
    put("/proc/meminfo", "MemTotal:         944268 kB\nMemFree:  1 kB\n")
    put("/proc/net/route", "Iface\tDestination\tGateway\nwlan0\t00000000\t0101A8C0\nwlan0\t0001A8C0\t00000000\n")
    for dev in ("i2c-1", "spidev0.0", "spidev0.1", "ttyUSB0", "rtc0"):
        put(f"/dev/{dev}", "")
    put("/sys/class/rtc/rtc0/name", "rtc-ds1307 1-0068\n")
    by_id = tmp_path / "dev/serial/by-id"
    by_id.mkdir(parents=True)
    os.symlink(tmp_path / "dev/ttyUSB0", by_id / "usb-1a86_USB_Serial-if00-port0")
    return str(tmp_path)


def test_the_board_says_what_it_is_without_any_code_naming_a_board(tmp_path):
    ident = identity(fake_root(tmp_path), agent_version="0.3.0")
    assert ident["model"] == "Raspberry Pi 3 Model B Rev 1.2" and ident["serial"] == "00000000a1b2c3d4"
    assert ident["os"] == {"id": "debian", "version": "12", "prettyName": "Debian GNU/Linux 12 (bookworm)"}
    assert ident["memoryMb"] == 922 and ident["agentVersion"] == "0.3.0"
    assert ident["network"] == {"interface": "wlan0", "kind": "wifi"}


def test_the_inventory_lists_chips_with_kernel_held_lines_buses_and_the_stable_usb_serial_path(tmp_path):
    root = fake_root(tmp_path)
    inv = inventory(root, chips=lambda: [ChipInfo("/dev/gpiochip0", "gpiochip0", "pinctrl-bcm2835", 54, used_lines=[2, 3])],
                    i2c_scan=lambda dev: [72, 104])
    assert inv["gpioChips"] == [{"label": "pinctrl-bcm2835", "lines": 54, "usedLines": [2, 3]}]
    assert inv["i2c"] == [{"device": "/dev/i2c-1", "busId": 1, "addresses": [72, 104]}]
    assert inv["spi"] == [{"device": "/dev/spidev0.0", "busId": 0, "cs": 0}, {"device": "/dev/spidev0.1", "busId": 0, "cs": 1}]
    assert inv["serial"] == [{"device": "/dev/ttyUSB0", "byId": "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"}]
    assert inv["rtc"] == [{"device": "/dev/rtc0", "name": "rtc-ds1307 1-0068"}]


def test_a_board_with_no_gpio_at_all_still_reports(tmp_path):
    report = hardware_report("0.3.0", root=str(tmp_path), scan=False)
    assert report["inventory"]["gpioChips"] == [] and report["agentVersion"] == "0.3.0"


# ------------------------------------------------------------------ bus scan

def test_modbus_crc_and_the_read_coils_request_match_the_standard():
    assert bus_scan.read_coils_request(1).hex() == "010100000001fdca"
    assert bus_scan.read_coils_request(17, 0x13, 0x25).hex() == "110100130025" + f"{bus_scan.crc16(bytes.fromhex('110100130025')):04x}"[2:] + f"{bus_scan.crc16(bytes.fromhex('110100130025')):04x}"[:2]


def test_a_reply_or_an_exception_from_the_unit_counts_and_noise_does_not():
    def framed(body):
        c = bus_scan.crc16(body)
        return body + bytes([c & 0xFF, c >> 8])
    assert bus_scan.is_reply_from(1, framed(bytes([1, 0x01, 1, 0x00])))
    assert bus_scan.is_reply_from(1, framed(bytes([1, 0x81, 0x02]))), "an exception reply still proves a device is there"
    assert not bus_scan.is_reply_from(2, framed(bytes([1, 0x01, 1, 0x00]))), "another unit's reply"
    assert not bus_scan.is_reply_from(1, bytes([1, 0x01, 1, 0x00, 0, 0])), "bad CRC"
    assert not bus_scan.is_reply_from(1, b"")


def test_a_modbus_scan_reports_the_units_that_answer_and_only_suggests():
    class Port:
        def __init__(self):
            self.unit = None
        def reset_input_buffer(self): pass
        def write(self, frame): self.unit = frame[0]
        def read(self, n):
            if self.unit not in (1, 7):
                return b""
            body = bytes([self.unit, 0x01, 1, 0x00])
            c = bus_scan.crc16(body)
            return body + bytes([c & 0xFF, c >> 8])
        def close(self): pass
    assert bus_scan.scan_modbus("/dev/ttyUSB0", {"baud": 9600}, 1, 16, opener=lambda d, s: Port(), sleep=lambda s: None) == [1, 7]
    assert bus_scan.scan_modbus("/dev/nope", {}, opener=lambda d, s: (_ for _ in ()).throw(OSError("no such device"))) is None
    r = bus_scan.run_bus_scan({"bus": {"id": "SPI-0", "kind": "spi", "device": "/dev/spidev0.0"}})
    assert r["answered"] == [] and "cannot be scanned" in r["error"]
