#!/usr/bin/env python3

# SPDX-License-Identifier: GPL-3.0-or-later
#
# Copyright 2025 Jason Gerecke <jason.gerecke@wacom.com>
# Copyright 2025 Wacom Co., Ltd.
#
# See the NOTICE file distributed with this work for additional information
# regarding copyright ownership.

"""Control brightness, contrast, and other controls of monitors that
expose their DDC/CI over USB.

Displays which do not provide hardware on-screen-display controls for
adjusting brightness, contrast, etc. may still allow adjustment through
software. The `ddcutil` program is often sufficient, but some displays
use bridge chips that aren't compatible. This program is specifically
written for use with devices using either the Silicon Labs CP210x or
the FTDI F232H as a bridge chip. Example devices include the Wacom
Cintiq 13HD (CP210x) and Wacom Cintiq Pro 16/24/32 (F232H).

Usage:
    ddc_usb <path> dump
    ddc_usb <path> list
    ddc_usb <path> <VCP=VALUE> [...]

Arguments:
    <path>         Path to the device to be controlled. Supported devices
                   include a serial device nodes (e.g. `/dev/ttyUSB0`),
                   PyFtdi URLs (e.g. `ftdi://ftdi:232h/1`), and I2C device
                   nodes (e.g. `/dev/i2c-3`).
    dump           Instruct the program to dump the raw capability list
    list           Instruct the program to list the device capabilities
    <VCP>          "Virtual Control Panel" code or name.
    <VALUE>        Value to assign to the specified VCP. May use the
                   special value "?" to get the current value instead of
                   setting it.

    The exact path required for any given device will depend on the bridge
    chip in use. Devices using a Silicon Labs CP210x bridge chip should be
    able to use a serial device node path. Devices which use an FTDI F232H
    bridge chip, however, will need to use an FTDI URL instead. Check the
    output of `lsusb` for information about the type of bridge chip that
    might be in use.

    Values may be specified as either decimal or hexadecimal numbers (e.g.
    the value "10" may alternatively be specified as "0x0A") or as a known
    name contained in the output from "list".

    See the following websites for more information about VCP codes and
    known values:
      - https://www.ddcutil.com/vcpinfo_output/
      - http://www.boichat.ch/nicolas/ddcci/specs.html

Examples:
    # Dump the list of capabilities from a monitor at /dev/ttyUSB0
    ddc_usb /dev/ttyUSB0 dump
    (prot(monitor)type(LCD)model(Wacom Cintiq 13HD)cmds(01 02 03 07 [...]

    # Set brightness to 10 and contrast to 50 for a monitor at /dev/ttyUSB0
    ddc_usb /dev/ttyUSB0 brightness=10 contrast=50

    # Set brightness (vcp code 0x10) to 10 and contrast (vcp code 0x12) to 50
    # for a monitor at /dev/ttyUSB0
    ddc_usb /dev/ttyUSB0 0x10=10 0x12=50

    # Change color preset to 6500K for a monitor at ftdi://ftdi:232h/1
    ddc_usb ftdi://ftdi:232h/1 select-color-preset=6500-k

    # Print the current brightness, change it to 50, and print it again
    # for a monitor at ftdi://ftdi:232h/1.
    ddc_usb ftdi://ftdi:232h/1 brightness=? brightness=50 brightness=?
"""

import re
import struct
import sys
import time

from pyftdi.i2c import I2cController
import pyftdi.misc
import serial
from smbus2 import SMBus, i2c_msg

DEBUG = False


class FtdiDevice:
    """
    Abstract low-level representation of a DDC/CI device that is
    accessible over an FTDI bridge chip.
    """

    def __init__(self, url):
        self._url = url
        self._i2c_master = None
        self._device_handle = None

    def __enter__(self):
        i2c = I2cController()
        i2c.configure(self._url, frequency=100000.0)
        print(f"Opened i2c connection to {self._url} at {i2c.frequency}Hz")
        self._device_handle = i2c.get_port(0x37)
        self._i2c_master = i2c
        return self

    def __exit__(self, *args):
        self._i2c_master.close()
        self._i2c_master = None
        self._device_handle = None

    def write(self, message):
        # The hardware bridge implementation will insert the destination
        # address on its own, so be sure that we do not also write it
        # ourselves.
        message = message[1:]
        self._device_handle.write(message)
        self._device_handle.flush()

    def read(self, length):
        return self._device_handle.read(length)


class SerialDevice:
    """
    Abstract low-level representation of a DDC/CI device that is
    accessible behind a serial device (e.g. /dev/ttyUSB0).
    """

    def __init__(self, path):
        self._path = path
        self._device_handle = None

    def __enter__(self):
        ser = serial.Serial(self._path)
        print("Opened serial connection")
        self._device_handle = ser
        return self

    def __exit__(self, *args):
        self._device_handle.close()
        self._device_handle = None

    def write(self, message):
        self._device_handle.write(message)
        self._device_handle.flush()

    def read(self, length):
        return self._device_handle.read(length)


class I2cDevice:
    """
    Abstract low-level representation of a DDC/CI device that is
    accessible via an I2C device node.
    """

    def __init__(self, path):
        self._path = path
        self._bus = None

    def __enter__(self):
        self._bus = SMBus(self._path)
        print(f"Opened i2c connection to {self._path}")
        return self

    def __exit__(self, *args):
        self._bus.close()
        self._bus = None

    def write(self, message):
        # Remove address from message before writing
        request = i2c_msg.write(0x37, message[1:])
        self._bus.i2c_rdwr(request)

    def read(self, length):
        rd = i2c_msg.read(0x37, length)
        self._bus.i2c_rdwr(rd)
        return bytes(rd)


