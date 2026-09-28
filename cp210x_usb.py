"""Userspace driver for the Silicon Labs CP2102 USB<->UART bridge.

This environment's kernel was built with ``# CONFIG_USB_SERIAL_CP210X is not
set`` and ships an empty module tree, so the ESP32 dev board's CP2102 bridge
enumerates on USB but never gets a driver bound to it - no ``/dev/ttyUSB*``
node ever appears and nothing can open the port.

This module talks to the chip directly over libusb (pyusb) and exposes an
object with the pyserial ``Serial`` API, so esptool and other pyserial-based
code can use it unchanged.

Request constants and semantics mirror ``linux/drivers/usb/serial/cp210x.c``
exactly, so behaviour matches the real kernel driver:

 * 0x00 IFC_ENABLE / 0x03 SET_LINE_CTL / 0x07 SET_MHS / 0x12 PURGE
 * 0x15 EMBED_EVENTS (kept OFF so incoming data stays raw)
 * 0x1E SET_BAUDRATE (32-bit LE block)
 * 0xFF vendor requests for part number / firmware version
"""

from __future__ import annotations

import io
import struct
import threading
import time

import serial.serialutil
import usb.core
import usb.util

# --- USB identity -----------------------------------------------------------
DEFAULT_VID = 0x10C4
DEFAULT_PID = 0xEA60

# --- bmRequestType ----------------------------------------------------------
REQTYPE_HOST_TO_DEVICE = 0x40
REQTYPE_DEVICE_TO_HOST = 0xC0
REQTYPE_HOST_TO_INTERFACE = 0x41
REQTYPE_INTERFACE_TO_HOST = 0xC1

# --- bRequest codes (cp210x.h) ---------------------------------------------
CP210X_IFC_ENABLE = 0x00
CP210X_SET_BAUDDIV = 0x01
CP210X_GET_BAUDDIV = 0x02
CP210X_SET_LINE_CTL = 0x03
CP210X_SET_BREAK = 0x05
CP210X_SET_MHS = 0x07
CP210X_GET_MDMSTS = 0x08
CP210X_SET_EVENTMASK = 0x0B
CP210X_RESET = 0x11
CP210X_PURGE = 0x12
CP210X_EMBED_EVENTS = 0x15
CP210X_SET_BAUDRATE = 0x1E
CP210X_VENDOR_SPECIFIC = 0xFF

# --- IFC_ENABLE -------------------------------------------------------------
UART_ENABLE = 0x0001
UART_DISABLE = 0x0000

# --- (SET|GET)_LINE_CTL -----------------------------------------------------
BITS_DATA_8 = 0x0800
BITS_PARITY_NONE = 0x0000
BITS_STOP_1 = 0x0000
LINE_CTL_8N1 = BITS_DATA_8 | BITS_PARITY_NONE | BITS_STOP_1

# --- PURGE ------------------------------------------------------------------
PURGE_ALL = 0x000F

# --- (SET_MHS|GET_MDMSTS) ---------------------------------------------------
CONTROL_DTR = 0x0001
CONTROL_RTS = 0x0002
CONTROL_CTS = 0x0010
CONTROL_DSR = 0x0020
CONTROL_RING = 0x0040
CONTROL_DCD = 0x0080
CONTROL_WRITE_DTR = 0x0100
CONTROL_WRITE_RTS = 0x0200

# --- VENDOR_SPECIFIC --------------------------------------------------------
CP210X_GET_FW_VER = 0x000E
CP210X_GET_FW_VER_2N = 0x0010
CP210X_GET_PARTNUM = 0x370B

PARTNUM_NAMES = {
    0x01: "CP2101",
    0x02: "CP2102",
    0x03: "CP2103",
    0x04: "CP2104",
    0x05: "CP2105",
    0x08: "CP2108",
    0x20: "CP2102N (QFN28)",
    0x21: "CP2102N (QFN24)",
    0x22: "CP2102N (QFN20)",
    0xFF: "unknown",
}

# Sanity cap on the RX buffer so a runaway reader can't eat all memory.
MAX_RX_BUFFER = 4 * 1024 * 1024


class CP210xError(serial.serialutil.SerialException):
    """Raised when the CP2102 cannot be opened or a USB request fails."""


