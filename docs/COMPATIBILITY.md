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

## Python

Requires Python 3.9 or newer. PySide6 is only needed for the GUI; the CLI and the protocol
library work without it.

## Firmware updates

A firmware update may change or extend the protocol. If something stops working after an
update, re-run `probe` and compare with the table above.