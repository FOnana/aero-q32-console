# Contributing

Thanks for helping. A few ground rules keep this project publishable.

## Absolute rules

1. **Never commit vendor binaries or decompiled output.** No `.apk`, no `.dex`, no decompiled
   sources, no extracted resources, no firmware images. See [NOTICE.md](NOTICE.md).
2. **Never weaken the safety gate.** `DENY_COMMANDS` must stay hard-blocked, including when
   the raw console is unlocked.
3. **No network code.** The library and app must stay fully offline.

## Before opening a PR

```
pip install -r requirements.txt ruff pytest
ruff check --select F,B .
pytest -q
```

The test suite is designed to run **without any hardware** - it must never open a serial port.

## Device compatibility reports

These are extremely valuable. Please open an issue with:

* the exact model name
* output of `python aero_cli.py doctor`
* output of `python aero_cli.py probe`
* what works and what does not

The `probe` command only issues read-only queries, so it is safe to run and paste.

## Code style

* Keep the public API in `aero_q32.py` backward compatible; it is the documented interface.
* Prefer explicit failure over silent guessing. If a response cannot be parsed, say so.
* Comments explain *why*, not *what*. Note anything that looks wrong but is actually correct
  (for example device-side quirks) so nobody "fixes" it later.

## Building a standalone exe

```bat
pip install pyinstaller
python -m PyInstaller --onefile --windowed --icon icon.ico ^
  --add-data "icon.ico;." --name AeroQ32 app.py
```

### If the build dies with an OpenBLAS memory error

```
OpenBLAS error: Memory allocation still failed after 10 retries, giving up.
PyInstaller.isolated._parent.SubprocessDiedError
```

PyInstaller inspects installed packages while discovering hooks, which can end up importing
numpy/OpenBLAS. OpenBLAS sizes its buffers by CPU count, so on a machine with many cores and a
small page file it can run out of memory. Two fixes:

**1. Cap the thread count (quick)**

```bat
set OPENBLAS_NUM_THREADS=1
set OMP_NUM_THREADS=1
```

**2. Build in a clean virtualenv (recommended for releases)**

It also produces a smaller exe, since numpy/scipy are not pulled in:

```bat
python -m venv .buildenv
.buildenv\Scripts\pip install pyserial PySide6 pyinstaller
.buildenv\Scripts\python -m PyInstaller --onefile --windowed --icon icon.ico ^
  --add-data "icon.ico;." --name AeroQ32 app.py
```

## Architecture

```
aero_q32.py   protocol + transport + safety gate + settings  (no UI, no Qt)
app.py        PySide6 GUI                                     (imports aero_q32)
aero_cli.py   command line front end                          (imports aero_q32)
tests/        hardware-free tests
docs/         protocol and troubleshooting documentation
```

Keeping the protocol layer free of UI dependencies is deliberate: it makes the whole thing
testable without a display and without a device.