from setuptools import setup, find_packages

setup(
    name="menvayal-agent",
    version="0.1.6",
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
        ],
    },
    entry_points={
        "console_scripts": [
            # Primary name — the provisioning systemd unit runs `agal-one-agent`
            # (the ecosystem rebranded Menvayal -> Agal One). The package/module
            # keep the legacy `menvayal_agent` name; `menvayal-agent` stays as a
            # back-compat alias.
            "agal-one-agent=menvayal_agent.main:main",
            "menvayal-agent=menvayal_agent.main:main",
        ],
    },
    python_requires=">=3.9",
)