class DDCInterface:
    """
    Interface for interacting with DDC/CI devices.

    This class exposes some of the APIs described in the VESA Display Data
    Channel Command Interface Standard, Version 1.1.
    """

    def __init__(self, ddc_device):
        """
        Create a new DDC Interface.

        The provided 'ddc_device' must support read and write function
        calls.
        """
        self._ddc_device = ddc_device
        self._retries = 3
        self._sleep_multiplier = 1.5

    def get_value(self, control):
        """
        Read the information about the provided VCP control number.

        This function returns a dictionary representing the various
        values returned from the device.
        """
        # VESA VESA-DDCCI-1.1
        # Section 4.3 "Get VCP Feature & VCP Feature Reply"
        if control > 255 or control < 0:
            raise ValueError(f"Invalid VCP control code {control}")
        message = b"\x6e\x51\x82\x01" + bytes({control})
        data = self._query(message, sleep=0.040)
        if len(data) != 12:
            raise ValueError("Received reply of unexpected length")
        if data[3] != 0x02:
            raise ValueError("Received reply of unexpected opcode")
        result = {
            "errno": data[4],
            "opcode": data[5],
            "type": data[6],
            "maximum": int.from_bytes(data[7:9], "big"),
            "value": int.from_bytes(data[9:11], "big"),
        }
        return result

    def set_value(self, control, value):
        """
        Set the provided VCP control number to the requested value.
        """
        # VESA VESA-DDCCI-1.1
        # Section 4.4 "Set VCP Feature"
        if control > 255 or control < 0:
            raise ValueError(f"Invalid VCP control code {control}")
        control_bytes = control.to_bytes(1, "big")
        value_bytes = value.to_bytes(2, "big")
        message = b"\x6e\x51\x84\x03" + control_bytes + value_bytes
        self._write(message, sleep=0.050)

    def save_settings(self):
        """
        Request the display save the current adjustment data (e.g. to
        EEPROM or other non-volatile storage).
        """
        # VESA VESA-DDCCI-1.1
        # Section 4.5 "Save Current Settings"
        message = b"\x6e\x51\x81\x0c"
        self._write(message, sleep=0.200)

    def request_capabilities(self):
        """
        Request the display's capabilities string.
        """
        # VESA-DDCCI-1.1
        # Section 4.6 "Capabilities Request & Capabilities Reply"
        result = b""
        offset = 0
        while True:
            message = b"\x6e\x51\x83\xf3" + struct.pack(">h", offset)
            self._write(message, sleep=0.040)
            data = self._read(sleep=0.050)
            payload = data[6:-1]
            if len(payload) == 0:
                break
            result += payload
            offset += len(payload)
        return str(result, "ASCII")

    @staticmethod
    def _checksum(data, destination_address=None):
        r"""
        Calculate the DDC/CI checksum of a chunk of data.

        DDC/CI uses a simple XOR checksum of all message bytes, including
        the source and destination addresses. If an destination address
        is provided to this function, that value will replace the first
        message byte when calculating the checksum (this is to handle
        checksums that are supposed to be calculated with the "0x50
        virtual host address").

        >>> DDCInterface._checksum(b'\x01\x02')
        3
        >>> DDCInterface._checksum(b'\x01\x02\x03')
        0
        >>> DDCInterface._checksum(b'\x01\x02', destination_address=0x50)
        82
        >>> DDCInterface._checksum(b'\x01\x02\x03', destination_address=0x50)
        81
        """
        # VESA VESA-DDCCI-1.1
        # Section 1.6 "I2C Bus Notation"
        checksum = 0
        for c in data:
            checksum = checksum ^ c
        if destination_address is not None:
            checksum = checksum ^ data[0] ^ destination_address
        return checksum

    def _retry(self, fn, sleep=0.040):
        """
        Repeatedly call the provided function until it either does not
        raise an exception, or the number maximum number of attempts is
        exceeded.
        """
        # VESA VESA-DDCCI-1.1
        # Section 5 "Communication Protocol"
        saved_exception = None
        for attempt in range(0, self._retries + 1):
            try:
                return fn()
            except Exception as e:
                saved_exception = e
                if DEBUG:
                    print(f"Failed attempt #{attempt+1}")
                time.sleep(sleep * self._sleep_multiplier)
        raise saved_exception

    def _write(self, message, sleep):
        """
        Attempt to write the provided message to the DDC/CI device.

        This method will sleep for the requested amount of time after
        a write to allow the DDC/CI device to process the write. Retries
        will be attempted in the case of a failure.
        """
        return self._retry(lambda m=message, s=sleep: self._write_once(m, s))

    def _write_once(self, message, sleep):
        """
        Attempt to write the provided message to the DDC/CI device.

        This method will sleep for the requested amount of time after
        a write to allow the DDC/CI device to process the write. Retries
        will *NOT* be attempted in the case of a failure.
        """
        x = message + bytes({DDCInterface._checksum(message)})
        if DEBUG:
            print(f"Writing: {pyftdi.misc.hexline(x)}")
        self._ddc_device.write(x)
        time.sleep(sleep * self._sleep_multiplier)

    def _read(self, sleep):
        """
        Attempt to read the next message from the DDC/CI device.

        This method will sleep for the requested amount of time after
        a read to allow the DDC/CI device to produce more data. Retries
        will be attempted in the case of a failure.
        """
        return self._retry(lambda: self._read_once(sleep))

    def _read_once(self, sleep):
        """
        Attempt to read the next message from the DDC/CI device.

        This method will sleep for the requested amount of time after
        a read to allow the DDC/CI device to produce more data. Retries
        will *NOT* be attempted in the case of a failure.

        This function will only accept messages containing a source
        address of 0x6E.

        This function will return the entire message received from a
        DDC/CI device, starting at the "destination address".
        """
        # Pre-fill the buffer with the (assumed) destination address since
        # the device handles do not provide this information on read().
        data = b"\x6f"
        data = data + self._ddc_device.read(2)
        if data[1] != 0x6E:
            raise IOError(f"Unexpected reply from address {data[1]}")
        data = data + self._ddc_device.read((data[2] & 0x7F) + 1)
        x = DDCInterface._checksum(data, 0x50)
        if DEBUG:
            print(f"Read: {pyftdi.misc.hexline(data)}")
        if x != 0:
            raise IOError(f"Received data with BAD checksum: {data})")
        if data == b"\x6f\x6e\x80\xbe":
            raise IOError("Received NULL message")
        time.sleep(sleep * self._sleep_multiplier)
        return data

    def _query(self, message, sleep):
        """
        Write to the DDC/CI device and read a response.

        Retries will be attempted in the case of a failure.
        """
        self._write(message, sleep)
        return self._read(sleep)


