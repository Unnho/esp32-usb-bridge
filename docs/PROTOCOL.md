# CP210x protocol reference

Everything `cp210x_usb.py` sends, and why. Transcribed from the Linux kernel's
own driver — **`drivers/usb/serial/cp210x.c`** (also `cp210x.h` sections inside
it) at v6.1. Treat that file as authoritative; re-check it if anything here
disagrees with observed behaviour.

Silicon Labs application notes **AN571** (CP210x standard requests) and
**AN205** (baud-rate generation) are the vendor-side equivalents.

---

## 1. The device

Our board enumerates as:

| Field | Value |
|---|---|
| `idVendor` / `idProduct` | `0x10C4` / `0xEA60` (Silicon Labs factory default) |
| `bcdDevice` | `0x0100` |
| Configuration | 1 configuration, 1 interface, bus powered, 100 mA |
| Interface 0 | class `0xFF` (vendor specific), subclass 0, protocol 0, 2 endpoints |
| Endpoint `0x81` | Bulk IN, wMaxPacketSize 64 |
| Endpoint `0x01` | Bulk OUT, wMaxPacketSize 64 |
| Speed | 12 Mbit/s (USB full speed) |

Other CP210x part numbers share this protocol (CP2102N, CP2103/04/05/08).

---

## 2. Setup packet fields

| Symbol | Value | Meaning |
|---|---|---|
| `REQTYPE_HOST_TO_DEVICE` | `0x40` | vendor, device recipient, OUT |
| `REQTYPE_DEVICE_TO_HOST` | `0xC0` | vendor, device recipient, IN |
| `REQTYPE_HOST_TO_INTERFACE` | `0x41` | vendor, interface recipient, OUT |
| `REQTYPE_INTERFACE_TO_HOST` | `0xC1` | vendor, interface recipient, IN |

`wIndex` is the interface number (`0` for us) unless noted.

---

## 3. bRequest codes

| Code | Name | Direction | Payload |
|---|---|---|---|
| `0x00` | `IFC_ENABLE` | OUT | `wValue` = `0x0001` enable / `0x0000` disable |
| `0x01` | `SET_BAUDDIV` | OUT | legacy divisor (unused) |
| `0x02` | `GET_BAUDDIV` | IN | legacy |
| `0x03` | `SET_LINE_CTL` | OUT | `wValue` = data/parity/stop bits |
| `0x04` | `GET_LINE_CTL` | IN | read back |
| `0x05` | `SET_BREAK` | OUT | `wValue` `0x0001` on / `0x0000` off |
| `0x06` | `IMM_CHAR` | OUT | send a char immediately |
| `0x07` | `SET_MHS` | OUT | `wValue` = modem handshake lines |
| `0x08` | `GET_MDMSTS` | IN | 1 byte modem status |
| `0x0B` | `SET_EVENTMASK` | OUT | `wValue` = event mask |
| `0x11` | `RESET` | OUT | software reset |
| `0x12` | `PURGE` | OUT | `wValue` `0x000F` = purge both queues |
| `0x13` | `SET_FLOW` | OUT | 16-byte flow-control block |
| `0x15` | `EMBED_EVENTS` | OUT | `wValue` = escape char (`0xEC`) to enable, `0` to disable |
| `0x1D` | `GET_BAUDRATE` | IN | 4-byte LE |
| `0x1E` | `SET_BAUDRATE` | OUT | 4-byte LE block, `wValue=0` |
| `0xFF` | `VENDOR_SPECIFIC` | IN/OUT | sub-request in `wValue` |

### Line control (`SET_LINE_CTL`) bit fields

| Field | Mask | Values |
|---|---|---|
| data bits | `0x0F00` | `0x0500`…`0x0900` (5–9); 8 = `0x0800` |
| parity | `0x00F0` | none `0x0000`, odd `0x0010`, even `0x0020`, mark `0x0030`, space `0x0040` |
| stop bits | `0x000F` | 1 = `0x0000`, 1.5 = `0x0001`, 2 = `0x0002` |

**8N1 = `0x0800`.**

### Modem handshake (`SET_MHS`) — this is DTR/RTS

| Bit | Value |
|---|---|
| `CONTROL_DTR` | `0x0001` |
| `CONTROL_RTS` | `0x0002` |
| `CONTROL_WRITE_DTR` | `0x0100` |
| `CONTROL_WRITE_RTS` | `0x0200` |
| `CONTROL_CTS` / `DSR` / `RING` / `DCD` (read-only, in `GET_MDMSTS`) | `0x0010` / `0x0020` / `0x0040` / `0x0080` |

A write only changes the lines whose `WRITE` bits are set. So:

* `setDTR(True)`  → `wValue = 0x0101`
* `setDTR(False)` → `wValue = 0x0100`
* `setRTS(True)`  → `wValue = 0x0202`
* `setRTS(False)` → `wValue = 0x0200`
* both at once, DTR=1 RTS=0 → `wValue = 0x0301`

### Vendor sub-requests (`bRequest = 0xFF`)

| `wValue` | Name | Size | Notes |
|---|---|---|---|
| `0x370B` | `GET_PARTNUM` | 1 | `0x01` CP2101 … `0x02` CP2102, `0x08` CP2108, `0x20/21/22` CP2102N, `0xFF` unknown |
| `0x000E` | `GET_FW_VER` | 3 | `major.minor.patch` (CP2105/CP2108) |
| `0x0010` | `GET_FW_VER_2N` | 3 | CP2102N variants |
| `0x00C2` | `READ_LATCH` | 1–2 | GPIO input state |
| `0x37E1` | `WRITE_LATCH` | 2 | GPIO output |
| `0x370C` | `GET_PORTCONFIG` | 13–73 | port/GPIO configuration |

