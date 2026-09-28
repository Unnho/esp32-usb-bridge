# AGENTS.md — briefing for AI assistants

This file is written for an AI agent (or a human who has not read the code)
that needs to work on, debug, or use this repository. Read it before touching
anything; the constraints below are not obvious from the code alone.

---

## 1. What this repository is

`esp32-usb-bridge` lets you flash, read and monitor an ESP32 on a Linux host
**where the kernel provides no serial driver for the board's USB-UART bridge**.

The board is an ESP32 DevKit whose bridge is a **Silicon Labs CP2102**
(`VID:PID 10c4:ea60`). On the target host:

```
# CONFIG_USB_SERIAL_CP210X is not set      <- kernel compiled WITHOUT the driver
/lib/modules/<uname -r>/                   <- empty; zero .ko files exist
/dev/ttyUSB*                               <- never created
```

So the device enumerates on USB but interface `1-1:1.0` has no driver bound,
and there is **no way to bind one** (no module tree, no headers, out-of-tree
modules are not an option). The fix is entirely in userspace: speak the CP210x
vendor-request protocol over `libusb` and hand esptool a pyserial-shaped
object.

---

## 2. Hard constraints (do not argue with these)

1. **Never propose a kernel build, `modprobe`, or an out-of-tree module.**
   `/lib/modules` is empty and the running kernel config lacks CP210X
   (`zcat /proc/config.gz | grep CP210X`). Any solution must stay in userspace.
2. **One process owns the CP2102 at a time.** `usb.util.claim_interface(dev, 0)`
   is exclusive. Two concurrent tools produce
   `[Errno 16] Resource busy`. Never run `esp-monitor` and `esp ...` together
   in a test harness — serialize them.
3. **Do not run destructive esptool commands without explicit user consent.**
   Destructive: `erase_flash`, `erase_region`, `write_flash`, `write_mem`,
   `write_flash_status`, `run` with arbitrary addresses. Safe/read-only:
   `chip_id`, `flash_id`, `read_mac`, `read_flash`, `read_flash_status`,
   `image_info`, `version`, `get_security_info`.
4. **Do not publish device identifiers.** The board's MAC address, USB serial
   (`0001`) and flash serial are device-specific. Keep them out of committed
   files, docs, issues and commit messages; use placeholders in examples.
5. **`/dev/esp32` is a virtual name.** No such file exists or should be
   created. It is passed to esptool purely so our patched
   `serial.serial_for_url()` can recognise it. It must keep the `/dev/`
   prefix because esptool special-cases port names that start with `com` or
   `/dev/` (`loader.py::_get_pid`).
6. **Keep the installed copies and the repo copies identical** by running
   `sudo ./install.sh` after editing files in the repo — the installer is the
   deployment mechanism, and the repo is the source of truth.

---

## 3. Repository map

| File | Role | Edit when… |
|---|---|---|
| `install.sh` | Idempotent installer: apt packages, restore missing esptool stubs, deploy files, udev rule, verification | adding dependencies or supporting another distro |
| `uninstall.sh` | Removes what the installer put in place | installer changes |
| `esp` | Wrapper: injects `--port`, patches pyserial + esptool reset logic, then runs stock `esptool._main()` | esptool CLI behaviour changes |
| `esp-monitor` | Serial console (replacement for `screen`/`minicom`, which need a tty) | console UX / reset behaviour |
| `cp210x_usb.py` | The driver: `CP210xSerial`, a pyserial-shaped object over libusb | protocol bugs, new features (break, GPIO, flow control) |
| `selftest.py` | Live hardware test: identity → line control → reset into download mode → ROM sync | verifying a change end-to-end |
| `README.md` | Human-facing docs | user-visible behaviour changes |
| `AGENTS.md` | This file | anything an agent should know |
| `LICENSE` | MIT license (copyright `Unnho`) | relicensing decisions |
| `docs/PROTOCOL.md` | CP210x USB request reference + ESP32 reset sequences | protocol questions |

Runtime layout after install:

