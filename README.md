# esp32-usb-bridge

> Talk to an ESP32 over USB on Linux **even when the kernel has no serial
> driver for the board's bridge chip** — no kernel build, no modules, no
> `/dev/ttyUSB*` node required.

On many machines (locked-down kernels, containers, Android-based systems) the
board shows up on USB but nothing can open it. This project fixes that in
**userspace**: a small libusb driver speaks the Silicon Labs CP210x protocol
directly and plugs into stock `esptool`, so `esp chip_id`, flashing and a
serial monitor all just work.

---

## The problem it solves

Plug in an ESP32 DevKit and you get a USB device like this:

```
Bus 001 Device 002: ID 10c4:ea60 Silicon Labs CP2102 USB to UART Bridge Controller
```

But then:

```
$ ls /dev/ttyUSB*
ls: cannot access '/dev/ttyUSB*': No such file or directory
$ zcat /proc/config.gz | grep CP210X
# CONFIG_USB_SERIAL_CP210X is not set
$ ls /lib/modules | wc -l
0
```

The CP2102 enumerates, interface `1-1:1.0` has **no driver bound**, and there
is no module tree to load one from. `esptool`, `screen` and `minicom` have
nothing to open — and you cannot fix it by compiling a module.

So we skip the kernel entirely and drive the chip over `libusb`.

---

## Install

```bash
git clone https://github.com/Unnho/esp32-usb-bridge.git
cd esp32-usb-bridge
sudo ./install.sh
```

or straight from the repo without cloning:

```bash
curl -fsSL https://raw.githubusercontent.com/Unnho/esp32-usb-bridge/main/install.sh | sudo bash
```

The installer is **idempotent** — run it as often as you like. It will:

1. Install `python3`, `python3-serial` (pyserial), `python3-usb` (pyusb),
   `libusb-1.0-0`, `esptool` and `curl` via `apt`
2. Restore the ESP32 stub-loader JSON files that the Debian `esptool` package
   ships without (`stub_flasher_32.json` and friends) from upstream v4.7.0
3. Copy the driver and commands into place (`/opt/esp-bridge`, `/usr/local/bin`)
4. Add a udev rule so a non-root user can access the CP2102
5. Syntax-check everything and, if a board is plugged in, run a live self-test

Installer options:

```bash
sudo ./install.sh --dry-run      # show what it would do
sudo ./install.sh --no-verify    # skip the live hardware test
sudo ./install.sh --help
```

---

## Usage

Everything auto-detects the board — you never pass `--port`.

```bash
esp bridge-info     # identity of the CP2102 bridge itself
esp chip_id         # chip model, features, crystal, MAC
esp flash_id        # flash manufacturer, device id, size
esp read_mac        # MAC address
esp read_flash 0x1000 4096 dump.bin
esp --baud 460800 write_flash 0x1000 firmware.bin
esp erase_flash
esp image_info build/firmware.bin

esp-monitor         # live serial console @ 115200
esp-monitor --reset # reset first so you capture the boot log
esp-monitor -b 748800
```

`esp` accepts every stock esptool command — `chip_id`, `flash_id`, `read_mac`,
`read_flash`, `write_flash`, `verify_flash`, `erase_flash`, `erase_region`,
`merge_bin`, `image_info`, `dump_mem`, `read_mem`, `write_mem`,
`read_flash_status`, `write_flash_status`, `get_security_info`, `run`,
`make_image`, `version` — plus this project's own `esp bridge-info`.
**There is no `esp monitor`**: the console is the separate `esp-monitor`
command (esptool has no built-in monitor in v4.7).

`esptool.py` is aliased to the same wrapper, so muscle memory works too:

```bash
esptool.py flash_id      # identical to: esp flash_id
```

Plain stock `esptool` (i.e. `/usr/bin/esptool`) will **not** find the board,
because it has no `/dev/ttyUSB*` to open. Always go through `esp` /
`esptool.py` from `/usr/local/bin`.

Verify the installation:

```bash
esp version                 # prints esptool version
python3 /opt/esp-bridge/selftest.py   # full hardware self-test
```

Expected self-test ending:

```
*** ESP32 ROM replied to sync - driver works end to end ***
```

---

## How it works