class CP210xSerial:
    """A pyserial-style serial port backed by a CP2102 over raw USB."""

    def __init__(
        self,
        port="/dev/esp32",
        baudrate=115200,
        timeout=1.0,
        write_timeout=10.0,
        vid=DEFAULT_VID,
        pid=DEFAULT_PID,
        serial_number=None,
        do_open=True,
        **_ignored,
    ):
        self.port = port          # esptool reads .port
        self.name = port          # esptool reads .name
        self._vid = vid
        self._pid = pid
        self._serial_number = serial_number
        self._baudrate = int(baudrate)
        self.timeout = timeout
        self.write_timeout = write_timeout

        self._dev = None
        self._intf = 0
        self._ep_in = None
        self._ep_out = None
        self._rx = bytearray()
        self._cv = threading.Condition()
        self._running = False
        self._reader = None
        self._is_open = False
        self._dtr = False
        self._rts = False
        self._usb_lock = threading.RLock()
        self._detached_kernel_driver = False

        if do_open:
            self.open()

    # ------------------------------------------------------------------ setup
    def open(self):
        if self._is_open:
            return

        kwargs = {"idVendor": self._vid, "idProduct": self._pid}
        if self._serial_number:
            kwargs["serial_number"] = self._serial_number
        dev = usb.core.find(**kwargs)
        if dev is None:
            raise CP210xError(
                "No CP2102 (VID:PID %04x:%04x) found on USB"
                % (self._vid, self._pid)
            )

        try:
            dev.set_configuration()
        except usb.core.USBError:
            # Already configured by the kernel during enumeration - fine.
            pass

        # On a kernel that *does* ship CONFIG_USB_SERIAL_CP210X the kernel
        # driver may own interface 0 already. A userspace claim requires it to
        # be released first; we re-attach on close() so /dev/ttyUSB0 returns.
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
                self._detached_kernel_driver = True
        except (NotImplementedError, usb.core.USBError, AttributeError):
            # Backend has no such concept (or it refused) - the claim below
            # will tell us if the interface is genuinely unavailable.
            pass

        try:
            usb.util.claim_interface(dev, 0)
        except usb.core.USBError as exc:
            if self._detached_kernel_driver:
                try:
                    dev.attach_kernel_driver(0)
                except Exception:
                    pass
                self._detached_kernel_driver = False
            raise CP210xError("Could not claim USB interface 0: %s" % exc)

        cfg = dev.get_active_configuration()
        intf = usb.util.find_descriptor(
            cfg, find_all=True, bInterfaceNumber=0
        )
        intf = list(intf)
        if not intf:
            raise CP210xError("Interface 0 not found on CP2102")
        intf = intf[0]

        ep_in = ep_out = None
        for ep in intf:
            if usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
                continue
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                ep_in = ep
            else:
                ep_out = ep
        if ep_in is None or ep_out is None:
            raise CP210xError("Could not locate bulk IN/OUT endpoints")

        self._dev = dev
        self._intf = intf.bInterfaceNumber
        self._ep_in = ep_in
        self._ep_out = ep_out
        self._is_open = True

        try:
            # Keep event-insertion mode OFF: the kernel only enables it when
            # input parity checking is on, and it would corrupt raw data.
            self._set_u16(CP210X_EMBED_EVENTS, 0)
            self._set_u16(CP210X_IFC_ENABLE, UART_ENABLE)
            self._set_u16(CP210X_SET_LINE_CTL, LINE_CTL_8N1)
            self._set_baudrate(self._baudrate)
            # Known state: both control lines deasserted (IO0 high, EN high).
            self._write_mhs(CONTROL_WRITE_DTR | CONTROL_WRITE_RTS)
            self._dtr = self._rts = False
        except CP210xError:
            self.close()
            raise

        self._running = True
        self._reader = threading.Thread(
            target=self._reader_loop, name="cp210x-rx", daemon=True
        )
        self._reader.start()

    def close(self):
        if not self._is_open and self._dev is None:
            return
        self._running = False
        self._is_open = False

        with self._cv:
            self._cv.notify_all()

        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None

        if self._dev is not None:
            with self._usb_lock:
                try:
                    self._set_u16(CP210X_PURGE, PURGE_ALL)
                    self._set_u16(CP210X_IFC_ENABLE, UART_DISABLE)
                except Exception:
                    pass
            try:
                usb.util.release_interface(self._dev, self._intf)
            except Exception:
                pass
            # Give the interface back to the kernel driver if we took it.
            if self._detached_kernel_driver:
                try:
                    self._dev.attach_kernel_driver(self._intf)
                except Exception:
                    pass
                self._detached_kernel_driver = False
            try:
                usb.util.dispose_resources(self._dev)
            except Exception:
                pass

        self._dev = None
        self._ep_in = None
        self._ep_out = None
        with self._cv:
            self._rx = bytearray()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @property
    def is_open(self):
        return self._is_open

    # ------------------------------------------------------- low level USB
    def _ctrl_out(self, request, wValue=0, wIndex=None, data=None, timeout=1000):
        if wIndex is None:
            wIndex = self._intf
        with self._usb_lock:
            try:
                return self._dev.ctrl_transfer(
                    REQTYPE_HOST_TO_INTERFACE, request, wValue, wIndex, data, timeout
                )
            except usb.core.USBError as exc:
                raise CP210xError(
                    "CP2102 control request 0x%02x failed: %s" % (request, exc)
                )

    def _ctrl_in(self, request, wValue=0, wIndex=None, length=1, timeout=1000,
                 reqtype=REQTYPE_DEVICE_TO_HOST):
        if wIndex is None:
            wIndex = self._intf
        with self._usb_lock:
            data = self._dev.ctrl_transfer(
                reqtype, request, wValue, wIndex, length, timeout
            )
        return bytes(bytearray(data))

    def _set_u16(self, request, value):
        self._ctrl_out(request, value, None, None)

    def _set_baudrate(self, baud):
        # 32-bit little-endian block; the chip quantises per Silicon Labs AN205.
        self._ctrl_out(CP210X_SET_BAUDRATE, 0, None, struct.pack("<I", int(baud)))

    def _write_mhs(self, control):
        self._set_u16(CP210X_SET_MHS, control)

    # ------------------------------------------------------------ RX thread
    def _reader_loop(self):
        size = self._ep_in.wMaxPacketSize or 64
        while self._running:
            try:
                data = self._ep_in.read(size, timeout=50)
            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError:
                if not self._running:
                    break
                time.sleep(0.005)
                continue
            if not data:
                continue
            with self._cv:
                self._rx += bytes(data)
                overflow = len(self._rx) - MAX_RX_BUFFER
                if overflow > 0:
                    del self._rx[:overflow]
                self._cv.notify_all()

    # ---------------------------------------------------------- stream API
    @property
    def baudrate(self):
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value):
        value = int(value)
        self._baudrate = value
        if self._is_open:
            self._set_baudrate(value)

    @property
    def in_waiting(self):
        with self._cv:
            return len(self._rx)

    def inWaiting(self):  # legacy pyserial spelling used by esptool
        return self.in_waiting

    @property
    def out_waiting(self):
        return 0

    def read(self, size=1):
        """Read exactly *size* bytes if they arrive before self.timeout.

        Matches pyserial: block until `size` bytes are collected or the
        timeout expires, then return what was collected (possibly b"").
        A timeout of None blocks forever, a timeout of 0 is non-blocking.
        """
        if size <= 0:
            return b""
        timeout = self.timeout
        if timeout is None:
            deadline = None
        else:
            deadline = time.monotonic() + max(0.0, float(timeout))

        out = bytearray()
        with self._cv:
            while True:
                if not self._is_open:
                    return bytes(out)
                if self._rx:
                    need = size - len(out)
                    take = min(need, len(self._rx))
                    out += self._rx[:take]
                    del self._rx[:take]
                    if len(out) == size:
                        return bytes(out)
                if deadline is None:
                    self._cv.wait(0.1)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return bytes(out)
                self._cv.wait(min(remaining, 0.1))

    def write(self, data):
        if not self._is_open:
            raise serial.serialutil.PortNotOpenError("Port is not open.")
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        else:
            data = bytes(data)
        timeout_ms = 0
        if self.write_timeout is not None:
            timeout_ms = int(float(self.write_timeout) * 1000)
        try:
            written = self._ep_out.write(data, timeout=timeout_ms)
        except usb.core.USBError as exc:
            # Right after an EN-toggle the board can brown out or re-enumerate
            # for a moment, which shows up as a one-off EIO on the bulk OUT.
            # One retry rides over it instead of failing the whole command.
            time.sleep(0.1)
            try:
                written = self._ep_out.write(data, timeout=timeout_ms)
            except usb.core.USBError as exc2:
                hint = ""
                if exc2.errno in (5, 19):  # EIO / ENODEV
                    hint = (" (USB link lost - the board may have re-enumerated"
                            "; unplug/replug, then re-run)")
                raise CP210xError("USB write failed: %s%s" % (exc2, hint))
        return written

    def flush(self):
        # Writes complete synchronously over bulk USB, so there is nothing to
        # drain here.  Give the last URB a beat to land on the wire.
        if self._is_open:
            time.sleep(0.005)

    def reset_input_buffer(self):
        with self._cv:
            self._rx = bytearray()

    def reset_output_buffer(self):
        pass

    flushInput = reset_input_buffer
    flushOutput = reset_output_buffer

    def fileno(self):
        raise io.UnsupportedOperation(
            "CP2102 is driven over USB and has no file descriptor"
        )

    def send_break(self, duration=0.25):
        self._set_u16(CP210X_SET_BREAK, 0x0001)  # BREAK_ON
        time.sleep(duration)
        self._set_u16(CP210X_SET_BREAK, 0x0000)  # BREAK_OFF

    # ------------------------------------------------------- control lines
    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, state):
        self.setDTR(state)

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, state):
        self.setRTS(state)

    def setDTR(self, state):
        state = bool(state)
        self._dtr = state
        control = CONTROL_WRITE_DTR | (CONTROL_DTR if state else 0)
        self._write_mhs(control)

    def setRTS(self, state):
        state = bool(state)
        self._rts = state
        control = CONTROL_WRITE_RTS | (CONTROL_RTS if state else 0)
        self._write_mhs(control)

    def setDTRandRTS(self, dtr=False, rts=False):
        """Set both lines with a single request (needed by UnixTightReset)."""
        dtr, rts = bool(dtr), bool(rts)
        self._dtr, self._rts = dtr, rts
        control = CONTROL_WRITE_DTR | CONTROL_WRITE_RTS
        if dtr:
            control |= CONTROL_DTR
        if rts:
            control |= CONTROL_RTS
        self._write_mhs(control)

    # --------------------------------------------------------- chip info
    @staticmethod
    def part_number_name(partnum):
        return PARTNUM_NAMES.get(partnum, "unknown (0x%02X)" % partnum)

    def get_part_number(self):
        """Read the CP210x part number register (0x370B, 1 byte)."""
        data = self._ctrl_in(CP210X_VENDOR_SPECIFIC, CP210X_GET_PARTNUM, 0, 1)
        if not data:
            raise CP210xError("Empty part-number response")
        return data[0]

    def get_firmware_version(self, request=CP210X_GET_FW_VER):
        """Read the 3-byte firmware version (major.minor.patch)."""
        data = self._ctrl_in(CP210X_VENDOR_SPECIFIC, request, 0, 3)
        if len(data) < 3:
            raise CP210xError("Firmware version request returned %d bytes" % len(data))
        return (data[0] << 16) | (data[1] << 8) | data[2]

    def get_modem_status(self):
        data = self._ctrl_in(CP210X_GET_MDMSTS, 0, self._intf, 1,
                             reqtype=REQTYPE_INTERFACE_TO_HOST)
        if not data:
            raise CP210xError("Empty modem-status response")
        return data[0]

    def describe(self):
        """Human-readable summary of the bridge (and its current setup)."""
        dev = usb.core.find(idVendor=self._vid, idProduct=self._pid)
        if dev is None:
            return "CP2102 not present on USB"

        try:
            partnum = self.get_part_number()
            part_str = self.part_number_name(partnum)
        except Exception as exc:
            part_str = "<error: %s>" % exc

        try:
            fw = self.get_firmware_version()
            fw_str = "%d.%d.%d" % (fw >> 16, (fw >> 8) & 0xFF, fw & 0xFF)
        except Exception:
            fw_str = "<unavailable>"

        return "\n".join(
            [
                "  USB device       : %s"
                % (usb.util.get_string(dev, dev.iProduct) or "?"),
                "  VID:PID          : %04x:%04x" % (dev.idVendor, dev.idProduct),
                "  Manufacturer     : %s"
                % (usb.util.get_string(dev, dev.iManufacturer) or "?"),
                "  USB serial       : %s"
                % (usb.util.get_string(dev, dev.iSerialNumber) or "?"),
                "  CP210x part no.  : %s" % part_str,
                "  CP210x firmware  : %s" % fw_str,
                "  UART config      : %d baud, 8N1" % self._baudrate,
                "  DTR / RTS        : %s / %s" % (self._dtr, self._rts),
            ]
        )
