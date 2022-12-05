"""
Waterforce X driver for liquidctl
SPDX-License-Identifier: GPL-3.0-or-later
"""
# uses the psf/black style

import logging

import usb

from liquidctl.driver.usb import UsbHidDriver
from liquidctl.error import NotSupportedByDevice
from liquidctl.keyval import RuntimeStorage
from liquidctl.util import clamp

_LOGGER = logging.getLogger(__name__)

_CMD_PREFIX = 0x99
_READ_LENGTH = 64
# Does not matter at the moment, but the file send functionality will
# require larger writes.
_WRITE_PAD = 64

_CMD_READ_FIRMWARE_VER = 0xD6
_CMD_READ_DEVICE_ANGLE = 0xD7
_CMD_READ_DEVICE_SPEED = 0xD8
_CMD_READ_DEVICE_CURVE = 0xD9
_CMD_READ_DEVICE_STATUS = 0xDA
_CMD_READ_DEVICE_MODE = 0xDD
_CMD_READ_DEVICE_VARIANT = 0xDE
_CMD_WRITE_CPU_INFO = 0xE0
_CMD_WRITE_CPU_NAME = 0xE1
_CMD_WRITE_DISPLAY = 0xE2
_CMD_WRITE_FANPUMP_MODE = 0xE5
_CMD_WRITE_FANPUMP_SPEED = 0xEB


DEVICE_WATERFORCE_X = "WATERFORCE X (240, 280 or 360)"
DEVICE_WATERFORCE_XG = "WATERFORCE X 360G"
DEVICE_WATERFORCE_EX = "WATERFORCE EX 360"

# 3 WATERFORCE X models share the same USB VID/PID, but respond to a
# command to identify them
MODEL_VARIANTS = {
    0: "WATERFORCE X 240",
    1: "WATERFORCE X 280",
    2: "WATERFORCE X 360"
}

FAN_MAX_RPM = 2500
PUMP_MAX_RPM = 2800

_STATUS_TEMPERATURE = 'Liquid temperature'
_STATUS_FAN_SPEED = 'Fan speed'
_STATUS_PUMP_SPEED = 'Pump speed'
_STATUS_FAN_DUTY = 'Fan duty'
_STATUS_PUMP_DUTY = 'Pump duty'
_STATUS_FIRMWARE_VER = 'Firmware version'
_STATUS_VARIANT = 'Model variant'
_STATUS_FAN_MODE = 'Fan mode'
_STATUS_PUMP_MODE = 'Pump mode'

_NUM_SPEED_CURVES = 4

_SPEED_CHANNELS = {
    'fan': 0x1,
    'pump': 0x2
}

_SPEED_PROFILES = {
    'balanced': 0x0,
    'custom': 0x1,
    'default': 0x2,
    'max': 0x4,
    'performance': 0x5,
    'quiet': 0x6,
    'zero rpm': 0x7
}

# Reversed of the above dict for lookup
_SPEED_PROFILES_REV = {v: k for k, v in _SPEED_PROFILES.items()}


