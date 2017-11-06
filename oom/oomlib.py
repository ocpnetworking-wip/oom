# /////////////////////////////////////////////////////////////////////
#
#  oomlib.py : Implements OOM decoding, all the routines that are not
#  visible in the Northbound API, calls decode routines
#  to decode raw data from the Southbound API
#  Also hides the messy data structures and allocates the memory
#  for the messy data
#
#  Copyright 2015  Finisar Inc.
#
#  Author: Don Bollinger don@thebollingers.org
#
# ////////////////////////////////////////////////////////////////////

import os
import struct
from ctypes import *
import importlib
import glob
import sys
from oomtypes import c_port_t
from oomtypes import port_class_e
from decode import collapse_cfp
from decode import expand_cfp
import re


#
# Mapping of port_type numbers to user accessible names
# This is a copy of a matching table in decode.py
# Might be a problem keeping these in sync, but
# these mappings are based on the relevant standards,
# so they should be fairly stable
#
port_type_e = {
    'UNKNOWN': 0x00,
    'GBIC': 0x01,
    'SOLDERED': 0x02,
    'SFP': 0x03,
    'XBI': 0x04,
    'XENPAK': 0x05,
    'XFP': 0x06,
    'XFF': 0x07,
    'XFP_E': 0x08,
    'XPAK': 0x09,
    'X2': 0x0A,
    'DWDM_SFP': 0x0B,
    'QSFP': 0x0C,
    'QSFP_PLUS': 0x0D,
    'CXP': 0x0E,
    'SMM_HD_4X': 0x0F,
    'SMM_HD_8X': 0x10,
    'QSFP28': 0x11,
    'CXP2': 0x12,
    'CDFP': 0x13,
    'SMM_HD_4X_FANOUT': 0x14,
    'SMM_HD_8X_FANOUT': 0x15,
    'CDFP_STYLE_3': 0x16,
    'MICRO_QSFP': 0x17,

    #  next values are CFP types. Note that their spec
    #  (CFP MSA Management Interface Specification ver 2.4 r06b page 67)
    #  values overlap with the values for i2c type devices.  OOM has
    #  chosen to add 256 (0x100) to the values to make them unique

    'CFP': 0x10E,
    '168_PIN_5X7': 0x110,
    'CFP2': 0x111,
    'CFP4': 0x112,
    '168_PIN_4X5': 0x113,
    'CFP2_ACO': 0x114,

    #  special values to indicate that no module is in this port,
    #  as well as invalid type

    'INVALID': -1,
    'NOT_PRESENT': -2,
    }


#
# load a python module as the southbound shim
#
def setshim(newshim, parms):
    try:
        oomsth.shim = importlib.import_module(newshim)
    except:
        oomsth.shim = importlib.import_module('oom.' + newshim)
    oomsth.ispy = True
    if parms is not None:
        oomsth.shim.setparms(parms)


#
# link in the southbound shim
# note this means the southbound shim MUST be installed in
# this location (relative to this module, in lib, named oom_south.so)
# or, if there is no oom_south.so, then use oomsysfsshim.py
#
class oomsth_c:
    shim = ''
    ispy = False


oomsth = oomsth_c()
packagedir = os.path.normpath(os.path.dirname(os.path.realpath(__file__)))

try:
    oomsth.shim = cdll.LoadLibrary(
                    os.path.join(packagedir, 'lib', 'oom_south.so'))
except:
    setshim("oomsysfsshim", None)

# The simulator shim needs to know where the package is installed,
# to find the module data, the Aardvark shim needs to know where
# to find it's 'aardvark.dll' library.  This is not a documented/required
# part of the Southbound API, so we'll try it.  If it doesn't work,
# skip it, the shim must not need the info
try:
    oomsth.shim.setpackagepath(packagedir)
except:
    pass

# one time setup, get the names of the decoders in the decode library
# try for a local copy first, then one relative to the package

try:
    decodelib = importlib.import_module('decode')
except:
    decodelib = importlib.import_module('oom.decode')

# and find any addons that should be incorporated
addon_fns = []
addon_dir = os.path.join(packagedir, 'addons')

