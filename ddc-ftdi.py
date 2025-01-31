#!/usr/bin/python3

# WARNING: THIS CODE IS A WORK-IN-PROGRESS
# IT IS IN THE MIDDLE OF A REFACTOR AND LIKELY DOES NOT WORK AT ALL,
# LET ALONE PROPERLY!
#
# YOU HAVE BEEN WARNED!

"""Control brightness and contrast of monitors exposing DDC over USB.

Displays which do not provide hardware on-screen-display controls for
adjusting brightness, contrast, etc. may still allow adjustment through
software. The `ddcutil` program is often sufficient, but some displays
use bridge chips that aren't compatible. This program is specifically
written for the Wacom Cintiq 13HD and Cintiq Pro 24/32 which use the
Silicon Labs CP210x and FTDI F232H bridge chips respectively.

Usage:
    ddc-ftdi.py [<brightness> <contrast>]

Arguments:
    <brightness>   Brightness value to set (0..100)
    <contrast>     Contrast value to set (0..100)
"""

import serial
from pyftdi.i2c import I2cController
import pyftdi.misc
import time
import sys
import struct

DEBUG=True

class DDCInterface:
	def checksum(data, addr=0x00):
		sum = addr
		for c in data:
			sum = sum ^ c
		return sum

	def __init__(self):
		self.device = None

	def write(self, message, sleep=0.04, include_address=True):
		x = message + bytes({DDCInterface.checksum(message)})
		if not include_address:
			x = x[1:]
		if DEBUG:
			print("Writing: {}".format(pyftdi.misc.hexline(x)))
		self.device.write(x)
		time.sleep(sleep)

	def read(self):
		data = self.device.read(2)
		if data[0] != 0x6E:
			raise IOError("Unexpected reply from address {}".format(data[0]))
		data = data + self.device.read((data[1] & 0x7F) + 1)
		x = DDCInterface.checksum(data, 0x50)
		if DEBUG:
			print("Read: {}".format(pyftdi.misc.hexline(data)))
		if x != 0:
			raise IOError("Received data with BAD checksum: {})".format(data))
		return data

	def query(self, message, sleep=0.04):
		self.ddc_write(message, sleep)
		return self.ddc_read()

class FtdiInterface(DDCInterface):
	def __init__(self):
		i2c = I2cController()
		i2c.configure('ftdi://ftdi:232h/1', frequency=100000.0)
		print("Opened i2c connection at {}Hz".format(i2c.frequency))
		self.device = i2c.get_port(0x37)

	def write(self, message, sleep=0.04):
		super(DDCInterface, self).write(message, sleep, False)


class SerialInterface(DDCInterface):
	def __init__(self):
		ser = serial.Serial('/dev/ttyUSB0')
		print("Opened serial connection")
		self.device = ser


class DDCDevice:
	def __init__(self, interface):
		self.interface = interface

	def get_value(self, control):
		message = b'\x6e\x51\x82\x01' + bytes({control})
		data = self.interface.query(message)
		print("Maximum: {}, Current: {}".format(data[7], data[9]))

	def set_value(self, control, value):
		message = b'\x6e\x51\x84\x03' + bytes({control}) + b'\x00' + bytes({value})
		self.interface.write(message)
		data = self.interface.read()

	def request_features(self):
		# VESA-DDCCI-1.1
		# Section 4.6 "Capabilities Request & Capabilities Reply"
		result = b''
		offset = 0
		while True:
			message = b'\x6e\x51\x83\xf3' + struct.pack(">h", offset)
			self.interface.write(message, 0.05)
			data = self.interface.read()
			payload = data[5:-1]
			if len(payload) == 0:
				break
			result += payload
			offset += len(payload)
		return str(result, 'ASCII')


class DDC_VCP:
	# http://www.boichat.ch/nicolas/ddcci/specs.html
	# https://www.ddcutil.com/vcpinfo_output/
	controls = {
	    'factory_reset': 0x04,
	    'factory_color': 0x08,
	    'brightness':    0x10,
	    'contrast':      0x12,
	    'color_preset':  0x14,
	    'gain_red':      0x16,
	    'gain_green':    0x18,
	    'gain_blue':     0x1A,
	    'offset_red':    0x6C,
	    'offset_green':  0x6E,
	    'offset_blue':   0x70,
	}
	color_presets = {
	    'sRGB':   1,
	    'native': 2,
	    '5000K':  4,
	    '6500K':  5,
	    '9300K':  8,
	}

