"""Abstraction layer for sensor entities"""

from .device import BaseDevice


class Sensor(BaseDevice):
    """Represents a Sensor device in the U-Home API.

    Maps to Home Assistant's Sensor platform.
    """