sys.path.insert(0, addon_dir)   # required, to get them imported

# build a list of modules in the addons directory
modulist = []
modnamelist = glob.glob(os.path.join(addon_dir, '*.py'))
for name in modnamelist:
    name = os.path.basename(name)
    name = name[0:len(name)-3]
    modulist.append(name)
modnamelist = glob.glob(addon_dir + '/*.pyc')  # obfuscated modules!
for name in modnamelist:
    name = os.path.basename(name)
    name = name[0:len(name)-4]
    modulist.append(name)
modulist = list(set(modulist))  # eliminate dups, eg: x.py and x.pyc

for module in modulist:
    try:
        addlib = importlib.import_module(module)
    except:
        # skip that one (it is not a module?)
        pass
    else:
        try:
            addon_fns.append(getattr(addlib, 'add_features'))
        except:
            # skip that one ('add_features' is not there?)
            pass
sys.path = sys.path[1:]  # put the search path back


# This class is the python port, which includes the C definition
# of a port, plus other useful things, including the port type,
# and the keymap for that port.
class Port:
    def __init__(self, cport):
        self.c_port = cport

        # create an empty page cache
        self.pages = {}
        self.readcount = 0

        # copy the C character array into a more manageable python string
        self.port_name = bytearray(cport.name).rstrip('\0')
        self.port_type = get_port_type(self)

        # initialize the key maps, potentially unique for each port
        typename = type_to_str(self.port_type).lower()
        if self.port_type > 0x100:  # all CFP* devices, same spec, same keys
            typename = 'cfp2'
        try:
            # here is where the type is tied to the keymap file
            # again, try for a local copy first, then relative to the package
            try:
                maps = importlib.import_module(typename, package=None)
            except:
                maps = importlib.import_module('oom.' + typename)
            self.mmap = maps.MM
            self.fmap = maps.FM
            self.wmap = maps.WMAP
        except ImportError:
            self.mmap = {}
            self.fmap = {}
            self.wmap = {}
        for func in addon_fns:  # call all the addons to extend this port
            func(self)
        return(None)

    def add_addr(self, address):
        self.pages.update({address: {}})

    def invalidate_page(self, address, pagekey):
        if address not in self.pages:
            self.add_addr(address)
        self.pages[address].pop(pagekey, 'already empty')


#
# oom_get_port(n): helper routine, provides a port without requiring the prior
# definition of the complicated port_t struct
# returns port 'n' of the list of ports returned by the shim
# note, sketchy way to define a port
#
def oom_get_port(n):
    portlist = oom_get_portlist()
    return(portlist[n])


#
# Sorts the portlist by port name
#
def sort_portlist( portlist ):
    convert = lambda text: int(text) if text.isdigit() else text
    alphanum_key = \
        lambda key: [convert(c) for c in re.split('([0-9]+)', key.port_name) ]
    return sorted(portlist, key = alphanum_key)


#
# similarly, provide the port list without requiring the definition
# of the port_t structure.  Allocate the memory here.
#
def oom_get_portlist():
    numports = oomsth.shim.oom_get_portlist(0, 0)
    if numports < 0:
        raise RuntimeError("oom_get_portlist error: %d" % numports)
    elif numports == 0:
        return list()

    cport_array = c_port_t * numports
    cport_list = cport_array()
    retval = oomsth.shim.oom_get_portlist(cport_list, numports)
    pl = [Port(cport) for cport in cport_list]
    portlist = sort_portlist(pl)
    return portlist


#
# figure out the type of a port
#
def get_port_type(port):
    if port.c_port.oom_class == port_class_e['SFF']:
        data = oom_get_cached_sff(port, 0xA0, 0, 0, 1)
        ptype = ord(data[0])
    elif port.c_port.oom_class == port_class_e['CFP']:
        data = oom_get_memory_cfp(port, 0x8000, 1)
        # CFP types overlap with i2c types, so OOM adds 0x100 to disambiguate
        ptype = ord(data[1]) + 0x100
    else:
        ptype = port_type_e['UNKNOWN']
    return (ptype)


