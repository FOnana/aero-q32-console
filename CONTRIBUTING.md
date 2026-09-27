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