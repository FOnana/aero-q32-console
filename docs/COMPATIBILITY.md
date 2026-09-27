# Compatibility

## Verified

| Device | Chipset family | Status |
|--------|----------------|--------|
| 1MORE AERO Q32 | Jieli | Fully working: battery, all 8 ANC modes, link mode, sound preset |

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