Quirk: some (often counterfeit) CP2102s return **one** byte instead of two for
a two-byte `GET_PARTNUM` request; the kernel reads that as "does not support
event-insertion mode". Our driver always requests 1 byte, which sidesteps it.

---

## 4. Sequences the driver uses

### Open

```
EMBED_EVENTS   = 0            # keep event-insertion OFF (raw data)
IFC_ENABLE     = UART_ENABLE  # 0x0001
SET_LINE_CTL   = 0x0800       # 8N1
SET_BAUDRATE   = <u32 LE>     # e.g. 115200 = 00 C2 01 00
SET_MHS        = 0x0300       # write both lines, both deasserted
start RX thread
```

`EMBED_EVENTS` matters: when enabled, the chip injects `0xEC` escape
sequences carrying LSR/MSR events into the byte stream. The kernel only turns
it on if `INPCK` parity checking is enabled — we always turn it off.

### Set baud rate

```
bmRequestType 0x41, bRequest 0x1E, wValue 0, wIndex 0,
data = struct.pack("<I", baud)     # 4 bytes
```

The chip quantises rates per **AN205**. CP2102 exact rates include 300 …
921600 (the kernel's table maps e.g. 4800 → 4803, 115200 → 115200,
921600 → top of range). 460800 and 921600 both work with esptool.

### Close

```
PURGE       = 0x000F   # clear TX and RX queues
IFC_ENABLE  = UART_DISABLE
release interface, dispose resources
```

---

## 5. ESP32 auto-reset (EN / IO0)

esptool drives the CP2102's DTR and RTS pins through the DevKit's two-transistor
circuit. Effective mapping (as documented in esptool's own comments):

| Line asserted | Effect on the board |
|---|---|
| DTR = 1 | GPIO0 (IO0) pulled **LOW** |
| RTS = 1 | EN pulled **LOW** (chip held in reset) |
| both 0 | nothing (EN high, IO0 high) |
| both 1 | nothing useful — the cross-coupled circuit cancels |

Download mode requires **IO0 low at the moment EN is released**.

### Strategies esptool uses

`ClassicReset` (sequential):

```
DTR=0 RTS=1   # EN low, chip in reset
sleep 0.1
DTR=1 RTS=0   # IO0 low, EN high -> boot with IO0 low
sleep reset_delay (0.05)
DTR=0         # IO0 released
```

`UnixTightReset` (both lines in one shot — this is what our board needs, and
what `_setDTRandRTS` implements):

```
(0,0)  (1,1)  (0,1)   # IO0 high, EN low: chip in reset
sleep 0.1
(1,0)              # IO0 low,  EN high: releases into download mode
sleep reset_delay
(0,0)              # IO0 released
DTR=0
```

`HardReset` (`--after hard_reset`, esptool's default) just pulses RTS:
`RTS=1 → sleep 0.1 → RTS=0`, which reboots the chip into the flash app.

Successful download mode looks like this in the boot log:

```
rst:0x1 (POWERON_RESET),boot:0x3 (DOWNLOAD_BOOT(UART0/UART1/SDIO_REI_REO_V2))
waiting for download
```

while a normal boot shows `boot:0x13 (SPI_FAST_FLASH_BOOT)`.

---

## 6. ROM serial protocol (what we talk after reset)

Framing is **SLIP**: frames are delimited by `0xC0`; inside a frame `0xC0` is
escaped as `0xDB 0xDC` and `0xDB` as `0xDB 0xDD`.

Request header (little-endian), then payload:

```
struct {
    u8  header;   // 0x00 for a request, 0x01 in a response
    u8  op;       // command
    u16 length;   // payload length
    u32 checksum; // 0 for most commands
};  // 8 bytes
```

`ESP_SYNC` (`op = 0x08`) payload, exactly as esptool sends it:

```python
data  = b"\x07\x07\x12\x20" + 32 * b"\x55"
frame = struct.pack("<BBHI", 0x00, 0x08, len(data), 0) + data
wire  = b"\xc0" + frame + b"\xc0"
```

A reply starts `c0 01 08 …`. The ROM repeats the sync reply several times, so
seeing `c00108040007205555...` is a healthy sign.

Other useful `op` values: `0x00` status/heartbeat, `0x05` read register,
`0x06` write register, `0x07` read flash, `0x09` begin download,
`0x0A` flash data, `0x0B` end download. esptool wraps all of this; you rarely
need it unless writing your own client.

---

## 7. Quick debugging checklist

1. `lsusb | grep 10c4` — is the device there at all?
2. `cat /sys/bus/usb/devices/1-1/1-1:1.0/driver/uevent` — should be **absent**
   (no kernel driver claimed it; that is the whole premise).
3. `esp bridge-info` — exercises control transfers only; confirms the CP210x
   side works independently of the ESP32.
4. `python3 /opt/esp-bridge/selftest.py` — proves DTR/RTS, RX, and the ROM
   sync path end to end.
5. If step 4 shows `boot:0x13` instead of `DOWNLOAD_BOOT`, the reset did not
   land: re-run (esptool retries 7× with 4 strategies), or hold BOOT.
