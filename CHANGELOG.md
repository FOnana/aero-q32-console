# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.0] - unreleased

### Added

* Support for the **little-endian frame variant** used by the 1MORE S20 Pro. Its header
  integers are byte-swapped and its byte 8 is not a checksum, so the parser now reads the
  trailer first and picks the byte order from it. Battery reads work on this variant;
  writes are refused. See [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).
* `WriteBlocked` exception and `AeroQ32.require_write()`, plus a `variant` field on the CLI
  `json` output.

### Fixed

* **Write methods bypassed the safety gate.** `set_listen_mode()`, `set_preset_sound()` and
  `set_connect_option()` called `send()` directly, so the gate was only consulted on the raw
  console path. They now go through `require_write()` like everything else.
* **Blocked commands crashed instead of refusing on Chinese Windows.** The console there is
  cp936, which cannot encode the warning glyphs in the gate messages, so a refusal raised
  `UnicodeEncodeError` - exit code 1 (crash) instead of 3 (blocked). Callers could not tell a
  refusal from a failure. The CLI now relaxes stream error handling (it keeps the console
  encoding, so Chinese still renders).
* `SyntaxWarning` from an invalid escape sequence in the `_parse_hwid()` docstring.

### Changed

* The GUI disables write controls when it detects the little-endian variant, rather than
  letting a click do nothing.
* `extract()` now returns `(sof, cmd, payload, variant)` - the tuple gained a field.

## [1.0.0] - unreleased

First public release. Reconstructed from an internal working tool with the following changes:

### Added

* Environment diagnostics (`doctor` / `ports` CLI subcommands, `env_report()` API).
* Port discovery now tolerates more Windows driver layouts and classifies SPP ports into
  remote vs. local, instead of assuming a COM number.
* The last successfully used port and MAC are remembered and tried first (still verified by
  an actual handshake before being trusted).
* `DeviceNotFound` exception, distinct from `PortBusy`, with actionable messages.
* Portable mode via `AEROQ32_DATA_DIR`.
* Hardware-free test suite.
* Documentation: protocol reference, compatibility notes, troubleshooting guide, notices.

### Changed

* Font loading no longer hard-codes a single CJK font; it probes a preference list and falls
  back gracefully, so the UI renders on non-Chinese Windows.
* User-facing branding is vendor-neutral. Product names appear only for identification.
* Error messages now tell the user what to do next.

### Removed

* All machine-specific values (device MAC addresses, COM numbers, absolute paths).
* All reverse-engineering tooling and vendor artifacts from the repository.

### Interface stability

The `aero_q32` public API is unchanged from the internal version:

```
build, checksum, extract,
list_spp_ports, find_port, probe_port, discover,
guard_command, probe_capabilities,
setup_logging, log, load_settings, save_settings,
config_dir, config_path, log_path, env_report, format_report,
class AeroQ32
```

CLI subcommands are unchanged and additive (`doctor`, `ports`, `--port`, `--mac` are new).