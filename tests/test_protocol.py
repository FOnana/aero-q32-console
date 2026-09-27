"""Frame encoding/decoding tests.

These are the tests that matter most: the parser must never stall on a byte stream that has
lost or gained bytes, because that failure is silent - the UI keeps rendering while nothing
updates. Every case below previously caused a permanent stall in an earlier implementation.
"""
import aero_q32 as A


GOOD = bytes.fromhex("0101004e000a0001450001040664010406645f")
GOOD2 = bytes.fromhex("0101005f000100015f01")


def feed(chunks):
    """Mimic the reader loop: feed chunks, collect frames."""
    buf = bytearray()
    out = []
    for ch in chunks:
        buf += ch
        out += A.extract(buf)
    return out, buf


class TestChecksum:
    def test_checksum_covers_header_only(self):
        # The payload is appended AFTER the checksum byte - easy to get wrong.
        f = A.build(0x5F)
        assert len(f) == 9
        assert A.checksum(f[0:8]) == f[8]

    def test_known_frame(self):
        assert A.build(0x5F).hex() == "1101005f000000014e"

    def test_payload_does_not_affect_checksum(self):
        a = A.build(0x5E, bytes([0]))
        b = A.build(0x5E, bytes([1]))
        assert a[8] == b[8]
        assert a[9] != b[9]


class TestFrameSync:
    def test_single_frame(self):
        frames, leftover = feed([GOOD])
        assert len(frames) == 1
        assert leftover == b""

    def test_back_to_back_frames(self):
        frames, _ = feed([GOOD, GOOD2])
        assert len(frames) == 2

    def test_frame_split_across_reads(self):
        frames, _ = feed([GOOD[:5], GOOD[5:]])
        assert len(frames) == 1

    def test_leading_garbage_byte(self):
        frames, _ = feed([b"\xff" + GOOD])
        assert len(frames) == 1

    def test_leading_garbage_with_huge_length_field(self):
        # This used to stall the parser forever.
        frames, _ = feed([b"\x00\x00\x00\x00\xff\xff" + GOOD])
        assert len(frames) == 1

    def test_trailing_garbage(self):
        frames, _ = feed([GOOD + b"\xaa\xbb\xcc"])
        assert len(frames) == 1

    def test_garbage_between_frames(self):
        frames, _ = feed([b"\xff" * 30 + GOOD + b"\x00" * 10 + GOOD2])
        assert len(frames) == 2

    def test_corrupt_checksum_recovers(self):
        bad = bytearray(GOOD)
        bad[8] ^= 0xFF
        frames, _ = feed([bytes(bad)])
        assert frames == []
        # and the parser must still work afterwards
        frames2, _ = feed([bytes(bad), GOOD])
        assert len(frames2) == 1

    def test_random_noise_does_not_stall(self):
        import random
        random.seed(1234)
        for _ in range(50):
            noise = bytes(random.randrange(256) for _ in range(random.randrange(1, 40)))
            frames, _ = feed([noise + GOOD + noise + GOOD2])
            assert len(frames) == 2

    def test_buffer_cap(self):
        buf = bytearray(b"\xff" * 20000)
        A.extract(buf)
        assert len(buf) < 200, "buffer must be capped to avoid unbounded growth"

    def test_extract_returns_direction_and_command(self):
        frames, _ = feed([GOOD])
        sof, cmd, payload = frames[0]
        assert sof == A.SOF_RSP
        assert cmd == 0x4E
        assert len(payload) == 10


class TestSafetyGate:
    """The safety gate is the only thing standing between a curious user and a bricked device."""

    def test_ota_and_delete_blocked(self):
        for cmd in (0x71, 0x72, 0x73, 0x49):
            ok, why = A.guard_command(cmd)
            assert ok is False
            assert why

    def test_ota_and_delete_blocked_even_when_unlocked(self):
        # This is the important one: unlocking must NOT bypass the deny list.
        for cmd in (0x71, 0x72, 0x73, 0x49):
            ok, _ = A.guard_command(cmd, unlocked=True)
            assert ok is False, "OTA/delete must never be reachable"

    def test_read_commands_allowed(self):
        for cmd in A.READ_COMMANDS:
            assert A.guard_command(cmd)[0] is True

    def test_verified_writes_allowed(self):
        for cmd in A.SAFE_WRITE:
            assert A.guard_command(cmd)[0] is True

    def test_unknown_blocked_by_default(self):
        assert A.guard_command(0xAA)[0] is False
        assert A.guard_command(0xAA, unlocked=True)[0] is True

    def test_command_tables_do_not_overlap(self):
        deny = set(A.DENY_COMMANDS)
        read = set(A.READ_COMMANDS)
        write = set(A.SAFE_WRITE)
        assert not (deny & read)
        assert not (deny & write)
        assert not (read & write)

    def test_probe_only_uses_read_commands(self):
        # probe_capabilities must be structurally incapable of writing.
        import inspect
        src = inspect.getsource(A.probe_capabilities)
        assert "READ_COMMANDS" in src
        assert "SAFE_WRITE" not in src


class TestValueMappings:
    def test_listen_modes_cover_0_to_7(self):
        assert set(A.LISTEN_MODES) == set(range(8))

    def test_connect_types_cover_0_to_2(self):
        assert set(A.CONNECT_TYPES) == {0, 1, 2}

    def test_key_to_vk(self):
        assert A.key_to_vk("A") == 0x41
        assert A.key_to_vk("1") == 0x31
        assert A.key_to_vk("F5") == 0x74
        assert A.key_to_vk("") is None
        assert A.key_to_vk("NoSuchKey") is None

    def test_hotkey_desc(self):
        assert A.hotkey_desc({"ctrl": True, "alt": True, "key": "1"}) == "Ctrl+Alt+1"


class TestBuildValidation:
    def test_invalid_command_raises(self):
        try:
            A.build(0x1FF)
        except (ValueError, OverflowError):
            return
        raise AssertionError("out-of-range command should not build silently")