```
   esp / esptool.py            (thin wrapper, stock esptool underneath)
        │
        ├─ patches serial.serial_for_url()  →  hands out our port object
        ├─ patches ResetStrategy._setDTRandRTS()  →  USB instead of ioctl(fd)
        └─ injects --port /dev/esp32
        │
   /opt/esp-bridge/cp210x_usb.py     (CP210xSerial: pyserial-shaped object)
        │  control transfers + bulk endpoints, background RX thread
   libusb  (/dev/bus/usb/001/002)
        │
   CP2102  (10c4:ea60)  ── UART ──  ESP32
```

`CP210xSerial` implements the subset of the pyserial API that esptool uses:
`read`, `write`, `inWaiting`, `baudrate`, `timeout`, `write_timeout`,
`reset_input_buffer`, `flushInput/flushOutput`, `setDTR`, `setRTS`,
`setDTRandRTS`, `dtr`, `rts`, `port`, `name`, `open`, `close`.

The request constants and semantics are copied from the Linux kernel's own
driver, `drivers/usb/serial/cp210x.c` — see [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

---

## Requirements

| | |
|---|---|
| OS | Debian/Ubuntu (installer uses `apt`; other distros: install the packages manually, see below) |
| Privileges | root (or a udev rule, which the installer adds) |
| Python | 3.9+ |
| Bridge chip | Silicon Labs CP210x — CP2102, CP2102N, CP2103/04/05/08 all use the same request set |
| Target | anything `esptool` supports: ESP32, ESP32-S2/S3/C3/C6/H2, ESP8266 |

Manual install (non-Debian): provide `python3`, pyserial, pyusb,
`libusb-1.0.0`, `esptool`, then copy `cp210x_usb.py` → `/opt/esp-bridge/`,
`esp` and `esp-monitor` → `/usr/local/bin/`, and make `esp` executable.

---

## Troubleshooting

**`Could not claim USB interface 0: [Errno 16] Resource busy`**
Another process has the CP2102 (usually a forgotten `esp-monitor`). Only one
process can own the interface — close it and retry. Non-root users can hit
`[Errno 13] Permission denied` instead; re-run the installer to refresh the
udev rule, then re-plug the board.

**`No CP2102 (VID:PID 10c4:ea60) found on USB`**
Check it is still enumerated: `lsusb | grep 10c4`. If another bridge chip is
on the board (CH340 = `1a86:7523`, FTDI = `0403:6001`), this project does not
apply — those have working kernel drivers, just install `linux-modules-extra`
or check `dmesg`.

**`FileNotFoundError: ... stub_flasher_32.json`**
The Debian package omits some stub files. The installer restores them; to do
it by hand:

```bash
curl -fsSL -o /usr/lib/python3/dist-packages/esptool/targets/stub_flasher/stub_flasher_32.json \
  https://raw.githubusercontent.com/espressif/esptool/v4.7.0/esptool/targets/stub_flasher/stub_flasher_32.json
```

**`Failed to connect ...`**
Make sure you are using `esp`, not `/usr/bin/esptool`. If it still fails, hold
the board's **BOOT** button while running `esp --before no_reset chip_id` to
confirm the data path, then investigate the reset wiring.

**`esptool: error: argument operation: invalid choice: 'chip-id'`**
Commands use underscores: `chip_id`, `flash_id`, `read_mac`, `read_flash`.

**Second run says `port is busy` after a crashed session**
Nothing is usually left over, but if it persists: `pkill -f esp-monitor`,
re-plug the board.

---

## Uninstall

```bash
sudo ./uninstall.sh                   # remove installed files
sudo ./uninstall.sh --purge-packages  # also apt-remove what it installed
```

---

## Repository layout

```
install.sh          idempotent installer (packages, stubs, files, udev, verify)
uninstall.sh        removes what install.sh put in place
esp                 esptool wrapper + the two pyserial/esptool patches
esp-monitor         serial console (screen/minicom replacement)
cp210x_usb.py       the userspace CP2102 driver (CP210xSerial)
selftest.py         live hardware self-test
AGENTS.md           briefing for AI assistants working on this repo
docs/PROTOCOL.md    CP210x USB request reference + ESP32 reset sequences
```

Read [`AGENTS.md`](AGENTS.md) before changing anything — it documents the
non-obvious constraints (kernel state, single-interface ownership, the
`sys.path` shadowing trap, `read()` semantics) that will bite you otherwise.

## License

No license file has been added yet — all rights reserved by default. Add one
(MIT/Apache-2.0 are typical for this kind of tool) before you expect others to
reuse it.
