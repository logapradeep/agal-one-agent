from setuptools import setup, find_packages

setup(
    name="agal-one-agent",
    # 0.2.0 (2026-09-07): automation-block runtime (ADR-017, contracts v1.5.0).
    # NOTE: the fleet provisioner still pins v0.1.6 until the backend side of
    # ADR-017 (rebuild phase P2) lands; 0.1.7/0.1.8 were never tagged.
    version="0.2.0",
    packages=find_packages(),
    install_requires=[
        "paho-mqtt>=2.0.0",
        "PyYAML>=6.0",
        # NOTE: the ADR-013 P0 durable ring buffer uses the stdlib `sqlite3`
        # (WAL) — no third-party dependency added on the core path.
    ],
    extras_require={
        "rpi": [
            "RPi.GPIO>=0.7.1",
            "gpiozero>=2.0",
            "smbus2>=0.4.3",
            # spidev also drives the raw-LoRa SX127x listener (ADR-011, DARK by
            # default; import-guarded so the daemon runs without it).
            "spidev>=3.6",
            "pyserial>=3.5",
            "w1thermsensor>=2.3.0",
            # BNO055 9-DoF IMU (sensor_type "bno055_9dof") — CircuitPython
            # driver + the Blinka compatibility layer it runs on.
            "adafruit-blinka>=8.0",
            "adafruit-circuitpython-bno055>=5.4",
            # Raw-LoRa AES-128-CCM frame crypto (ADR-011 §6). Import-guarded and
            # unused until the live SX127x RX path lands post-bench-spike, but
            # declared here so the field build is complete.
            "cryptography>=42.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "agal-one-agent=agal_one_agent.main:main",
            "agal-one-agent-ota-verify=agal_one_agent.ota_updater:main_verify",
            "agal-one-agent-sim=agal_one_agent.blocks.simulate:main",
        ],
    },
    python_requires=">=3.9",
)