```
/opt/esp-bridge/cp210x_usb.py     driver (imported by the wrappers)
/opt/esp-bridge/selftest.py
/opt/esp-bridge/README.md, AGENTS.md, LICENSE, docs/
/usr/local/bin/esp                the command
/usr/local/bin/esptool.py -> esp  conventional spelling
/usr/local/bin/esp-monitor
/etc/udev/rules.d/99-cp2102-esp32-bridge.rules
```

---

## 4. Architecture — the three moving parts

```
esp (wrapper)
 ├─ A. sys.path guard          removes its own dir from sys.path
 ├─ B. serial.serial_for_url   patched → returns CP210xSerial for /dev/esp32
 ├─ C. ResetStrategy._setDTRandRTS patched → port.setDTRandRTS()
 └─ D. ESPLoader._get_pid      patched → returns None (quietly)
      └── esptool._main()      stock, unmodified
             └── CP210xSerial (cp210x_usb.py)
                   ├─ control transfers (setup packets)
                   ├─ bulk EP 0x81 IN  → background thread → RX buffer
                   └─ bulk EP 0x01 OUT → write()
```

### A. The `sys.path` guard — why it exists

`esp` lives in `/usr/local/bin`, and we also install an `esptool.py` symlink
there. When Python runs a script, `sys.path[0]` is the script's directory, so
`import esptool` would resolve to **`/usr/local/bin/esptool.py` (our own
wrapper) instead of the installed esptool package** — a classic shadowing bug
that produces `AttributeError`/`ImportError` deep inside esptool. The guard
filters that directory out at the top of `esp`. If you rename or relocate the
wrapper, keep this behaviour.

### B. Why patch `serial.serial_for_url`

esptool builds its port in `loader.py`:

```python
if isinstance(port, str):
    self._port = serial.serial_for_url(port)
```

Patching that one function means **esptool itself needs no modification** —
every other line of esptool runs as shipped by Debian. Our replacement
recognises `PORT_NAME` (`/dev/esp32`) and the URL forms `cp2102://`,
`cp2102://auto`, and returns a `CP210xSerial` instance (already open, like
pyserial does).

### C. Why patch `_setDTRandRTS`

esptool's `UnixTightReset` (chosen first on Linux) drives both control lines
simultaneously using:

```python
fcntl.ioctl(self.port.fileno(), TIOCMSET, struct.pack("I", status))
```

We have no file descriptor — `CP210xSerial.fileno()` deliberately raises
`io.UnsupportedOperation`. The patch routes the call to
`port.setDTRandRTS(dtr, rts)`, which issues **one** CP210x `SET_MHS` request
with both write-enable bits set. That is exactly what the kernel driver does,
so timing semantics are preserved. The original fd-based code is kept as a
fallback for real ttys.

### D. Why patch `_get_pid`

esptool's `_get_pid()` looks the port up in pyserial's `list_ports` to find a
USB PID for USB-JTAG-Serial detection. Our virtual port is not in that list,
so every run printed a confusing
`Failed to get PID of a device on /dev/esp32, using standard reset sequence.`
Returning `None` means "unknown → use the standard reset sequence", which is
the correct answer for a CP2102 anyway.

---

## 5. Driver internals (`cp210x_usb.py`)

* **Open sequence** mirrors `cp210x_open()` in the kernel: `EMBED_EVENTS=0`
  (event-insertion OFF — otherwise `0xEC` escape sequences get interleaved into
  raw data), `IFC_ENABLE=UART_ENABLE`, `SET_LINE_CTL=0x0800` (8N1),
  `SET_BAUDRATE` (4-byte LE), `SET_MHS` with both write bits (known line state).
* **RX**: a daemon thread does `ep_in.read(wMaxPacketSize, timeout=50)` in a
  loop and appends to a `bytearray` guarded by a `threading.Condition`.
  Without this, data would pile up in USB buffers during boot logs and be lost
  on the next read. Cap is `MAX_RX_BUFFER` (4 MiB).