#
# Manage the buffer cache in the port class, transparently fill the
# cache (for this i2c address, for this page) if it is empty,
# return the requested data
#
def oom_get_cached_sff(port, address, page, offset, length):
    if address not in port.pages:
        port.add_addr(address)

    if offset < 128:   # Special case low memory, not a real page
        pagekey = -1
        pageoffs = 0
    else:
        pagekey = page
        pageoffs = 128

    if pagekey not in port.pages[address]:
        buf = oom_get_memory_sff(port, address, page, pageoffs, 128)
        port.pages[address].update({pagekey: buf})

    # the data is now in the page cache, just fetch what is needed
    start = offset - pageoffs
    end = start + length
    if end < 129:
        data = port.pages[address][pagekey][start: end]
    else:   # allow reading low memory and page memory in one read
        data = port.pages[address][pagekey][start: 128]
        data += oom_get_cached_sff(port, address, page, 128,
                                   length - (128 - offset))
    return(data)


#
# Allocate the memory for raw reads, return the data cleanly
# Does not interact at all with port's page caches
#
def oom_get_memory_sff(port, address, page, offset, length):
    data = create_string_buffer(length)  # allocate space
    port.readcount = port.readcount + 1
    #
    # hack: if oomsouth is the python version, I can't figure out how to
    # deal with a byref() pointer, so I pass the c_port rather than the
    # pointer to it.  Fixing this hack would make me happy.
    # Fix it in oom_set_memory_sff too!
    #
    if oomsth.ispy:
        retlen = oomsth.shim.oom_get_memory_sff(port.c_port, address,
                                                page, offset, length, data)
    else:
        retlen = oomsth.shim.oom_get_memory_sff(byref(port.c_port), address,
                                                page, offset, length, data)
    return(data)


#
# Raw write
#
def oom_set_memory_sff(port, address, page, offset, length, data):
    pagekey = page
    if offset < 128:
        pagekey = -1
    port.invalidate_page(address, pagekey)  # force re-read after write
    if oomsth.ispy:
        retlen = oomsth.shim.oom_set_memory_sff(port.c_port, address,
                                                page, offset, length, data)
    else:
        retlen = oomsth.shim.oom_set_memory_sff(byref(port.c_port), address,
                                                page, offset, length, data)
    return retlen


#
# CFP version of get_cached...
# for now, don't cache, just fetch everything, everytime
# because, unlike i2c devices, there is no natural page boundary for
# cache lines
#
def oom_get_cached_cfp(port, offset, length):
    return oom_get_memory_cfp(port, offset, length)


#
# CFP version of get_memory_*
# REMEMBER!  address and length are in words, not bytes
# Allocate the memory for raw reads, return the data cleanly
#
def oom_get_memory_cfp(port, address, length):
    data = create_string_buffer(length*2)  # allocate space in bytes
    port.readcount = port.readcount + 1
    retlen = oomsth.shim.oom_get_memory_cfp(port.c_port, address, length, data)
    return(data)


#
# Raw write
#
def oom_set_memory_cfp(port, address, length, data):
    retlen = oomsth.shim.oom_set_memory_cfp(port.c_port, address, length, data)
    return retlen


# for given port, return the value of the given key
def oom_get_keyvalue(port, key):
    mm = port.mmap                            # get the keys and memory map
    if key not in mm:
        return ''
    decoder = getattr(decodelib, mm[key][1])      # get the decoder
    if port.c_port.oom_class == port_class_e['SFF']:
        par = (port,) + mm[key][2:6]              # get the location
        if mm[key][0] == 0:                       # static data, use cache
            raw_data = oom_get_cached_sff(*par)   # get the cached data
        else:
            raw_data = oom_get_memory_sff(*par)   # get the fresh data
        par = mm[key][6:]                         # extra decoder parms
    elif port.c_port.oom_class == port_class_e['CFP']:
        par = (port,) + mm[key][3:5]              # get the location
        if mm[key][0] == 0:                       # static data, use cache
            raw_data = oom_get_cached_cfp(*par)   # get the cached  data
        else:
            raw_data = oom_get_memory_cfp(*par)   # get the fresh data
        if mm[key][2] == 1:                       # collapse CFP zeros out
            raw_data = collapse_cfp(raw_data)
        par = mm[key][5:]                         # extra decoder parms
    temp = decoder(raw_data, *par)                # get the value
    return temp


