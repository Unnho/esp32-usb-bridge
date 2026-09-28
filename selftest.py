#!/usr/bin/env python3
"""Smoke test for the userspace CP2102 driver.

Opens the bridge, prints its identity, runs an esptool-style classic reset
sequence and reports whether the ESP32 answers the ROM sync ping.
"""
import struct
import sys
import time

sys.path.insert(0, "/opt/esp-bridge")
from cp210x_usb import CP210xSerial  # noqa: E402


def main():
    print("== opening CP2102 over raw USB ==")
    port = CP210xSerial(baudrate=115200, timeout=1.0)
    try:
        print(port.describe())

        print("\n== line control via SET_MHS ==")
        for dtr, rts in [(False, False), (True, False), (False, True), (False, False)]:
            port.setDTRandRTS(dtr, rts)
            print("  DTR=%-5s RTS=%-5s -> modem status 0x%02x"
                  % (dtr, rts, port.get_modem_status()))

        print("\n== ESP32 auto-reset into download mode ==")
        # Same sequence esptool's UnixTightReset uses; it needs both lines
        # driven together, which the kernel driver would do with a single
        # SET_MHS request (our setDTRandRTS does exactly that).
        port.reset_input_buffer()
        port.setDTRandRTS(False, False)
        port.setDTRandRTS(True, True)
        port.setDTRandRTS(False, True)  # IO0 high, EN low: chip in reset
        time.sleep(0.1)
        port.setDTRandRTS(True, False)  # IO0 low, EN high: release -> download
        time.sleep(0.05)
        port.setDTRandRTS(False, False)
        port.setDTR(False)
        time.sleep(0.3)

        # Show whatever the ROM printed while booting
        drained = port.read(port.inWaiting()) if port.inWaiting() else b""
        print("  boot output: %d bytes" % len(drained))
        if drained:
            print("  ---")
            for line in drained.decode("utf-8", "replace").splitlines():
                print("    %s" % line)
            print("  ---")

        print("\n== ROM sync ping (up to 5 tries, like esptool) ==")
        # Exact frame esptool sends: SLIP( 00 | op=0x08 | len | chk=0 | data )
        data = b"\x07\x07\x12\x20" + 32 * b"\x55"
        frame = struct.pack("<BBHI", 0x00, 0x08, len(data), 0) + data
        packet = b"\xc0" + frame + b"\xc0"

        for attempt in range(1, 6):
            port.reset_input_buffer()
            port.write(packet)
            port.timeout = 0.5
            resp = port.read(64)
            print("  try %d: %s" % (attempt, resp.hex() or "(no reply)"))
            if len(resp) > 2 and resp[0] == 0xC0 and resp[1] == 0x01:
                print("\n*** ESP32 ROM replied to sync - driver works end to end ***")
                return 0
            time.sleep(0.05)

        print("\n!! no sync reply (chip not in download mode)")
        return 2
    finally:
        port.close()


if __name__ == "__main__":
    sys.exit(main())
