# Compatibility

## Verified

| Device | Chipset family | Status |
|--------|----------------|--------|
| 1MORE AERO Q32 | Jieli | Fully working: battery, all 8 ANC modes, link mode, sound preset |
| 1MORE S20 Pro (open-ear clip) | Jieli | **Read-only**: battery works. Writes disabled - see below |

## The little-endian variant

The S20 Pro answers this protocol family, but wraps its frames differently. Compared with the
AERO Q32:

| | AERO Q32 | S20 Pro |
|---|---|---|
| 16-bit header fields | big-endian | **little-endian** |
| Trailer field | `00 01` | `01 00` |
| Byte 8 | `XOR(bytes[0:8])` | **not a checksum** |

The trailer doubles as the discriminator, so the parser tells the two apart unambiguously.

Byte 8 on the S20 Pro is still unexplained. All 256 CRC-8 polynomials were tried against
initial values 0x00 and 0xFF, with and without reflection, over nine different byte ranges -
plus plain sum, XOR and ones-complement. None matched. Two observations suggest it is not a
content check at all:

* the reply to `0x4E` carried `0x89` on every request,
* but an *unsolicited* frame of the same command carried `0x3f`.

Same command, same length, different byte - so it more likely tags the frame origin
(reply vs. notification) than its contents.

**Because it cannot be verified, the parser falls back to structural checks** for this
variant: a fixed 3-byte prefix, a valid trailer, and a sane length. Random noise is very
unlikely to satisfy all three, but the guarantee is weaker than a real checksum, which is
one more reason writes stay off.

### Why writes are disabled

The S20 Pro **replies correctly** to this project's request frames, so reads are safe and
useful. That does not mean the *request* framing is correct in the other direction - the
device may simply be ignoring fields it does not need.

Testing that guess means writing to a real pair of earbuds that cannot be replaced. The
asymmetry is not worth it, so `require_write()` refuses and the GUI disables the controls.

Reading works because the device answers; writing waits until someone can verify it on
hardware that is expendable.

### No ANC on the S20 Pro

The S20 Pro is an open-ear clip design; it has no noise cancelling hardware. `0x5F` (ANC)
is silent - that is the correct behaviour for this model, not a protocol failure.

## Expected to work

Other earbuds built on the same Jieli command protocol. The framing and command identifiers are
chipset-level, not model-level, but **not every model implements every command** - feature
availability is partly server-driven in the vendor ecosystem and varies by firmware.

Always check what your unit actually answers:

```
python aero_cli.py probe
```

On the reference device 7 of 20 probed commands responded. Music list, EQ parameter,
double/triple-click customisation and ear-detection queries were silent - i.e. genuinely
unimplemented on that model, not a bug here.

## Will not work

Models built on other chipset families speak entirely different protocols, usually over
BLE GATT rather than SPP, with different service UUIDs and framing:

* BES / Bestechnic
* Airoha
* Bluetrum
* Qualcomm GAIA (the classic CSR/Qualcomm command set)

If `python aero_cli.py doctor` reports no remote SPP port, this is the likely reason.

## Operating system

| | |
|---|---|
| Windows 11 | Tested, primary target |
| Windows 10 | Expected to work; SPP handling is the same |
| macOS / Linux | Not supported - the port discovery layer is Windows-specific |

### Bluetooth stack matters

The project relies on Windows creating a virtual COM port for the earbuds SPP service. This is
done by the **Microsoft Bluetooth stack**. Third-party stacks (older CSR or Broadcom drivers,
some vendor utilities) may not create SPP ports at all.

If `doctor` finds no Bluetooth serial ports, switching the adapter to the Microsoft driver in
Device Manager is usually the fix.

### Multiple adapters

Multiple Bluetooth adapters are fine. Candidates are ranked (explicit `--port`, then remembered
port, then MAC match) and each is verified with a real handshake before being trusted, so the
wrong adapter will not be silently selected.

### Multiple earbuds

Every remote SPP port is probed; the first one that answers the handshake wins. Use
`--port` or `--mac` to pin a specific device.


## Two things we learned the hard way

### 1. Opening the port does not mean the link is up

On Windows, `serial.Serial(port)` returns in a fraction of a second, but the underlying
RFCOMM link may still be connecting. Measured on real hardware: **the first byte from the
device arrived 6.2 seconds after the port opened**, and everything written before that was
lost.

This used to make compatible devices look unreachable, and because every retry repeated the
same too-short probe, the app looped forever on "searching". Discovery now retries the
handshake for up to 10 seconds per candidate port.

### 2. Custom SPP UUIDs are invisible to a COM-port based tool

Not every Jieli-based product exposes its control channel on the **standard** SPP UUID
(`00001101-0000-1000-8000-00805F9B34FB`). Some use a vendor-specific service instead:

```
FriendlyName : JL_SPP
InstanceId   : BTHENUM\{EDF00000-EDFE-DFED-FEDF-EDFEDFEDFEDF}_VID&..._<MAC>_...
```

Windows only creates a virtual COM port for the standard SPP profile. A custom-UUID service
has no COM port at all, so pyserial cannot reach it - it needs a raw RFCOMM socket
(WinRT `RfcommDeviceService`).

For such a device the standard-SPP port shows up in `doctor`, but nothing ever answers on it.
That is a transport mismatch, not a protocol bug. Verified on a 漫步者花再 Zero Buds: it is a
Jieli device (service named `JL_SPP`) but does not answer this command set.

## Python

Requires Python 3.9 or newer. PySide6 is only needed for the GUI; the CLI and the protocol
library work without it.

## Firmware updates

A firmware update may change or extend the protocol. If something stops working after an
update, re-run `probe` and compare with the table above.