class VCPCode:

    def __init__(self, vcp, value=None):
        self._vcp = vcp
        self._value = value

    def get_vcp_code(self):
        result = VCPCode._lookup(self._vcp, None)
        if result is not None:
            result = result[0]
        return result

    def get_vcp_name(self):
        result = VCPCode._lookup(self._vcp, None)
        if result is not None:
            result = result[1]
        return result

    def get_value_code(self):
        result = VCPCode._lookup(self._vcp, self._value)
        if result is not None:
            result = result[0]
        return result

    def get_value_name(self):
        result = VCPCode._lookup(self._vcp, self._value)
        if result is not None:
            result = result[1]
        return result

    @staticmethod
    def _lookup(vcp, value):
        try:
            vcp = int(vcp, 0)
        except (TypeError, ValueError):
            pass
        try:
            value = int(vcp, 0)
        except (TypeError, ValueError):
            pass

        for item in VCPCode._codes:
            search = [item[0], item[1]]
            if vcp not in search:
                continue

            if value is None:
                return item

            if item[4] is not None:
                for entry in item[4]:
                    if value in [entry[0], entry[1]]:
                        return entry
            return None
        return None

    # Table generated from https://www.ddcutil.com/vcpinfo_output/
    # (opcode, name, readable, writable, known_values)
    # fmt: off
    _codes = [
        (0x01, "degauss", False, True, None),
        (0x02, "new-control-value", True, True, [
                (0xFF, "no-user-controls-are-present"),
                (0x00, "no-button-active"),
            ],
        ),
        (0x03, "soft-controls", True, True, [
                (0x01, "button-1-active"),
                (0x02, "button-2-active"),
                (0x03, "button-3-active"),
                (0x04, "button-4-active"),
                (0x05, "button-5-active"),
                (0x06, "button-6-active"),
                (0x07, "button-7-active"),
            ],
        ),
        (0x04, "restore-factory-defaults", False, True, None),
        (0x05, "restore-factory-brightness-contrast-defaults", False, True, None),
        (0x06, "restore-factory-geometry-defaults", False, True, None),
        (0x08, "restore-color-defaults", False, True, None),
        (0x0A, "restore-factory-tv-defaults", False, True, None),
        (0x0B, "color-temperature-increment", True, False, None),
        (0x0C, "color-temperature-request", True, True, None),
        (0x0E, "clock", True, True, None),
        (0x10, "brightness", True, True, None),
        (0x11, "flesh-tone-enhancement", True, True, None),
        (0x12, "contrast", True, True, None),
        (0x13, "backlight-control", True, True, None),
        (0x14, "select-color-preset", True, True, [
                (0x01, "srgb"),
                (0x02, "display-native"),
                (0x03, "4000-k"),
                (0x04, "5000-k"),
                (0x05, "6500-k"),
                (0x06, "7500-k"),
                (0x07, "8200-k"),
                (0x08, "9300-k"),
                (0x09, "10000-k"),
                (0x0A, "11500-k"),
                (0x0B, "user-1"),
                (0x0C, "user-2"),
                (0x0D, "user-3"),
            ],
        ),
        (0x16, "video-gain-red", True, True, None),
        (0x17, "user-color-vision-compensation", True, True, None),
        (0x18, "video-gain-green", True, True, None),
        (0x1A, "video-gain-blue", True, True, None),
        (0x1C, "focus", True, True, None),
        (0x1E, "auto-setup", True, True, [
                (0x00, "auto-setup-not-active"),
                (0x01, "performing-auto-setup"),
                (0x02, "enable-continuous-periodic-auto-setup"),
            ],
        ),
        (0x1F, "auto-color-setup", True, True, [
                (0x00, "auto-setup-not-active"),
                (0x01, "performing-auto-setup"),
                (0x02, "enable-continuous-periodic-auto-setup"),
            ],
        ),
        (0x20, "horizontal-position-phase", True, True, None),
        (0x22, "horizontal-size", True, True, None),
        (0x24, "horizontal-pincushion", True, True, None),
        (0x26, "horizontal-pincushion-balance", True, True, None),
        (0x28, "horizontal-convergence-r-b", True, True, None),
        (0x29, "horizontal-convergence-m-g", True, True, None),
        (0x2A, "horizontal-linearity", True, True, None),
        (0x2C, "horizontal-linearity-balance", True, True, None),
        (0x2E, "gray-scale-expansion", True, True, None),
        (0x30, "vertical-position-phase", True, True, None),
        (0x32, "vertical-size", True, True, None),
        (0x34, "vertical-pincushion", True, True, None),
        (0x36, "vertical-pincushion-balance", True, True, None),
        (0x38, "vertical-convergence-r-b", True, True, None),
        (0x39, "vertical-convergence-m-g", True, True, None),
        (0x3A, "vertical-linearity", True, True, None),
        (0x3C, "vertical-linearity-balance", True, True, None),
        (0x3E, "clock-phase", True, True, None),
        (0x40, "horizontal-parallelogram", True, True, None),
        (0x41, "vertical-parallelogram", True, True, None),
        (0x42, "horizontal-keystone", True, True, None),
        (0x43, "vertical-keystone", True, True, None),
        (0x44, "rotation", True, True, None),
        (0x46, "top-corner-flare", True, True, None),
        (0x48, "top-corner-hook", True, True, None),
        (0x4A, "bottom-corner-flare", True, True, None),
        (0x4C, "bottom-corner-hook", True, True, None),
        (0x52, "active-control", True, False, None),
        (0x54, "performance-preservation", True, True, None),
        (0x56, "horizontal-moire", True, True, None),
        (0x58, "vertical-moire", True, True, None),
        (0x59, "6-axis-saturation-red", True, True, None),
        (0x5A, "6-axis-saturation-yellow", True, True, None),
        (0x5B, "6-axis-saturation-green", True, True, None),
        (0x5C, "6-axis-saturation-cyan", True, True, None),
        (0x5D, "6-axis-saturation-blue", True, True, None),
        (0x5E, "6-axis-saturation-magenta", True, True, None),
        (0x60, "input-source", True, True, [
                (0x01, "vga-1"),
                (0x02, "vga-2"),
                (0x03, "dvi-1"),
                (0x04, "dvi-2"),
                (0x05, "composite-video-1"),
                (0x06, "composite-video-2"),
                (0x07, "s-video-1"),
                (0x08, "s-video-2"),
                (0x09, "tuner-1"),
                (0x0A, "tuner-2"),
                (0x0B, "tuner-3"),
                (0x0C, "component-video-yprpb-ycrcb-1"),
                (0x0D, "component-video-yprpb-ycrcb-2"),
                (0x0E, "component-video-yprpb-ycrcb-3"),
                (0x0F, "displayport-1"),
                (0x10, "displayport-2"),
                (0x11, "hdmi-1"),
                (0x12, "hdmi-2"),
            ],
        ),
        (0x62, "audio-speaker-volume", True, True, None),
        (0x63, "speaker-select", True, True, [
                (0x00, "front-l-r"),
                (0x01, "side-l-r"),
                (0x02, "rear-l-r"),
                (0x03, "center-subwoofer"),
            ],
        ),
        (0x64, "audio-microphone-volume", True, True, None),
        (0x66, "ambient-light-sensor", True, True, [
                (0x01, "disabled"),
                (0x02, "enabled"),
            ],
        ),
        (0x6B, "backlight-level-white", True, True, None),
        (0x6C, "video-black-level-red", True, True, None),
        (0x6D, "backlight-level-red", True, True, None),
        (0x6E, "video-black-level-green", True, True, None),
        (0x6F, "backlight-level-green", True, True, None),
        (0x70, "video-black-level-blue", True, True, None),
        (0x71, "backlight-level-blue", True, True, None),
        (0x72, "gamma", True, True, None),
        (0x73, "lut-size", True, False, None),
        (0x74, "single-point-lut-operation", True, True, None),
        (0x75, "block-lut-operation", True, True, None),
        (0x76, "remote-procedure-call", False, True, None),
        (0x78, "display-identification-operation", True, False, None),
        (0x7A, "adjust-focal-plane", True, True, None),
        (0x7C, "adjust-zoom", True, True, None),
        (0x7E, "trapezoid", True, True, None),
        (0x80, "keystone", True, True, None),
        (0x82, "horizontal-mirror-flip", True, True, [
                (0x00, "normal-mode"),
                (0x01, "mirrored-horizontally-mode"), ],
        ),
        (0x84, "vertical-mirror-flip", True, True, [
                (0x00, "normal-mode"),
                (0x01, "mirrored-vertically-mode"),
            ],
        ),
        (0x86, "display-scaling", True, True, [
                (0x01, "no-scaling"),
                (0x02, "max-image-no-aspect-ration-distortion"),
                (0x03, "max-vertical-image-no-aspect-ratio-distortion"),
                (0x04, "max-horizontal-image-no-aspect-ratio-distortion"),
                (0x05, "max-vertical-image-with-aspect-ratio-distortion"),
                (0x06, "max-horizontal-image-with-aspect-ratio-distortion"),
                (0x07, "linear-expansion-compression-on-horizontal-axis"),
                (0x08, "linear-expansion-compression-on-h-and-v-axes"),
                (0x09, "squeeze-mode"),
                (0x0A, "non-linear-expansion"),
            ],
        ),
        (0x87, "sharpness", True, True, [
                (0x01, "filter-function-1"),
                (0x02, "filter-function-2"),
                (0x03, "filter-function-3"),
                (0x04, "filter-function-4"),
            ],
        ),
        (0x88, "velocity-scan-modulation", True, True, None),
        (0x8A, "color-saturation", True, True, None),
        (0x8B, "tv-channel-up-down", False, True, [
                (0x01, "increment-channel"),
                (0x02, "decrement-channel"),
            ],
        ),
        (0x8C, "tv-sharpness", True, True, None),
        (0x8D, "audio-mute-screen-blank", True, True, [
                (0x01, "mute-the-audio"),
                (0x02, "unmute-the-audio"),
            ],
        ),
        (0x8E, "tv-contrast", True, True, None),
        (0x8F, "audio-treble", True, True, None),
        (0x90, "hue", True, True, None),
        (0x91, "audio-bass", True, True, None),
        (0x92, "tv-black-level-luminesence", True, True, None),
        (0x93, "audio-balance-l-r", True, True, None),
        (0x94, "audio-processor-mode", True, True, [
                (0x00, "speaker-off-audio-not-supported"),
                (0x01, "mono"),
                (0x02, "stereo"),
                (0x03, "stereo-expanded"),
                (0x11, "srs-2.0"),
                (0x12, "srs-2.1"),
                (0x13, "srs-3.1"),
                (0x14, "srs-4.1"),
                (0x15, "srs-5.1"),
                (0x16, "srs-6.1"),
                (0x17, "srs-7.1"),
                (0x21, "dolby-2.0"),
                (0x22, "dolby-2.1"),
                (0x23, "dolby-3.1"),
                (0x24, "dolby-4.1"),
                (0x25, "dolby-5.1"),
                (0x26, "dolby-6.1"),
                (0x27, "dolby-7.1"),
                (0x31, "thx-2.0"),
                (0x32, "thx-2.1"),
                (0x33, "thx-3.1"),
                (0x34, "thx-4.1"),
                (0x35, "thx-5.1"),
                (0x36, "thx-6.1"),
                (0x37, "thx-7.1"),
            ],
        ),
        (0x95, "window-position-tl_x", True, True, None),
        (0x96, "window-position-tl_y", True, True, None),
        (0x97, "window-position-br_x", True, True, None),
        (0x98, "window-position-br_y", True, True, None),
        (0x99, "window-control-on-off", True, True, [
                (0x00, "no-effect"),
                (0x01, "off"),
                (0x02, "on"),
            ],
        ),
        (0x9A, "window-background", True, True, None),
        (0x9B, "6-axis-hue-control-red", True, True, None),
        (0x9C, "6-axis-hue-control-yellow", True, True, None),
        (0x9D, "6-axis-hue-control-green", True, True, None),
        (0x9E, "6-axis-hue-control-cyan", True, True, None),
        (0x9F, "6-axis-hue-control-blue", True, True, None),
        (0xA0, "6-axis-hue-control-magenta", True, True, None),
        (0xA2, "auto-setup-on-off", False, True, [
                (0x01, "off"),
                (0x02, "on"),
            ],
        ),
        (0xA4, "window-mask-control", True, True, None),
        (0xA5, "change-the-selected-window", True, True, [
                (0x00, "full-display-image-area-selected-except-active-windows"),
                (0x01, "window-1-selected"),
                (0x02, "window-2-selected"),
                (0x03, "window-3-selected"),
                (0x04, "window-4-selected"),
                (0x05, "window-5-selected"),
                (0x06, "window-6-selected"),
                (0x07, "window-7-selected"),
            ],
        ),
        (0xAA, "screen-orientation", True, False, [
                (0x01, "0-degrees"),
                (0x02, "90-degrees"),
                (0x03, "180-degrees"),
                (0x04, "270-degrees"),
                (0xFF, "display-cannot-supply-orientation"),
            ],
        ),
        (0xAC, "horizontal-frequency", True, False, None),
        (0xAE, "vertical-frequency", True, False, None),
        (0xB0, "settings", False, True, [
                (0x01, "store-current-settings-in-the-monitor"),
                (0x02, "restore-factory-defaults-for-current-mode"),
            ],
        ),
        (0xB2, "flat-panel-sub-pixel-layout", True, False, [
                (0x00, "sub-pixel-layout-not-defined"),
                (0x01, "red-green-blue-vertical-stripe"),
                (0x02, "red-green-blue-horizontal-stripe"),
                (0x03, "blue-green-red-vertical-stripe"),
                (0x04, "blue-green-red-horizontal-stripe"),
                (0x05, "quad-pixel-red-at-top-left"),
                (0x06, "quad-pixel-red-at-bottom-left"),
                (0x07, "delta-triad"),
                (0x08, "mosaic"),
            ],
        ),
        (0xB4, "source-timing-mode", True, True, None),
        (0xB6, "display-technology-type", True, False, [
                (0x01, "crt-shadow-mask"),
                (0x02, "crt-aperture-grill"),
                (0x03, "lcd-active-matrix"),
                (0x04, "lcos"),
                (0x05, "plasma"),
                (0x06, "oled"),
                (0x07, "el"),
                (0x08, "mem"),
            ],
        ),
        (0xB7, "monitor-status", True, False, None),
        (0xB8, "packet-count", True, True, None),
        (0xB9, "monitor-x-origin", True, True, None),
        (0xBA, "monitor-y-origin", True, True, None),
        (0xBB, "header-error-count", True, True, None),
        (0xBC, "body-crc-error-count", True, True, None),
        (0xBD, "client-id", True, True, None),
        (0xBE, "link-control", True, True, None),
        (0xC0, "display-usage-time", True, False, None),
        (0xC2, "display-descriptor-length", True, False, None),
        (0xC3, "transmit-display-descriptor", True, True, None),
        (0xC4, "enable-display-of-'display-descriptor'", True, True, None),
        (0xC6, "application-enable-key", True, False, None),
        (0xC8, "display-controller-type", True, False, [
                (0x01, "conexant"),
                (0x02, "genesis"),
                (0x03, "macronix"),
                (0x04, "idt"),
                (0x05, "mstar"),
                (0x06, "myson"),
                (0x07, "phillips"),
                (0x08, "pixelworks"),
                (0x09, "realtek"),
                (0x0A, "sage"),
                (0x0B, "silicon-image"),
                (0x0C, "smartasic"),
                (0x0D, "stmicroelectronics"),
                (0x0E, "topro"),
                (0x0F, "trumpion"),
                (0x10, "welltrend"),
                (0x11, "samsung"),
                (0x12, "novatek"),
                (0x13, "stk"),
                (0x14, "silicon-optics"),
                (0x15, "texas-instruments"),
                (0x16, "analogix"),
                (0x17, "quantum-data"),
                (0x18, "nxp-semiconductors"),
                (0x19, "chrontel"),
                (0x1A, "parade-technologies"),
                (0x1B, "thine-electronics"),
                (0x1C, "trident"),
                (0x1D, "micros"),
                (0xFF, "not-defined-a-manufacturer-designed-controller"),
            ],
        ),
        (0xC9, "display-firmware-level", True, False, None),
        (0xCA, "osd-button-control", True, True, [
                (0x01, "osd-disabled"),
                (0x02, "osd-enabled"),
                (0xFF, "display-cannot-supply-this-information"),
            ],
        ),
        (0xCC, "osd-language", True, True, [
                (0x00, "reserved-value-must-be-ignored"),
                (0x01, "chinese-traditional-hantai"),
                (0x02, "english"),
                (0x03, "french"),
                (0x04, "german"),
                (0x05, "italian"),
                (0x06, "japanese"),
                (0x07, "korean"),
                (0x08, "portuguese-portugal"),
                (0x09, "russian"),
                (0x0A, "spanish"),
                (0x0B, "swedish"),
                (0x0C, "turkish"),
                (0x0D, "chinese-simplified-kantai"),
                (0x0E, "portuguese-brazil"),
                (0x0F, "arabic"),
                (0x10, "bulgarian"),
                (0x11, "croatian"),
                (0x12, "czech"),
                (0x13, "danish"),
                (0x14, "dutch"),
                (0x15, "estonian"),
                (0x16, "finnish"),
                (0x17, "greek"),
                (0x18, "hebrew"),
                (0x19, "hindi"),
                (0x1A, "hungarian"),
                (0x1B, "latvian"),
                (0x1C, "lithuanian"),
                (0x1D, "norwegian"),
                (0x1E, "polish"),
                (0x1F, "romanian"),
                (0x20, "serbian"),
                (0x21, "slovak"),
                (0x22, "slovenian"),
                (0x23, "thai"),
                (0x24, "ukranian"),
                (0x25, "vietnamese"),
            ],
        ),
        (0xCD, "status-indicators", True, True, None),
        (0xCE, "auxiliary-display-size", True, False, None),
        (0xCF, "auxiliary-display-data", False, True, None),
        (0xD0, "output-select", True, True, [
                (0x01, "analog-video-r-g-b-1"),
                (0x02, "analog-video-r-g-b-2"),
                (0x03, "digital-video-tdms-1"),
                (0x04, "digital-video-tdms-22"),
                (0x05, "composite-video-1"),
                (0x06, "composite-video-2"),
                (0x07, "s-video-1"),
                (0x08, "s-video-2"),
                (0x09, "tuner-1"),
                (0x0A, "tuner-2"),
                (0x0B, "tuner-3"),
                (0x0C, "component-video-yprpb-ycrcb-1"),
                (0x0D, "component-video-yprpb-ycrcb-2"),
                (0x0E, "component-video-yprpb-ycrcb-3"),
                (0x0F, "displayport-1"),
                (0x10, "displayport-2"),
                (0x11, "hdmi-1"),
                (0x12, "hdmi-2"),
            ],
        ),
        (0xD2, "asset-tag", True, True, None),
        (0xD4, "stereo-video-mode", True, True, None),
        (0xD6, "power-mode", True, True, [
                (0x01, "dpm-on-dpms-off"),
                (0x02, "dpm-off-dpms-standby"),
                (0x03, "dpm-off-dpms-suspend"),
                (0x04, "dpm-off-dpms-off"),
                (0x05, "write-only-value-to-turn-off-display"),
            ],
        ),
        (0xD7, "auxiliary-power-output", True, True, [
                (0x01, "disable-auxiliary-power"),
                (0x02, "enable-auxiliary-power"),
            ],
        ),
        (0xDA, "scan-mode", True, True, [
                (0x00, "normal-operation"),
                (0x01, "underscan"),
                (0x02, "overscan"),
                (0x03, "widescreen"), ],
        ),
        (0xDB, "image-mode", True, True, [
                (0x00, "no-effect"),
                (0x01, "full-mode"),
                (0x02, "zoom-mode"),
                (0x03, "squeeze-mode"),
                (0x04, "variable"),
            ],
        ),
        (0xDC, "display-mode", True, True, [
                (0x00, "standard-default-mode"),
                (0x01, "productivity"),
                (0x02, "mixed"),
                (0x03, "movie"),
                (0x04, "user-defined"),
                (0x05, "games"),
                (0x06, "sports"),
                (0x07, "professional-all-signal-processing-disabled"),
                (0x08, "standard-default-mode-with-intermediate-power-consumption"),
                (0x09, "standard-default-mode-with-low-power-consumption"),
                (0x0A, "demonstration"),
                (0xF0, "dynamic-contrast"),
            ],
        ),
        (0xDE, "scratch-pad", True, True, None),
        (0xDF, "vcp-version", True, False, None),
    ]