* **`read(size)` semantics are pyserial's**: block until `size` bytes are
  collected or `timeout` expires, then return whatever was collected
  (`b""` only if nothing arrived). Do **not** "optimise" this to return as
  soon as any byte is available — that truncates replies (this bug shipped
  once: the ROM sync reply came back as a single `c0` byte because the first
  byte satisfied the read). `timeout=0` is non-blocking, `None` blocks forever.
* **Control lines**: `setDTR(s)`/`setRTS(s)` send only the write-enable bit for
  the line being changed (kernel behaviour); `setDTRandRTS` sends both.
  `.dtr`/`.rts` are plain properties — esptool reads `.dtr` back inside
  `_setRTS` as a Windows usbser.sys workaround.
* **`close()`** purges and disables the UART, releases the interface and
  disposes USB resources. esptool calls it in its `finally` paths.

### `CP210xSerial` API surface (what esptool actually touches)

```
attributes : port, name, baudrate (get/set), timeout, write_timeout,
             is_open, dtr, rts, in_waiting
methods    : open, close, read(n), write(bytes), flush, inWaiting,
             reset_input_buffer, reset_output_buffer, flushInput, flushOutput,
             setDTR, setRTS, setDTRandRTS, fileno (raises)
extras     : describe(), get_part_number(), get_firmware_version(),
             get_modem_status(), send_break()
```

If you upgrade esptool, diff `loader.py` and `reset.py` for any newly used
port attribute/method and add it here.

---

## 6. Install & verify

```bash
sudo ./install.sh            # idempotent; safe to re-run
sudo ./install.sh --dry-run  # preview
```

Verification (run in this order, they are not concurrent-safe):

```bash
esp version                       # → esptool.py v4.7.0
python3 /opt/esp-bridge/selftest.py   # → exit 0
esp chip_id                       # → chip model + MAC
esp flash_id                      # → 4MB
esp read_flash 0x1000 64 /tmp/bl.bin && od -A d -t x1z /tmp/bl.bin | head -1
                                  # → must start with e9 (ESP image magic)
timeout 5 esp-monitor --reset </dev/null | head -5
                                  # → "ets Jun  8 2016 ..." boot banner
```

A healthy `selftest.py` prints, in order: bridge identity table →
`modem status` lines reflecting DTR/RTS → a boot log containing
`boot:0x3 (DOWNLOAD_BOOT(...))` and `waiting for download` →
`*** ESP32 ROM replied to sync - driver works end to end ***`.

Note `selftest.py` **resets the board**; `esp-monitor --reset` does too.

---

## 7. Known gotchas (all hit at least once in real life)

| Symptom | Cause | Fix |
|---|---|---|
| `AttributeError` / `ImportError` inside esptool | `/usr/local/bin/esptool.py` shadowing the package via `sys.path[0]` | keep the `sys.path` guard in `esp` |
| `invalid choice: 'chip-id'` | esptool uses underscores | `chip_id`, `flash_id`, … |
| `FileNotFoundError: stub_flasher_32.json` | Debian's `esptool` package omits classic-ESP32/ESP32-S3 stubs | installer restores them from upstream v4.7.0 |
| `Could not claim USB interface 0: Resource busy` | second process (usually `esp-monitor`) holds the interface | serialize processes |
| Sync reply is a single `c0` byte | `read()` returned on first byte | see §5 read semantics |
| Boot log shows `boot:0x13 (SPI_FAST_FLASH_BOOT)` | chip booted the app, not download mode | reset sequence didn't land — retry, or hold BOOT |
| `Failed to get PID of a device on /dev/esp32` | `_get_pid` cannot see a virtual port | patched to return `None` |
| `esptool` (plain) can't connect | it looks for a real tty | always use `esp` / `esptool.py` |
| `Permission denied` on `/dev/bus/usb/...` | usbfs node is root-only | installer's udev rule, or run as root |
| `install.sh` exits **1** with `tmp: unbound variable` right after a successful install | an `EXIT` trap captured a `local` variable, which is out of scope by the time the trap runs (`set -u`) | keep trap-captured variables global (`FETCH_TMP`) |
| a `set -e` script aborts on a line that is *only* `[[ cond ]] && cmd` **as that function's last statement** | the failed `&&` list becomes the function's return status | mid-function `[[ ]] && cmd` / `(( )) && return` are safe (verified), only *tails* are dangerous — write tails as `if` |
| board loops `rst:0xc (SW_CPU_RESET)` + `Brownout detector was triggered` while `esp …` all succeed | hardware: 3.3 V rail sagging (Wi-Fi TX current), and the same spike makes the host drop the whole USB bus (`usb usb1: USB disconnect`) | not a software bug — better cable / powered hub / external 5V. See README → Troubleshooting |
| `esp-monitor --reset` put the chip in download mode instead of booting the app | it used esptool's `ClassicReset` (the *download* sequence) while documenting a "hard reset" | `hard_reset()` pulses EN only, IO0 kept high |

