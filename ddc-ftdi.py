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
    (prot(monitor)type(LCD)model(Wacom Cintiq 13HD)cmds(01 02 03 07 0C E3 F3)vcp(02 04 08 10 12 14(04 05 08 0B) 16 18 1A 52 6C 6E 70 86(03 08) AC AE B6 C8 DF)mswhql(1)asset_eep(40)mccs_ver(2.1))

    # Set brightness (vcp code 0x10) to 10 and contrast (vcp code 12) to 50
    # for a monitor at /dev/ttyUSB0
    ddc-ftdi.py 0x10=10 0x12=50

    # Change color preset (vcp code 0x14) to 6500K (value 0x05) for
    # a monitor at ftdi://ftdi:232h/1
    ddc-ftdi.py 0x14=0x05

    # Get the current red channel gain for a monitor at ftdi://ftdi:232h/1.
    ddc-ftdi.py 0x16=?
"""

import serial
from pyftdi.i2c import I2cController
import pyftdi.misc
import time
import sys
import struct
import re

DEBUG = False


class DDCDevice:
    @staticmethod
    def _checksum(data, addr=0x00):
        sum = addr
        for c in data:
            sum = sum ^ c
        return sum

    def __init__(self):
        self._device_handle = None
        self._retries = 3
        self._sleep_multiplier = 1.5

    def _retry(self, fn, sleep=0.040):
        saved_exception = None
        for attempt in range(0, self._retries):
            try:
                return fn()
            except Exception as e:
                saved_exception = e
                if DEBUG:
                    print(f"Failed attempt #{attempt+1}")
                time.sleep(sleep * self._sleep_multiplier)
        raise saved_exception

    def write(self, message, sleep, include_address=True):
        fn = lambda m=message, s=sleep, i=include_address: self._write_once(m, s, i)
        return self._retry(fn)

    def _write_once(self, message, sleep, include_address=True):
        x = message + bytes({DDCDevice._checksum(message)})
        if not include_address:
            x = x[1:]
        if DEBUG:
            print("Writing: {}".format(pyftdi.misc.hexline(x)))
        self._device_handle.write(x)
        self._device_handle.flush()
        time.sleep(sleep * self._sleep_multiplier)

    def read(self, sleep):
        fn = lambda: self._read_once(sleep)
        return self._retry(fn)

    def _read_once(self, sleep):
        data = self._device_handle.read(2)
        if data[0] != 0x6E:
            raise IOError("Unexpected reply from address {}".format(data[0]))
        data = data + self._device_handle.read((data[1] & 0x7F) + 1)
        x = DDCDevice._checksum(data, 0x50)
        if DEBUG:
            print("Read: {}".format(pyftdi.misc.hexline(data)))
        if x != 0:
            raise IOError("Received data with BAD checksum: {})".format(data))
        # The DDC/CI spec does not indicate any minimum time requied to
        # sleep after reading, but just to be safe, lets allow some time
        # to pass.
        time.sleep(sleep * self._sleep_multiplier)
        return data

    def query(self, message, sleep):
        self.write(message, sleep)
        return self.read(sleep)

    def close(self):
        self._device_handle.close()


class FtdiDevice(DDCDevice):
    def __init__(self, url):
        super().__init__()
        i2c = I2cController()
        i2c.configure(url, frequency=100000.0)
        print("Opened i2c connection to {} at {}Hz".format(url, i2c.frequency))
        self._device_handle = i2c.get_port(0x37)
        self._i2c_master = i2c

    def write(self, message, sleep):
        super().write(message, sleep, False)

    def close(self):
        self._i2c_master.close()


class SerialDevice(DDCDevice):
    def __init__(self, path):
        super().__init__()
        ser = serial.Serial(path)
        print("Opened serial connection")
        self._device_handle = ser


class DDCInterface:
    def __init__(self, ddc_device):
        self._ddc_device = ddc_device

    def close(self):
        self._ddc_device.close()

    def get_value(self, control):
        # VESA VESA-DDCCI-1.1
        # Section 4.3 "Get VCP Feature & VCP Feature Reply"
        message = b"\x6e\x51\x82\x01" + bytes({control})
        data = self._ddc_device.query(message, sleep=0.040)
        return (data[9], data[7])

    def set_value(self, control, value):
        # VESA VESA-DDCCI-1.1
        # Section 4.4 "Set VCP Feature"
        message = b"\x6e\x51\x84\x03" + bytes({control}) + b"\x00" + bytes({value})
        self._ddc_device.write(message, sleep=0.050)

    def save_settings(self):
        # VESA VESA-DDCCI-1.1
        # Section 4.5 "Save Current Settings"
        message = b"\x6e\x51\x81\x0c"
        self._ddc_device.write(message, sleep=0.200)

    def request_features(self):
        # VESA-DDCCI-1.1
        # Section 4.6 "Capabilities Request & Capabilities Reply"
        result = b""
        offset = 0
        while True:
            message = b"\x6e\x51\x83\xf3" + struct.pack(">h", offset)
            self._ddc_device.write(message, sleep=0.050)
            data = self._ddc_device.read(sleep=0.040)
            payload = data[5:-1]
            if len(payload) == 0:
                break
            result += payload
            offset += len(payload)
        return str(result, "ASCII")


class DDCParser:
    @staticmethod
    def _find_next_char(str, match, pos):
        """
        Search the input string for any of the requested "match" characters.

        ## Examples:
        >>> DDCParser._find_next_char("0123456789", "765", 0)
        5
        >>> DDCParser._find_next_char("1 (A B)", "()", 0)
        2
        """
        char_indicies = [str.find(x, pos) for x in match]
        valid_indicies = [idx for idx in char_indicies if idx >= 0]
        if len(valid_indicies) == 0:
            return -1
        return min(valid_indicies)

    @staticmethod
    def _parse_tree(str, sep="", pos=0):
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
        while pos < len(str):
            # print(f"Scanning for next tree token after pos={pos}")
            token_idx = DDCParser._find_next_char(str, "()" + sep, pos)
            if token_idx == -1:
                result.append(str[pos:])
                return result
            elif str[token_idx] == "(":
                if pos != token_idx:
                    result.append(str[pos:token_idx])
                child, pos = DDCParser._parse_tree(str, sep, token_idx + 1)
                result.append(child)
            elif str[token_idx] == ")":
                if pos != token_idx:
                    result.append(str[pos:token_idx])
                return (result, token_idx + 1)
            else:
                if pos != token_idx:
                    result.append(str[pos:token_idx])
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
    def parse_capabilities(str):
        """
		Parse the capabilities string returned from DDC/CI into a nested-
		dictionary structure that we can query.

		## Examples:
		>>> str = "(prot(monitor)type(LCD)model(Wacom Cintiq 13HD) " \
				  "cmds(01 02 03 07 0C E3 F3)vcp(02 04 08 10 12 14 " \
				  "(04 05 08 0B) 16 18 1A 52 6C 6E 70 86(03 08) AC " \
				  "AE B6 C8 DF)mswhql(1)asset_eep(40)mccs_ver(2.1))"
		>>> DDCParser.parse_capabilities(str)
		{'prot': 'monitor', 'type': 'LCD', 'model': 'Wacom Cintiq 13HD', \
'cmds': {1: None, 2: None, 3: None, 7: None, 12: None, 227: None, \
243: None}, 'vcp': {2: None, 4: None, 8: None, 16: None, 18: None, \
20: {4: None, 5: None, 8: None, 11: None}, 22: None, 24: None, \
26: None, 82: None, 108: None, 110: None, 112: None, 134: {3: None, \
8: None}, 172: None, 174: None, 182: None, 200: None, 223: None}, \
'mswhql': '1', 'asset_eep': '40', 'mccs_ver': '2.1'}
		"""
        result = {}

        tree = DDCParser._parse_tree(str, " ")
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


def main():
    if len(sys.argv) <= 2:
        print(sys.modules[__name__].__doc__)
        return

    path = sys.argv[1]
    if path.startswith("ftdi://"):
        device = DDCInterface(FtdiDevice(path))
    else:
        device = DDCInterface(SerialDevice(path))

    try:
        print("Requesting features...")
        features = device.request_features()
        capabilities = DDCParser.parse_capabilities(features)

        for arg in sys.argv[2:]:
            if arg == "dump":
                print(features)
                continue

            match = re.match(r"(.+)=(.+)", arg)
            if match is not None:
                vcp = int(match.group(1), 0)
                value = match.group(2)

                if vcp not in capabilities["vcp"].keys():
                    print(f"Ignoring request {vcp}={value}: VCP code is not supported by this device.")
                    continue

                print(f"Requesting value of VCP {vcp}...")
                current, maximum = device.get_value(vcp)
                print(f"Current value of VCP {vcp} is {current} (maximum = {maximum})")
                if value == "?":
                    pass
                else:
                    value = int(value, 0)
                    allowed_values = capabilities["vcp"][vcp]
                    if (
                        allowed_values is not None
                        and value not in allowed_values.keys()
                    ):
                        print(
                            f"Ignoring request to set VCP {vcp} to {value}: Value not one of the supported items: {list(allowed_values.keys())}"
                        )
                        continue
                    if value > maximum or value < 0:
                        print(
                            f"Ignoring request to set VCP {vcp} to {value}: Value is outside the supported range 0..{maximum}"
                        )
                        continue
                    device.set_value(vcp, value)
                    print(f"Set value of VCP {vcp} to {value}")
                continue

            print(f"Ignoring unexpected command-line argument: {arg}")
    finally:
        device.close()


if __name__ == "__main__":
    main()
