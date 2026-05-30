from setuptools import setup, find_packages

setup(
    name="agal-agent",
    version="0.1.5",
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
            "agal-agent=agal_agent.main:main",
        ],
    },
    python_requires=">=3.9",
)
