# Troubleshooting

## Always start here

```
python aero_cli.py doctor
```

This reports your OS, Python, pyserial, every COM port, and classifies Bluetooth SPP ports into
remote (your earbuds) versus local (incoming). It never opens the serial port, so it is always
safe to run - and safe to paste into an issue.

---

## "未找到可应答的耳机串口" / no device found

`doctor` will tell you which of these applies.

**No Bluetooth serial ports at all**

* Bluetooth is off, or
* the earbuds are not paired, or
* your adapter is running a third-party driver stack.

Fix the last one by switching the adapter to the Microsoft driver in Device Manager, then
re-pair the earbuds.

**Only a local incoming port**

Ports whose hardware ID contains `LOCALMFG` are this PC listening for incoming connections.
They are not your earbuds. If that is all you have, the earbuds are not paired - or that model
does not expose SPP at all.

**Port exists but never answers**

The port is probed with a real handshake, so a wrong device will not be selected. If nothing
answers, the model probably speaks a different protocol. See [COMPATIBILITY.md](COMPATIBILITY.md).

---

## "串口被占用" / port busy

Something else holds the COM port. Most often it is **a second copy of this app**.

* The app is single-instance guarded, but a *previously launched* copy may still be running.
* Look for the tray icon (bottom-right). Right-click it and choose Exit.
* Closed the window but nothing is in the tray? Check Task Manager for a leftover process.

Other programs that can hold the port: vendor utilities, some Bluetooth scanners,
and terminal software you opened earlier.

---

## Mode changes seem to be ignored

Two device-side behaviours look like failures but are not:

* **Mode changes are asynchronous.** The app polls until the value settles. Reading back
  immediately can legitimately return the old value.
* **Wind noise reduction only works while ANC is on.** Requesting it while ANC is off is
  accepted briefly and then reverted by the device. The app reports this honestly as
  "device did not adopt it" rather than pretending it succeeded.

---

## Switching to LDAC disconnects everything for ~10 seconds

Expected. Changing the link mode rebuilds the A2DP link, which tears down the SPP control
channel with it. A write timeout during this window is normal; the app reconnects automatically.

Also note that **Windows does not support LDAC**. Selecting it on a PC does not improve audio
quality there - the setting is stored on the earbuds and takes effect when they later connect to
an LDAC-capable source.

---

## Battery numbers look wrong or jump around

Every battery change is written to the log together with the **raw payload**, which makes this
diagnosable:

```
%LOCALAPPDATA%\AeroQ32\logs\aero_q32.log
```

Look for lines like:

```
电量变化 -> 左94 右81 盒95 | payload=[01 01 04 06 5e 01 04 06 51 5f] len=10
```

If the payload layout differs from the documented one, please open an issue with the line.
The charging-case byte in particular has not been verified across charging states.

---

## UI text renders as boxes

The UI loads fonts from a preference list (Chinese, then Japanese/Korean, then Western).
If none of those cover your script, install a font that does and open an issue.

Set `AEROQ32_DATA_DIR` to keep logs and settings somewhere writable if `%LOCALAPPDATA%`
is restricted.

---

## The app is not visible but still running

Closing the window minimises to the system tray. Right-click the tray icon and choose Exit
to actually quit.

If you have no system tray at all, the app detects this and quits directly instead of hiding.

---

## Firmware / OTA

**This project deliberately refuses to send firmware-update commands.** They are hard-blocked
even when the raw console is unlocked. Flashing firmware over a reverse-engineered protocol can
brick the device permanently. Use the vendor app for firmware updates.