---

## 8. Testing policy

* Read-only commands are fine to run freely against a connected board.
* Anything that writes or erases flash/RAM: **ask the user first**.
* **The flash-write path has been verified** (do not redo it blindly): a 4 KB
  test image (ASCII text + `0x00`-`0xFF` pattern + `0xFF`/`0x00` runs + footer
  byte) was written to scratch `0x000000` — a fully erased region that belongs
  to no partition — and confirmed four ways: esptool's post-write MD5, a
  separate `verify_flash`, a `read_flash` whose SHA-256 matched the source
  byte-for-byte, and a final `erase_region` restore that read back 100% `0xFF`,
  identical to the pre-test copy. The bootloader at `0x1000` was re-validated
  afterwards with `image_info` (`Checksum … (valid)`, `Validation Hash …
  (valid)`).
* **Write targets:** never write to `nvs`, `otadata`, `app0`, `spiffs`,
  `coredump`, or the partition table at `0x8000` (it is *not* in the "free
  gaps" list, because the table does not contain an entry for itself).
  `app1` currently holds an image — it is not free either. `0x000000..0x1000`
  is the only verified-safe scratch area on this board.
* **Quirk:** `write_flash --no-compress` printed `Wrote 16384 bytes at
  0x00000000` for a 4096-byte file (16 KB write-block accounting). Verified
  afterwards that only `0x000000..0x0fff` was actually erased/programmed —
  the bootloader's checksum and SHA-256 stayed valid. Do not treat that line
  as evidence of a 16 KB write.
* `selftest.py` and `esp-monitor --reset` reset the chip (it reboots into the
  app afterwards via esptool's `--after hard_reset`) — acceptable, but say so.
* There is no hardware-in-the-loop CI. Treat `selftest.py` as the test suite
  and report its exit code.
* When the board is unplugged, `esp` should fail with a clean
  `No CP2102 (VID:PID 10c4:ea60) found on USB` — that is expected, not a bug.

---

## 9. Extension ideas

* **PTY bridge** (`screen`/`minicom` support): expose a `/dev/pts/N` fed by the
  same reader thread. Caveat: `ioctl(TIOCMSET)` on a pty cannot be forwarded
  to the device, so DTR/RTS-based auto-reset would not work — do the reset
  when the slave side is opened instead.
* **Other bridge chips**: CH340 (`1a86:7523`) and FTDI (`0403:6001`) have
  working kernel drivers — those boards need `dmesg`/module fixes, not this
  project. Only CP210x needs a userspace driver here.
* **ESP-IDF integration**: `idf.py` shells out to `esptool.py`; because
  `/usr/local/bin` precedes `/usr/bin` in `PATH` and we install an
  `esptool.py` shim, IDF flashing may already route through our wrapper.
  Verify before claiming it works.
* **Extra diagnostics**: `GET_PORTCONFIG`/`GET_MDMSTS` registers are already
  wrapped (`describe()`), so GPIO/line-state reporting is easy to add.

---

## 10. Protocol reference

See [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the CP210x request table, the
open/set-baud/set-MHS sequences, the ESP32 EN/IO0 reset truth table, and the
SLIP framing used by the ROM sync. The authoritative source is the Linux
kernel's `drivers/usb/serial/cp210x.c`; constants here were transcribed from
it, so re-check that file if something behaves differently than documented.