import string
def parse_features(s, i):
    items = []
    name = ''
    while i < len(s):
        char = s[i]
        if char == ' ':
            if name != '':
                items.append(name)
                name = ''
        elif char == '(':
            child, i = parse_features(s, i+1)
            items.append({name: child})
            name = ''
        elif char == ')':
            items.append(name)
            break
        else:
            name += char
        i += 1
    is_string = all(isinstance(item, str) for item in items)
    is_hex = is_string and \
        all(len(item) == 2 for item in items) and \
        all(all(c in string.hexdigits for c in item) for item in items)
    if is_string and not is_hex: 
        items = " ".join(items)
    return items, i

def dict_reverse_lookup(search_dict, value):
	result = [k for k,v in search_dict.items() if v == value]
	if len(result) > 0:
		return result
	else:
		return None

def decode_hex(context, hexstring):
	is_hex = isinstance(hexstring, str) and \
	    len(hexstring) == 2 and \
	    all(c in string.hexdigits for c in hexstring)
	if not is_hex:
		return hexstring
	value = int(hexstring, 16)
	if context == ['vcp']:
		name = dict_reverse_lookup(DDC_VCP.controls, value)
		return name[0] if name is not None else value
	if context == ['vcp', 'color_preset']:
		name = dict_reverse_lookup(DDC_VCP.color_presets, value)
		return name[0] if name is not None else value

def decode_features(f, context):
	for idx, item in enumerate(f):
		print("Decoding {} in context {}".format(item, context))
		item = decode_hex(context, item)
		if isinstance(item, str) or isinstance(item, int):
			f[idx] = item
		elif isinstance(item, dict):
			k,v = next(iter(item.items()))
			decode_features(v, context + [k])
		elif isinstance(item, list):
			decode_features(item, context)




def decode_features(feature_string):
	if feature_string[0] != "(" or
	   feature_string[-1] != ")":
		return feature_string
	dict = {}
	while True:
		key = # everything up to (
		value = decode_features() # everything up to balanced )
		dict[key] = value
	#... TODO ..

def decode_features(feature_string):
	if feature_string[0] != "(" or
	   feature_string[-1] != ")":
		return feature_string
	dict = {}
	while True:
		
		value = decode_features() # everything up to balanced )
		dict[key] = value
	#... TODO ..

def main():
	if len(sys.argv) != 3:
		brightness = 10
		contrast = 50
	else:
		brightness = int(sys.argv[1])
		contrast = int(sys.argv[2])

	if False:
		device = DDCDevice(FtdiInterface())
	else:
		device = DDCDevice(SerialInterface())

	# Example:
	# (prot(monitor)type(LCD)model(Wacom Cintiq 13HD)cmds(01 02 03 07 0C E3 F3)vcp(02 04 08 10 12 14(04 05 08 0B) 16 18 1A 52 6C 6E 70 86(03 08) AC AE B6 C8 DF)mswhql(1)asset_eep(40)mccs_ver(2.1))
	# prot: [monitor]
	# type: [LCD]
	# model: [Wacom Cintiq 13 HD]
	# cmds: [01 02 03 07 0C E3 F3]
	# VCP: [02 04 08 10 12 {14: [04 05 08 0B]} 16 18 1A 52 6C 6E 70 {86: [03 08]} AC AE B6 C8 DF ]
	# mswhql: [1]
	# asset_eep: [40]
	# mccs_ver:[2.1]
	features = device.request_features()
	features = decode_features(features)
	print(features)

	vcp_list = #Extract vcp list

	if controls['brightness'] in vcp_list:
		get_value(slave, controls['brightness'])
		set_value(slave, controls['brightness'], brightness)

	if controls['contrast'] in vcp_list:
		get_value(slave, controls['contrast'])
		set_value(slave, controls['contrast'], contrast)


	get_value(slave, controls['color_preset'])
	set_value(slave, controls['color_preset'], color_presets['sRGB'])

	get_value(slave, controls['gain_red'])
	set_value(slave, controls['gain_green'], 50)
	get_value(slave, controls['gain_green'])
	set_value(slave, controls['gain_green'], 48)
	get_value(slave, controls['gain_blue'])
	set_value(slave, controls['gain_blue'], 49)

	get_value(slave, controls['offset_red'])
	get_value(slave, controls['offset_green'])
	get_value(slave, controls['offset_blue'])

if __name__=="__main__":
	main()