class DDCParser:
    @staticmethod
    def _find_next_char(source, match, pos):
        """
        Search the input string for any of the requested "match" characters.

        ## Examples:
        >>> DDCParser._find_next_char("0123456789", "765", 0)
        5
        >>> DDCParser._find_next_char("1 (A B)", "()", 0)
        2
        """
        char_indicies = [source.find(x, pos) for x in match]
        valid_indicies = [idx for idx in char_indicies if idx >= 0]
        if len(valid_indicies) == 0:
            return -1
        return min(valid_indicies)

    @staticmethod
    def _parse_tree(source, sep="", pos=0):
        """
        Parse a string of paren-nested nodes into a nested list.

        An additional separator character may optionally be provided to
        interpret depimeted items as sepearate items in the list.

        ## Examples:
        >>> DDCParser._parse_tree('1 2 3')
        ['1 2 3']
        >>> DDCParser._parse_tree('1 2 3', sep = ' ')
        ['1', '2', '3']
        >>> DDCParser._parse_tree('1 (A B)')
        ['1 ', ['A B']]
        >>> DDCParser._parse_tree('1 (A B)', sep = ' ')
        ['1', ['A', 'B']]
        >>> DDCParser._parse_tree('1 2 (A B (a)) 3 (! @)')
        ['1 2 ', ['A B ', ['a']], ' 3 ', ['! @']]
        >>> DDCParser._parse_tree('1 2 (A B (a)) 3 (! @)', sep = ' ')
        ['1', '2', ['A', 'B', ['a']], '3', ['!', '@']]
        >>> DDCParser._parse_tree('(1 2 3 (a b))')
        [['1 2 3 ', ['a b']]]
        >>> DDCParser._parse_tree('(1 2 3 (a b))', sep = ' ')
        [['1', '2', '3', ['a', 'b']]]
        """
        result = []
        while pos < len(source):
            # print(f"Scanning for next tree token after pos={pos}")
            token_idx = DDCParser._find_next_char(source, "()" + sep, pos)
            if token_idx == -1:
                result.append(source[pos:])
                return result
            elif source[token_idx] == "(":
                if pos != token_idx:
                    result.append(source[pos:token_idx])
                child, pos = DDCParser._parse_tree(source, sep, token_idx + 1)
                result.append(child)
            elif source[token_idx] == ")":
                if pos != token_idx:
                    result.append(source[pos:token_idx])
                return (result, token_idx + 1)
            else:
                if pos != token_idx:
                    result.append(source[pos:token_idx])
                pos = token_idx + 1
        return result

    @staticmethod
    def _unparse_tree(tree, sep=""):
        """
        Unparse a nested list into a string of paren-nested nodes.

        An additional separator character may optionally be provided to
        keep items properly separated.

        ## Examples:
        >>> DDCParser._unparse_tree(['1 2 3'])
        '1 2 3'
        >>> DDCParser._unparse_tree(['1', '2', '3'], sep = ' ')
        '1 2 3'
        >>> DDCParser._unparse_tree(['1 ', ['A B']])
        '1 (A B)'
        >>> DDCParser._unparse_tree(['1', ['A', 'B']], sep = ' ')
        '1 (A B)'
        >>> DDCParser._unparse_tree(['1 2 ', ['A B ', ['a']], ' 3 ', ['! @']])
        '1 2 (A B (a)) 3 (! @)'
        >>> DDCParser._unparse_tree(['1', '2', ['A', 'B', ['a']], '3', ['!', '@']], sep = ' ')
        '1 2 (A B (a)) 3 (! @)'
        >>> DDCParser._unparse_tree([['1 2 3 ', ['a b']]])
        '(1 2 3 (a b))'
        >>> DDCParser._unparse_tree([['1', '2', '3', ['a', 'b']]], sep = ' ')
        '(1 2 3 (a b))'
        """
        result = ""
        for node in tree:
            if isinstance(node, list):
                node = f"({DDCParser._unparse_tree(node, sep)})"
            result += node
            result += sep
        if len(sep) > 0:
            result = result[0 : -len(sep)]
        return result

    @staticmethod
    def _convert_blobtree(tree):
        """
        Convert a nested-list tree of blobs into a nested-dictionary tree of
        blobs.

        This function walks the nested list, taking each item as a key value
        and asssigning it a value of either 'None' (if the item is not followed
        by a nested list) or the converted sub-tree. Keys are assumed to be
        hexadecimal numbers and will be converted to integers.

        ### Examples:
        >>> DDCParser._convert_blobtree(['01', '02', 'FF', ['00']])
        {1: None, 2: None, 255: {0: None}}
        """
        previous_key = None
        result = {}
        for node in tree:
            if isinstance(node, list):
                if previous_key is None:
                    raise ValueError("Discovered child without context")
                key = previous_key
                value = DDCParser._convert_blobtree(node)
                result[key] = value
                previous_key = None
            else:
                key = int(node, 16)
                previous_key = key
                result[key] = None
        return result

    @staticmethod
    def parse_capabilities(raw_caps):
        """
        Parse the capabilities string returned from DDC/CI into a nested-
        dictionary structure that we can query.

        ## Examples:
        >>> raw_caps = "(prot(monitor)type(LCD)model(Wacom Cintiq \
13HD)cmds(01 02 03 07 0C E3 F3)vcp(02 04 08 10 12 14(04 05 08 0B) 16 18 1A \
52 6C 6E 70 86(03 08) AC AE B6 C8 DF)mswhql(1)asset_eep(40)mccs_ver(2.1))"
        >>> DDCParser.parse_capabilities(raw_caps)
        {'prot': 'monitor', 'type': 'LCD', 'model': 'Wacom Cintiq 13HD', \
'cmds': {1: None, 2: None, 3: None, 7: None, 12: None, 227: None, \
243: None}, 'vcp': {2: None, 4: None, 8: None, 16: None, 18: None, \
20: {4: None, 5: None, 8: None, 11: None}, 22: None, 24: None, \
26: None, 82: None, 108: None, 110: None, 112: None, 134: {3: None, \
8: None}, 172: None, 174: None, 182: None, 200: None, 223: None}, \
'mswhql': '1', 'asset_eep': '40', 'mccs_ver': '2.1'}
        """
        result = {}

        tree = DDCParser._parse_tree(raw_caps, " ")
        if len(tree) != 1:
            raise ValueError("Unexpected data outside of capabilities root")

        tree = tree[0]
        it = iter(tree)
        for node in it:
            key = node.strip()
            value = next(it)
            if key in ["cmds", "vcp"]:
                value = DDCParser._convert_blobtree(value)
            else:
                value = DDCParser._unparse_tree(value, " ")
            result[node] = value
        return result

    @staticmethod
    def list_capabilities(tree):
        """
        Produce a user-friendly representation of the capabilities in
        string format.
        """
        result = ""
        for key in tree.keys():
            if key == "vcp":
                result += f"{key}: \n"
                for vcp in tree[key].keys():
                    vcp_name = VCPCode(vcp).get_vcp_name()
                    result += f"  - {vcp} ({vcp_name})\n"
                    if tree[key][vcp] is not None:
                        for value in tree[key][vcp].keys():
                            value_name = VCPCode(vcp, value).get_value_name()
                            result += f"      - {value} ({value_name})\n"
            elif key == "cmds":
                result += f"{key}: {list(tree[key].keys())}\n"
            else:
                result += f"{key}: {tree[key]}\n"
        return result