# for given port, return the value of the given key
# this version always uses the cached value.  Used by oom_get_memory
# to scoop up all of the keys with one read of EEPROM
def oom_get_keyvalue_cached(port, key):
    mm = port.mmap
    if key not in mm:
        return ''
    decoder = getattr(decodelib, mm[key][1])      # get the decoder
    if port.c_port.oom_class == port_class_e['SFF']:
        par = (port,) + mm[key][2:6]              # get the location
        raw_data = oom_get_cached_sff(*par)       # get the cached data
        par = mm[key][6:]                         # extra decoder parms
    elif port.c_port.oom_class == port_class_e['CFP']:
        par = (port,) + mm[key][3:5]              # get the location
        raw_data = oom_get_cached_cfp(*par)       # get the fresh data
        if mm[key][2] == 1:                       # collapse CFP zeros out
            raw_data = collapse_cfp(raw_data)
        par = mm[key][5:]                         # extra decoder parms
    temp = decoder(raw_data, *par)                # get the value
    return temp


# set the chosen key to the specified value
def oom_set_keyvalue(port, key, value):
    mm = port.mmap
    wm = port.wmap
    if key not in mm:
        return -1
    if key not in wm:
        return -1
    encoder = getattr(decodelib, wm[key])  # find the encoder
    if port.c_port.oom_class == port_class_e['SFF']:
        par = (port,) + mm[key][2:6]           # get the read parameters
        raw_data = oom_get_memory_sff(*par)    # get the current data
        par = mm[key][6:]                      # extra decoder parms
        temp = encoder(raw_data, value, *par)  # stuff value into raw_data
        retval = oom_set_memory_sff(port, mm[key][2], mm[key][3], mm[key][4],
                                    mm[key][5], temp)
    elif port.c_port.oom_class == port_class_e['CFP']:
        par = (port,) + mm[key][3:5]           # get the location
        raw_data = oom_get_memory_cfp(*par)    # get the current data
        par = mm[key][5:]                      # extra decoder parms
        temp = encoder(raw_data, value, *par)  # stuff value into raw_data
        if mm[key][2] == 1:                    # expand with zeros for CFP
            temp = expand_cfp(temp)
        retval = oom_set_memory_cfp(port, mm[key][3], mm[key][4], temp)
    return retval


#
# given a 'function', return a dictionary with the values of all the
# keys in that function
#
def oom_get_memory(port, function):

    mm = port.mmap
    funcmap = port.fmap
    retval = {}

    if function not in funcmap:
        return None

    for key in funcmap[function]:
        if mm[key][0] == 1:         # Dynamic key, invalidate page cache
            pagekey = mm[key][3]    # page of interest
            if mm[key][4] < 128:    # unless it is the low memory page
                pagekey = -1        # whose pagekey is -1
            port.invalidate_page(mm[key][2], pagekey)

    for key in funcmap[function]:
        retval[key] = oom_get_keyvalue_cached(port, key)
    return retval


# debug helper function, print raw data, in hex
def print_block_hex(data, initial):
    dataptr = 0
    bytesleft = len(data)
    lines = (bytesleft + 15) / 16
    lineaddr = initial
    for i in range(lines):
        outstr = "%.4xx:  " % lineaddr
        blocks = (bytesleft + 3) / 4
        if blocks > 4:
            blocks = 4
        for j in range(blocks):
            nbytes = bytesleft
            if nbytes > 4:
                nbytes = 4
            for k in range(nbytes):
                temp = ord(data[dataptr])
                foo = hex(temp)
                if temp < 16:
                    outstr += '0'
                outstr += foo[2:4]
                dataptr += 1
                bytesleft -= 1
                lineaddr += 1
            outstr += ' '
        print outstr


# helper routine to return module type as string (reverse port_type_e)
def type_to_str(modtype):
    for tname, mtype in port_type_e.iteritems():
        if mtype == modtype:
            return tname
    return ''
