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
        sof, cmd, payload, var = frames[0]
        assert sof == A.SOF_RSP
        assert cmd == 0x4E
        assert len(payload) == 10
        assert var == A.VAR_Q32


# ---- 小端变体 (1MORE S20PRO) ----
#
# 抓自真机。与 Q32 同属一套协议族, 但每个 16 位字段都是小端, 且第 8 字节不是校验和:
# 穷举 256 个 CRC-8 多项式 x 初值 0x00/0xFF x 位反转 x 9 种取值范围, 加上求和、异或、
# 取反, 9 个样本全部不匹配。它更像是标记帧的来源 —— 同一条 0x4E 的应答是 0x89,
# 而设备主动推送的同类帧是 0x3f。既然无法校验, 就只能靠结构性约束防误同步。
LE_HANDSHAKE = bytes.fromhex("0101004d010001004301")
LE_BATTERY = bytes.fromhex("0101004e0a0001008900020006640200066457")


class TestLittleEndianVariant:
    def test_le_frame_parses(self):
        frames, leftover = feed([LE_HANDSHAKE])
        assert len(frames) == 1
        assert leftover == b""
        sof, cmd, payload, var = frames[0]
        assert sof == A.SOF_RSP
        assert cmd == 0x4D
        assert payload == b"\x01"
        assert var == A.VAR_LE

    def test_le_length_field_is_little_endian(self):
        frames, _ = feed([LE_BATTERY])
        assert len(frames) == 1
        assert len(frames[0][2]) == 10

    def test_le_battery_layout_matches_q32(self):
        # Values sit at the same offsets as the Q32: p[4] left, p[8] right, p[9] case.
        frames, _ = feed([LE_BATTERY])
        p = frames[0][2]
        assert (p[4], p[8], p[9]) == (100, 100, 87)

    def test_le_frame_survives_unknown_checksum_byte(self):
        # Byte 8 is unverifiable on this variant, so it must not gate parsing.
        mutated = bytearray(LE_HANDSHAKE)
        mutated[8] = 0x00
        frames, _ = feed([bytes(mutated)])
        assert len(frames) == 1

    def test_le_and_q32_frames_mix(self):
        frames, _ = feed([LE_HANDSHAKE, GOOD, GOOD2])
        assert [f[3] for f in frames] == [A.VAR_LE, A.VAR_Q32, A.VAR_Q32]

    def test_le_frame_split_across_reads(self):
        frames, _ = feed([LE_BATTERY[:4], LE_BATTERY[4:]])
        assert len(frames) == 1

    def test_unknown_tail_is_not_a_frame(self):
        # 00 02 is neither variant tail - must never be accepted.
        frames, _ = feed([bytes.fromhex("0101004d010000024301")])
        assert frames == []

    def test_le_writes_blocked_by_default(self):
        # The request-frame format for this variant is unverified: reads are safe,
        # writes would be tested on a real pair of earbuds.
        for cmd in A.SAFE_WRITE:
            ok, why = A.guard_command(cmd, variant=A.VAR_LE)
            assert ok is False, why

    def test_le_reads_still_allowed(self):
        for cmd in A.READ_COMMANDS:
            assert A.guard_command(cmd, variant=A.VAR_LE)[0] is True

    def test_le_deny_list_unaffected(self):
        for cmd in A.DENY_COMMANDS:
            assert A.guard_command(cmd, unlocked=True, variant=A.VAR_LE)[0] is False


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


class TestConsoleEncoding:
    """中文 Windows 控制台是 cp936, 而拦截提示里带 ⛔/⚠。

    修复前: 被拦截的 raw 命令抛 UnicodeEncodeError, 退出码从 3 变成 1,
    调用方无法区分"被安全闸拒绝"和"程序崩了"。
    """

    def test_guard_messages_survive_gbk(self):
        for cmd in list(A.DENY_COMMANDS) + [0xAA]:
            _, why = A.guard_command(cmd)
            assert why.encode("gbk", errors="replace")

    def test_le_guard_message_survives_gbk(self):
        _, why = A.guard_command(0x5E, variant=A.VAR_LE)
        assert why.encode("gbk", errors="replace")

    def test_cli_relaxes_console_errors(self):
        import inspect, aero_cli
        assert "reconfigure" in inspect.getsource(aero_cli)


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