"""Agal One IoT Agent - connects hardware to Agal One cloud via MQTT."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("agal-one-agent")
except PackageNotFoundError:  # running from a checkout without an install
    __version__ = "0.2.1"
