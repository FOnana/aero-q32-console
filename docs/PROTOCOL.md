# Protocol reference

This document describes the command protocol used by Jieli-based earbuds over the Bluetooth
Serial Port Profile. Everything here was determined by observing the link and testing against
hardware. See [../NOTICE.md](../NOTICE.md) for provenance.

## Transport

| | |
|---|---|
| Profile | Classic Bluetooth SPP (RFCOMM) |
| Service UUID | `00001101-0000-1000-8000-00805F9B34FB` |
| Host side | a virtual COM port created by Windows on pairing |
| Serial params | 9600 8N1 (the values are ignored by the SPP bridge) |

On Windows the port is named `Standard Serial over Bluetooth link (COMx)`, the *same* name as
local incoming ports. They can only be told apart by the device hardware ID:

```
remote device port: BTHENUM\{00001101-...}_VID&..._PID&...\..._<12 hex MAC>_...
local  incoming  : BTHENUM\{00001101-...}_LOCALMFG&....\...
```

Never hard-code a COM number.

## Frame format

On the reference device all multi-byte integers are **big-endian**. Not every model agrees -
see [Frame variants](#frame-variants) below.

```
offset  size  meaning
------  ----  --------------------------------------------------
  0      1    direction: 0x11 = request, 0x01 = response
  1      1    always 0x01
  2      1    always 0x00
  3      1    command id
  4      2    payload length
  6      1    always 0x00
  7      1    always 0x01
  8      1    checksum = XOR of bytes[0..7]
  9      n    payload
```

The checksum covers **only the 8 header bytes**, never the payload. This is easy to get wrong:
the payload is appended after the checksum byte.

Worked example - query the current listening mode (command `0x5F`, no payload):

```
header : 11 01 00 5F 00 00 00 01
checksum: 11^01^00^5F^00^00^00^01 = 0x4E
frame   : 11 01 00 5F 00 00 00 01 4E
response: 01 01 00 5F 00 01 00 01 5F 01
                                        ^^ payload: mode = 1 (strong ANC)
```

## Frame variants

The same protocol family is wrapped differently across 1MORE models. Two variants have been
observed on real hardware:

| | `VAR_Q32` | `VAR_LE` |
|---|---|---|
| Seen on | AERO Q32 | S20 Pro |
| Header integers | big-endian | little-endian |
| Trailer (bytes 6-7) | `00 01` | `01 00` |
| Byte 8 | `XOR(bytes[0:8])` | unknown, not a checksum |

The trailer is the discriminator, so the two never get confused - the parser reads bytes 6-7
first and picks the byte order accordingly.

### `VAR_LE` byte 8 is unsolved

Exhaustive search found no match: all 256 CRC-8 polynomials x initial values `0x00`/`0xFF` x
with/without reflection x nine byte ranges, plus plain sum, XOR and ones-complement.

Evidence that it is not a content check: the reply to `0x4E` always carried `0x89`, while an
**unsolicited** frame of the same command and same length carried `0x3f`. Same content,
different byte - so it likely encodes the frame origin (reply vs. notification).

Since it cannot be verified, `VAR_LE` frames are accepted on structure alone: fixed 3-byte
prefix, valid trailer, sane length. That is weaker than a checksum, so this variant is
**read-only** - see [COMPATIBILITY.md](COMPATIBILITY.md).

## Resynchronisation

A byte stream can lose or gain bytes. A parser must not assume frame alignment:

1. Look for a plausible header: byte0 in {0x01, 0x11}, byte1 = 0x01, byte2 = 0x00, and the
   trailer pair `(byte6, byte7)` equal to `(0x00, 0x01)` or `(0x01, 0x00)`.
2. Pick the byte order from the trailer, then reject implausible length fields (a sane cap is
   a few hundred bytes).
3. For `VAR_Q32`, verify the checksum and advance **one byte** on mismatch. For `VAR_LE` there
   is nothing to verify, so the structural checks in steps 1-2 carry the whole burden.

Advancing a whole frame on a bad checksum is wrong - a single corrupt byte would then lose
an entire frame. Advancing one byte recovers.

## Commands

### Safe / verified

| CMD | Direction | Meaning | Notes |
|-----|-----------|---------|-------|
| `0x4D` | write | handshake | send `01` as payload after connecting |
| `0x4E` | read | battery | see parsing below |
| `0x5F` | read | get listening mode | 1 byte |
| `0x5E` | write | set listening mode | 1 byte |
| `0x6C` | read | get link mode | 1 byte |
| `0x6B` | write | set link mode | 1 byte |
| `0x6A` | read | get sound preset | 1 byte |
| `0x69` | write | set sound preset | 1 byte |

### Never send

| CMD | Meaning | Why |
|-----|---------|-----|
| `0x71`,`0x72`,`0x73` | firmware update framing | can brick the device |
| `0x49` | delete music stored on device | irreversible |

These are blocked in code and must remain blocked.

## Value mappings

### Listening mode (`0x5E` / `0x5F`) - this is ANC

| Value | Meaning |
|-------|---------|
| 0 | off |
| 1 | strong noise cancelling |
| 2 | mild noise cancelling |
| 3 | transparency |
| 4 | wind noise reduction |
| 5 | voice pass-through |
| 6 | voice enhancement |
| 7 | adaptive |

### Link mode (`0x6B` / `0x6C`)

| Value | Meaning |
|-------|---------|
| 0 | standard |
| 1 | entertainment / low latency |
| 2 | LDAC |

Note: Windows does not support LDAC as a sink, so selecting it on a PC does not improve audio
quality there. The setting is stored on the earbuds and applies when they later connect to an
LDAC-capable source. Switching modes rebuilds the A2DP link, which **interrupts this control
channel for roughly 10 seconds** - a write timeout during that window is expected, not a fault.

### Sound preset (`0x69` / `0x6A`)

A single byte. Values 0-12 were accepted in testing. Only a few identifiers are known from the
protocol surface; everything else is reported by number rather than guessed.

## Battery parsing (`0x4E`)

Response payload, 10 bytes in the observed case:

```
index :  0   1  2  3   4    5  6  7   8    9
value : 00  01 04 06  62   01 04 06  5f   5f
                     ^left          ^right ^case
```

| Field | Index | Example |
|-------|-------|---------|
| left earbud % | 4 | 0x62 = 98 |
| right earbud % | 8 | 0x5F = 95 |
| charging case % | 9 | 0x5F = 95 |

**Caveat:** the exact semantics of index 9 have not been confirmed across charging states.
The observed right-earbud value has also been seen to oscillate between two values a few
percent apart; the raw payload is logged on every change so this can be investigated.

## Device-side quirks worth knowing

These look like bugs but are the device behaving as designed. Do not "fix" them:

* **Mode changes are asynchronous.** Reading back immediately can return the previous value.
  Poll until the value settles. This was observed roughly 4% of the time for mode 7.
* **Wind noise reduction is a sub-mode of ANC.** Requesting it while ANC is off is accepted
  momentarily and then reverts. It is not a communication failure.
* **Changing link mode rebuilds the A2DP link** and interrupts the control channel for ~10 s.

## Capability probing

Not every device implements every command. `probe_capabilities()` sends only read-only queries
and reports which ones answer. On the reference device, 7 of 20 probed commands responded.