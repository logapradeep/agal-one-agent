from setuptools import setup, find_packages

setup(
    name="agal-one-agent",
    # NOTE: v0.1.6 was released/deployed from origin/main (commit 25cd7f3, still
    # on the pre-rename `menvayal_agent` package lineage) and is NOT merged into
    # this rename branch yet — see CHANGELOG.md. 0.1.7 is the next free patch.
    version="0.1.7",
    packages=find_packages(),
    install_requires=[
        "paho-mqtt>=2.0.0",
        "PyYAML>=6.0",
    ],
    extras_require={
        "rpi": [
            "RPi.GPIO>=0.7.1",
            "gpiozero>=2.0",
            "smbus2>=0.4.3",
            "spidev>=3.6",
            "pyserial>=3.5",
            "w1thermsensor>=2.3.0",
            # BNO055 9-DoF IMU (sensor_type "bno055_9dof") — CircuitPython
            # driver + the Blinka compatibility layer it runs on.
            "adafruit-blinka>=8.0",
            "adafruit-circuitpython-bno055>=5.4",
        ],
    },
    entry_points={
        "console_scripts": [
            "agal-one-agent=agal_one_agent.main:main",
            "agal-one-agent-ota-verify=agal_one_agent.ota_updater:main_verify",
        ],
    },
    python_requires=">=3.9",
)