class Waterforce(UsbHidDriver):
    """Gigabyte AORUS WATERFORCE X liquid cooler"""

    _MATCHES = [
        (0x1044, 0x7a4d, 'Gigabyte AORUS WATERFORCE X (240, 280, 360)', {
            'device_type': DEVICE_WATERFORCE_X,
            'speed_channels': _SPEED_CHANNELS
        }),
        (0x1044, 0x7a52, 'Gigabyte AORUS WATERFORCE X 360G', {
            'device_type': DEVICE_WATERFORCE_XG,
            'speed_channels': _SPEED_CHANNELS
        }),
        (0x1044, 0x7a53, 'Gigabyte AORUS WATERFORCE EX 360', {
            'device_type': DEVICE_WATERFORCE_EX,
            'speed_channels': _SPEED_CHANNELS
        }),
    ]

    def __init__(
            self,
            device,
            description,
            speed_channels,
            device_type=DEVICE_WATERFORCE_X,
            **kwargs):
        super().__init__(device, description)
        self.device_type = device_type
        self.supports_lighting = False
        self.supports_cooling = True
        self._firmware_version = None
        self._speed_channels = speed_channels

    def initialize(self, **kwargs):
        """Initialize the device and the driver.

        This method should be called every time the system boots, resumes from
        a suspended state, or if the device has just been (re)connected.  In
        those scenarios, no other method, except `connect()` or `disconnect()`,
        should be called until the device and driver has been (re-)initialized.

        Returns None or a list of `(property, value, unit)` tuples, similarly
        to `get_status()`.
        """

        self._status = []

        # Get device variant if WATERFORCE X
        if self.device_type == DEVICE_WATERFORCE_X:
            self._write([_CMD_PREFIX, _CMD_READ_DEVICE_VARIANT])
            variantmsg = self._read()
            self._status.append(
                (_STATUS_VARIANT, MODEL_VARIANTS.get(variantmsg[2]), ""))

        # Get firmware version, determines max pump duty RPM.
        self._write([_CMD_PREFIX, _CMD_READ_FIRMWARE_VER])
        firmwaremsg = self._read()
        # ALlow higher pump rpm if on F14 or higher
        pumphighrpmallowed = firmwaremsg[2] * 10 + firmwaremsg[3] > 13
        if pumphighrpmallowed:
            PUMP_MAX_RPM = 3200
        firmwarestring = "F" + str(firmwaremsg[2]) + "." + str(firmwaremsg[3])
        self._status.append((_STATUS_FIRMWARE_VER, firmwarestring, ""))
        return sorted(self._status)

    def get_status(self, **kwargs):
        """Get a status report.

        Returns a list of `(property, value, unit)` tuples.
        """
        self._write([_CMD_PREFIX, _CMD_READ_DEVICE_MODE])
        msg = self._read()
        fan_mode = _SPEED_PROFILES_REV.get(msg[2])
        pump_mode = _SPEED_PROFILES_REV.get(msg[3])
        self._write([_CMD_PREFIX, _CMD_READ_DEVICE_STATUS])
        msg = self._read()
        # Should get 0x99, 0xDA in first two bytes of response
        if not (msg[0] == _CMD_PREFIX and msg[1] == _CMD_READ_DEVICE_STATUS):
            _LOGGER.warning('did not get back expected response from cooler?')
        return [
            (_STATUS_TEMPERATURE, msg[0xD], "°C"),
            (_STATUS_FAN_SPEED, int.from_bytes(
                [msg[2], msg[3]], byteorder="little"), "rpm"),
            (_STATUS_PUMP_SPEED, int.from_bytes(
                [msg[5], msg[6]], byteorder="little"), "rpm"),
            (_STATUS_FAN_DUTY, msg[8], "%"),
            (_STATUS_PUMP_DUTY, msg[9], "%"),
            (_STATUS_FAN_MODE, fan_mode, ''),
            (_STATUS_PUMP_DUTY, pump_mode, ''),
        ]

    def set_fixed_speed(self, channel, duty, **kwargs):
        # The set speed command expects the speeds in a single byte, likely to be duty.
        # Command is apparently a no-op right now. Unknown as to why Gigabyte
        # have the routine in the dll?
        selected_channel = _SPEED_CHANNELS.get(channel)
        if channel == 'pump':
            # Pump max duty is limited on earlier WATERFORCE X firmware
            rpm = int((PUMP_MAX_RPM / 100) * duty)
            # Gigabyte enforces a min of 750 rpm.
            if rpm < 750:
                duty = int(750 / PUMP_MAX_RPM) * 100

        return self._write(
            [_CMD_PREFIX, _CMD_WRITE_FANPUMP_SPEED, selected_channel, duty])

    def _write(self, data):
        padding = [0x0] * (_WRITE_PAD - len(data))
        self.device.write(data + padding)

    def _read(self):
        data = self.device.read(_READ_LENGTH)
        return data


"""
    def set_speed_profile(self, channel, profile, **kwargs):
        if channel not in _SPEED_CHANNELS:
            _LOGGER.warning('Selected channel not in allowable channels?')
        if channel == 0x02:
            # Pump only allows for certain profiles
            if (channel not in _SPEED_PROFILES_FANS):
                _LOGGER.warning('Selected disallowed preset for pump')
                return
            speed_profile = _SPEED_PROFILES_PUMP.get(profile)
        else:
            speed_profile = _SPEED_PROFILES_FANS.get(profile)

        return self._write(
            [_CMD_PREFIX, _CMD_WRITE_FANPUMP_MODE, channel, speed_profile])
"""
