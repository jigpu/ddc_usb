#!/usr/bin/env python3

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
    ddc-ftdi.py <path> dump
    ddc-ftdi.py <path> <VCP=VALUE> [...]

Arguments:
    <path>         Path to the device to be controlled. Must be either
                   a serial device node (e.g. `/dev/ttyUSB0`) or a PyFtdi
                   URL (e.g. `ftdi://ftdi:232h/1`).
    dump           Instruct the program to dump the raw capability list
    <VCP>          "Virtual Control Panel" code.
    <VALUE>        Value to assign to the specified VCP. May use the
                   special value "?" to get the current value instead of
                   setting it.

    The exact path required for any given device will depend on the bridge
    chip in use. Devices using a Silicon Labs CP210x bridge chip should be
    able to use a serial device node path. Devices which use an FTDI F232H
    bridge chip, however, will need to use an FTDI URL instead. Check the
    output of `lsusb` for information about the type of bridge chip that
    might be in use.

    Numeric values may be specified in decimal or hexadecimal (e.g. the
    value "10" may alternatively be specified as "0x0A").

    See the following websites for more information about VCP codes and
    known values:
      - https://www.ddcutil.com/vcpinfo_output/
      - http://www.boichat.ch/nicolas/ddcci/specs.html

Examples:
    # Dump the list of capabilities from a monitor at /dev/ttyUSB0
    ddc-ftdi.py /dev/ttyUSB0 dump
    (prot(monitor)type(LCD)model(Wacom Cintiq 13HD)cmds(01 02 03 07 [...]

    # Set brightness (vcp code 0x10) to 10 and contrast (vcp code 12) to 50
    # for a monitor at /dev/ttyUSB0
    ddc-ftdi.py 0x10=10 0x12=50

    # Change color preset (vcp code 0x14) to 6500K (value 0x05) for
    # a monitor at ftdi://ftdi:232h/1
    ddc-ftdi.py 0x14=0x05

    # Get the current red channel gain for a monitor at ftdi://ftdi:232h/1.
    ddc-ftdi.py 0x16=?
"""

from copy import deepcopy
import re
import struct
import sys
import time

from pyftdi.i2c import I2cController
import pyftdi.misc
import serial

DEBUG = True


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

        This function returns a tuple that contains both the current
        value of the VCP and the maximum value it supports.
        """
        # VESA VESA-DDCCI-1.1
        # Section 4.3 "Get VCP Feature & VCP Feature Reply"
        if control > 255 or control < 0:
            raise ValueError(f"Invalid VCP control code {control}")
        message = b"\x6e\x51\x82\x01" + bytes({control})
        data = self._query(message, sleep=0.040)
        maximum = int.from_bytes(data[7:9], "big")
        value = int.from_bytes(data[9:11], "big")
        return (value, maximum)

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
        fn = lambda m=message, s=sleep: self._write_once(m, s)
        return self._retry(fn)

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
        fn = lambda: self._read_once(sleep)
        return self._retry(fn)

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
        data = b"\x6F"
        data = data + self._ddc_device.read(2)
        if data[1] != 0x6E:
            raise IOError(f"Unexpected reply from address {data[1]}")
        data = data + self._ddc_device.read((data[2] & 0x7F) + 1)
        x = DDCInterface._checksum(data, 0x50)
        if DEBUG:
            print(f"Read: {pyftdi.misc.hexline(data)}")
        if x != 0:
            raise IOError(f"Received data with BAD checksum: {data})")
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
    codes = [
        (0x01, "degauss", None),
        (0x02, "new-control-value", None),
        (0x03, "soft-controls", None),
        (0x04, "restore-factory-defaults", None),
        (0x05, "restore-factory-brightness-contrast", None),
        (0x06, "restore-factory-geometry", None),
        (0x08, "restore-factory-color", None),
        (0x0A, "restore-factory-tv", None),
        (0x0B, "color-temperature-increment", None),
        (0x0C, "color-temperature-request", None),
        (0x0E, "clock", None),
        (0x10, "brightness", None),
        (0x11, "flesh-tone-enhancement", None),
        (0x12, "contrast", None),
        (0x13, "backlight-control", None),
        (
            0x14,
            "color-preset",
            [
                (0x01, "srgb", None),
                (0x02, "native", None),
                (0x03, "4000K", None),
                (0x04, "5000K", None),
                (0x05, "6500K", None),
                (0x06, "7500K", None),
                (0x07, "8200K", None),
                (0x08, "9300K", None),
                (0x09, "10000K", None),
                (0x0A, "11500K", None),
                (0x0B, "User 1", None),
                (0x0C, "User 2", None),
                (0x0D, "User 3", None),
            ],
        ),
        (0x16, "video-gain-r", None),
        (0x17, "color-vision-compensation", None),
        (0x18, "video-gain-g", None),
        (0x1A, "video-gain-b", None),
        (0x1C, "focus", None),
        (
            0x1E,
            "auto-setup",
            [
                (0x00, "not-active", None),
                (0x01, "in-progress", None),
                (0x02, "continuous-periodic", None),
            ],
        ),
        (
            0x1F,
            "auto-color-setup",
            [
                (0x00, "not-active", None),
                (0x01, "in-progress", None),
                (0x02, "continuous-periodic", None),
            ],
        ),
        (0x20, "horizontal-position", None),
        (0x22, "horizontal-size", None),
        (0x24, "horizontal-pincushion", None),
        (0x26, "horizontal-pincushion-balance", None),
        (0x28, "horizontal-convergence-rb", None),
        (0x29, "horizontal-convergence-mg", None),
        (0x2A, "horizontal-linearity", None),
        (0x2C, "horizontal-linearity-balance", None),
        (0x2E, "gray-scale-expansion", None),
        (0x30, "vertical-position", None),
        (0x32, "vertical-size", None),
        (0x34, "veritcal-pincushion", None),
        (0x36, "veritcal-pincushion-balance", None),
        (0x38, "veritcal-convergence-rb", None),
        (0x39, "veritcal-convergence-mg", None),
        (0x3A, "veritcal-linearity", None),
        (0x3C, "veritcal-linearity-balance", None),
        (0x3E, "clock-phase", None),
        (0x40, "horizontal-parallelogram", None),
        (0x41, "vertical-parallelogram", None),
        (0x42, "horizontal-keystone", None),
        (0x43, "vertical-keystone", None),
        (0x44, "rotation", None),
        (0x46, "top-corner-flare", None),
        (0x48, "top-corner-hook", None),
        (0x4A, "bottom-corner-flare", None),
        (0x4C, "bottom-corner-hook", None),
        (0x52, "active-control", None),
        (0x54, "performance-preservation", None),
        (0x56, "horizontal-moire", None),
        (0x58, "vertical-moire", None),
        (0x59, "six-axis-saturation-r", None),
        (0x5A, "six-axis-saturation-y", None),
        (0x5B, "six-axis-saturation-g", None),
        (0x5C, "six-axis-saturation-c", None),
        (0x5D, "six-axis-saturation-b", None),
        (0x5E, "six-axis-saturation-m", None),
        (
            0x60,
            "input-source",
            [
                (0x01, "vga-1", None),
                (0x02, "vga-2", None),
                (0x03, "dvi-1", None),
                (0x04, "dvi-2", None),
                (0x05, "composite-1", None),
                (0x06, "composite-2", None),
                (0x07, "svideo-1", None),
                (0x08, "svideo-2", None),
                (0x09, "tuner-1", None),
                (0x0A, "tuner-2", None),
                (0x0B, "tuner-3", None),
                (0x0C, "component-1", None),
                (0x0D, "component-2", None),
                (0x0E, "component-3", None),
                (0x0F, "dp-1", None),
                (0x10, "dp-2", None),
                (0x11, "hdmi-1", None),
                (0x12, "hdmi-2", None),
            ],
        ),
    ]

    @staticmethod
    def _item_lookup(code, value):
        for item in VCPCode.codes:
            if item[0] == code:
                if value is None:
                    return item
                for entry in item[2]:
                    if entry[0] == value:
                        return entry
                return None
        return None

    @staticmethod
    def _name_lookup(code, value):
        for item in VCPCode.codes:
            if code in [item[0], item[1]]:
                if value is None:
                    return item
                for entry in item[2]:
                    if value in [entry[0], entry[1]]:
                        return entry
                return None
        return None

    @staticmethod
    def code_to_name(code):
        item = VCPCode._item_lookup(code, None)
        if item is not None:
            return item[1]
        return code

    @staticmethod
    def code_value_to_name(code, value):
        item = VCPCode._item_lookup(code, value)
        if item is not None:
            return item[1]
        return value

    @staticmethod
    def name_to_code(codename):
        item = VCPCode._name_lookup(codename, None)
        if item is not None:
            return item[0]
        return None

    @staticmethod
    def name_to_code_value(codename, valuename):
        item = VCPCode._name_lookup(codename, valuename)
        if item is not None:
            return item[0]
        return None


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
    def parse_capabilities(raw_capabilities):
        """
        Parse the capabilities string returned from DDC/CI into a nested-
        dictionary structure that we can query.

        ## Examples:
        >>> raw_capabilities = "(prot(monitor)type(LCD)model(Wacom Cintiq \
13HD)cmds(01 02 03 07 0C E3 F3)vcp(02 04 08 10 12 14(04 05 08 0B) 16 18 1A \
52 6C 6E 70 86(03 08) AC AE B6 C8 DF)mswhql(1)asset_eep(40)mccs_ver(2.1))"
        >>> DDCParser.parse_capabilities(raw_capabilities)
        {'prot': 'monitor', 'type': 'LCD', 'model': 'Wacom Cintiq 13HD', \
'cmds': {1: None, 2: None, 3: None, 7: None, 12: None, 227: None, \
243: None}, 'vcp': {2: None, 4: None, 8: None, 16: None, 18: None, \
20: {4: None, 5: None, 8: None, 11: None}, 22: None, 24: None, \
26: None, 82: None, 108: None, 110: None, 112: None, 134: {3: None, \
8: None}, 172: None, 174: None, 182: None, 200: None, 223: None}, \
'mswhql': '1', 'asset_eep': '40', 'mccs_ver': '2.1'}
        """
        result = {}

        tree = DDCParser._parse_tree(raw_capabilities, " ")
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
    def rename_capabilities(tree):
        """
        Replace numeric VCP IDs in a parsed capabilities tree with user-
        friendly names instead.
        """
        vcptree = {}
        codes = tree["vcp"].keys()
        for code in codes:
            codename = VCPCode.code_to_name(code)
            vcptree[codename] = None

            values = tree["vcp"][code]
            if values is not None:
                vcptree[codename] = {}
                for value in values:
                    valuename = VCPCode.code_value_to_name(code, value)
                    vcptree[codename][valuename] = deepcopy(tree["vcp"][code][value])
        result = deepcopy(tree)
        result["vcp"] = vcptree
        return result


def get_ddc_device(path):
    if path.startswith("ftdi://"):
        return FtdiDevice(path)
    return SerialDevice(path)


def main():
    if len(sys.argv) <= 2:
        print(sys.modules[__name__].__doc__)
        return

    path = sys.argv[1]

    with get_ddc_device(path) as device:
        interface = DDCInterface(device)
        print("Requesting features...")
        raw_capabilities = interface.request_capabilities()
        numeric_capabilities = DDCParser.parse_capabilities(raw_capabilities)
        human_capabilities = DDCParser.rename_capabilities(numeric_capabilities)

        for arg in sys.argv[2:]:
            if arg == "dump":
                print(raw_capabilities)
                continue

            if arg == "list":
                print(human_capabilities)
                continue

            match = re.match(r"(.+)=(.+)", arg)
            if match is not None:
                vcp = match.group(1)
                value = match.group(2)
                vcpcode = None
                valuecode = None

                try:
                    vcpcode = int(vcp, 0)
                except ValueError:
                    vcpcode = VCPCode.name_to_code(vcp)
                    if vcpcode is None:
                        print("Ignoring unrecognized VCP code '{vcp}'")
                        continue

                try:
                    if value == "?":
                        valuecode = "?"
                    else:
                        valuecode = int(value, 0)
                except ValueError:
                    valuecode = VCPCode.name_to_code_value(vcp, value)
                    if valuecode is None:
                        print("Ignoring unrecognized VCP value '{value}'")
                        continue

                if str(vcp) != str(vcpcode) or str(value) != str(valuecode):
                    print(f"Interpreting {vcp}={value} as {vcpcode}={valuecode}")

                if vcpcode not in numeric_capabilities["vcp"].keys():
                    print(
                        f"Ignoring request {vcp}={value}: VCP code is not supported by this device."
                    )
                    continue

                print(f"Requesting value of VCP {vcp}...")
                current, maximum = interface.get_value(vcpcode)
                current = VCPCode.code_value_to_name(vcpcode, current)
                print(f"Current value of VCP {vcp} is {current} (maximum = {maximum})")
                if value == "?":
                    pass
                else:
                    allowed_values = numeric_capabilities["vcp"][vcpcode]
                    if (
                        allowed_values is not None
                        and valuecode not in allowed_values.keys()
                    ):
                        print(
                            f"Ignoring request to set VCP {vcp} to {value}: Value not one of the supported items: {list(allowed_values.keys())}"
                        )
                        continue
                    if valuecode > maximum or valuecode < 0:
                        print(
                            f"Ignoring request to set VCP {vcp} to {value}: Value is outside the supported range 0..{maximum}"
                        )
                        continue
                    interface.set_value(vcpcode, valuecode)
                    print(f"Set value of VCP {vcp} to {value}")
                continue

            print(f"Ignoring unexpected command-line argument: {arg}")


if __name__ == "__main__":
    main()