class DDCSession:

    def __init__(self, path):
        self._device = None
        self._interface = None
        self._raw_caps = None
        self._path = path

    @staticmethod
    def _get_ddc_device(path):
        if path.startswith("ftdi://"):
            return FtdiDevice(path)
        elif path.startswith("/dev/i2c-"):
            return I2cDevice(path)
        return SerialDevice(path)

    def __enter__(self):
        self._device = DDCSession._get_ddc_device(self._path).__enter__()
        self._interface = DDCInterface(self._device)
        print("Requesting features...")
        self._raw_caps = self._interface.request_capabilities()
        return self

    def __exit__(self, *args):
        self._device.__exit__()
        self._interface = None
        self._raw_caps = None

    def print_cap_dump(self):
        print(self._raw_caps)

    def print_cap_info(self):
        parsed_caps = DDCParser.parse_capabilities(self._raw_caps)
        print(DDCParser.list_capabilities(parsed_caps))

    @staticmethod
    def _get_vcp_code(vcp):
        try:
            return int(vcp, 0)
        except ValueError:
            return VCPCode(vcp).get_vcp_code()

    @staticmethod
    def _get_value_code(value):
        try:
            return int(value, 0)
        except ValueError:
            return VCPCode(value).get_value_code()

    def _convert_vcp_and_value(self, vcp, value):
        vcpcode = DDCSession._get_vcp_code(vcp)
        valuecode = DDCSession._get_value_code(value)
        if vcpcode is None:
            print(f"Ignoring unrecognized VCP code '{vcp}'")
            return None
        if valuecode is None and value != "?":
            print(f"Ignoring unrecognized VCP value '{value}'")
            return None
        if DEBUG and (str(vcp) != str(vcpcode) or str(value) != str(valuecode)):
            print(f"Interpreting {vcp}={value} as {vcpcode}={valuecode}")
        return (vcpcode, valuecode)

    def getset(self, vcp, value):
        codes = self._convert_vcp_and_value(vcp, value)
        if codes is None:
            return
        vcpcode, valuecode = codes

        parsed_caps = DDCParser.parse_capabilities(self._raw_caps)
        if vcpcode not in parsed_caps["vcp"].keys():
            print(
                f"Ignoring request for {vcp}: VCP code is not supported by this device."
            )
            return

        if DEBUG:
            print(f"Requesting value of VCP {vcp}...")
        result = self._interface.get_value(vcpcode)
        if result["errno"] != 0:
            print(f"Display does not recognize VCP {vcp}")
            return
        if result["opcode"] != vcpcode:
            print(f"Ignoring unexpected response for VCP {result['opcode']}")
            return

        current = result["value"]
        maximum = result["maximum"]
        current_name = VCPCode(vcpcode, current).get_value_name()
        if current_name is None:
            current_name = current
        if valuecode is None or DEBUG:
            print(f"Current value of VCP {vcp} is {current_name} (maximum = {maximum})")
        if valuecode is None:
            return

        allowed_values = parsed_caps["vcp"][vcpcode]
        if allowed_values is not None and valuecode not in allowed_values.keys():
            print(
                f"Ignoring request to set VCP {vcp} to {value}: Value not one of the supported items: {list(allowed_values.keys())}"
            )
            return
        if valuecode > maximum or valuecode < 0:
            print(
                f"Ignoring request to set VCP {vcp} to {value}: Value is outside the supported range 0..{maximum}"
            )
            return

        self._interface.set_value(vcpcode, valuecode)
        print(f"Set value of VCP {vcp} to {value}")


def main():
    if len(sys.argv) <= 2:
        print(sys.modules[__name__].__doc__)
        return

    path = sys.argv[1]

    with DDCSession(path) as session:
        for arg in sys.argv[2:]:
            getset_match = re.match(r"(.+)=(.+)", arg)

            if arg == "dump":
                session.print_cap_dump()
            elif arg == "list":
                session.print_cap_info()
            elif getset_match is not None:
                vcp = getset_match.group(1)
                value = getset_match.group(2)
                session.getset(vcp, value)
            else:
                print(f"Ignoring unknown argument {arg}")


if __name__ == "__main__":
    main()
