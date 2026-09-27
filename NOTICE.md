# Notices

## Trademarks and affiliation

This is an **independent, unofficial, community project**.

* It is **not** affiliated with, authorized by, endorsed by, or supported by any earbud
  manufacturer or vendor.
* All product names, trademarks and registered trademarks are the property of their
  respective owners. They are used here only for **identification and interoperability**
  purposes (to state which hardware this software was tested against).
* No vendor logo or brand asset is included in this repository.

## Provenance of the protocol implementation

The protocol constants and packet framing documented in `docs/PROTOCOL.md` were obtained by
**observing the traffic between the earbuds and the host** and by testing commands against
hardware owned by the contributors. This is interoperability reverse engineering.

**This repository contains no third-party code, no decompiled code, and no vendor binaries.**
Specifically, it does **not** contain and must never contain:

* vendor application packages (`.apk` / `.aab` / `.exe`)
* decompiled or decompilation output of vendor software
* extracted vendor resources (layouts, string tables, images, `resources.arsc`)
* vendor firmware or OTA images

Any pull request containing such material will be closed.

## Safety-critical commands

Command identifiers associated with firmware flashing and irreversible deletion are listed in
`aero_q32.DENY_COMMANDS` and are **hard-blocked in code**, including when the raw-command
console is unlocked. Please keep it that way.

## Data collection

This software collects nothing and sends nothing anywhere. It has no network code.
It writes only to its own data directory:

* `settings.json` - your preferences
* `logs/aero_q32.log` - rotating diagnostic log

Both stay on your machine.