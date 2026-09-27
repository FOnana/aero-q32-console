# -*- coding: utf-8 -*-
"""
Jieli 真无线耳机命令行接口 —— 供脚本 / 批处理 / 计划任务调用

用法:
  python aero_cli.py battery              电量
  python aero_cli.py mode                 查询降噪模式
  python aero_cli.py mode strong          设置降噪 (见下方别名)
  python aero_cli.py connect              查询连接模式
  python aero_cli.py connect ldac         设置连接模式
  python aero_cli.py sound                查询风格音效(声效)
  python aero_cli.py sound 5              设置风格音效 (0-12)
  python aero_cli.py probe                设备能力探测 (只读)
  python aero_cli.py raw 4E [0102]        原始命令 (经安全闸)
  python aero_cli.py json                 一次性输出全部状态的 JSON
  python aero_cli.py doctor               环境诊断 (不连设备, 故障排查第一步)
  python aero_cli.py ports                列出所有蓝牙串口

可选参数:
  --port COM6      指定串口, 跳过自动发现
  --mac AABBCCDDEEFF  指定设备 MAC 优先匹配

降噪别名: off/close=0  strong=1  mild=2  transparent=3  wnr=4
          passthrough=5  voice=6  adaptive=7
连接别名: standard=0  entertainment=1  ldac=2

退出码: 0 成功 / 1 参数或运行错误 / 2 未找到设备 / 3 被安全闸拦截
"""
import sys, json, time
import aero_q32 as A

MODE_ALIAS = {
    "off": 0, "close": 0, "0": 0, "关闭": 0,
    "strong": 1, "1": 1, "强降噪": 1,
    "mild": 2, "2": 2, "轻度": 2, "轻度降噪": 2,
    "transparent": 3, "3": 3, "通透": 3,
    "wnr": 4, "4": 4, "风噪": 4, "风噪降低": 4,
    "passthrough": 5, "5": 5, "通透人声": 5,
    "voice": 6, "6": 6, "人声增强": 6,
    "adaptive": 7, "7": 7, "自适应": 7,
}
CONN_ALIAS = {"standard": 0, "0": 0, "标准": 0, "标准模式": 0,
              "entertainment": 1, "1": 1, "娱乐": 1, "娱乐模式": 1,
              "ldac": 2, "2": 2}

EXIT_OK, EXIT_ERR, EXIT_NODEV, EXIT_BLOCKED = 0, 1, 2, 3


def die(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def parse_opts(argv):
    """提取 --port / --mac, 其余作为位置参数。保持原有位置参数接口不变。"""
    opts = {"port": None, "mac": None}
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--port", "-p") and i + 1 < len(argv):
            opts["port"] = argv[i + 1]; i += 2
        elif a in ("--mac", "-m") and i + 1 < len(argv):
            opts["mac"] = argv[i + 1]; i += 2
        elif a.startswith("--port="):
            opts["port"] = a.split("=", 1)[1]; i += 1
        elif a.startswith("--mac="):
            opts["mac"] = a.split("=", 1)[1]; i += 1
        else:
            rest.append(a); i += 1
    return opts, rest


def connect(opts=None):
    A.setup_logging()
    opts = opts or {}
    dev = A.AeroQ32(port=opts.get("port"), prefer_mac=opts.get("mac"))
    try:
        dev.open()
    except A.PortBusy as e:
        die(EXIT_NODEV, ("串口被占用: %s" % e) +
            "\n  另一个实例可能正在运行。先关闭它, 或运行 doctor 查看。")
    except A.DeviceNotFound as e:
        die(EXIT_NODEV, "未找到耳机: %s" % e)
    except Exception as e:
        die(EXIT_NODEV, "连接失败: %s" % e)
    if dev.handshake() is None:
        dev.close()
        die(EXIT_NODEV, "握手无应答，可能不是本耳机")
    return dev


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return EXIT_OK

    opts, argv = parse_opts(argv)
    if not argv:
        print(__doc__)
        return EXIT_OK

    cmd = argv[0].lower()

    # ---- 不连设备的命令 ----
    if cmd in ("doctor", "diagnose", "env"):
        A.setup_logging()
        print(A.format_report(A.env_report()))
        return EXIT_OK
    if cmd == "ports":
        cands = A.list_spp_ports()
        if not cands:
            print("未发现任何 SPP 串口。")
            return EXIT_NODEV
        for c in cands:
            print("%-8s %-10s MAC=%-14s %s" %
                  (c["device"], "远端设备" if c["remote"] else "本机传入",
                   c["mac"] or "-", c["desc"]))
        return EXIT_OK

    dev = connect(opts)
    try:
        if cmd == "battery":
            b = dev.battery()
            if not b:
                die(EXIT_ERR, "读取电量失败")
            print("左耳 %s%%  充电盒 %s%%  右耳 %s%%" %
                  (b["left"], b["case"], b["right"]))
            return EXIT_OK

        if cmd == "mode":
            if len(argv) == 1:
                m = dev.get_listen_mode()
                if m is None:
                    die(EXIT_ERR, "查询失败")
                print("%s (%d)" % (A.LISTEN_MODES.get(m, "?"), m))
                return EXIT_OK
            key = argv[1].lower()
            if key not in MODE_ALIAS:
                die(EXIT_ERR, "未知模式: %s" % key)
            v = MODE_ALIAS[key]
            got = dev.set_listen_mode(v)
            print("%s (%s)" % (A.LISTEN_MODES.get(got, "?"), got))
            return EXIT_OK if got == v else EXIT_ERR

        if cmd == "connect":
            if len(argv) == 1:
                c = dev.get_connect_option()
                if c is None:
                    die(EXIT_ERR, "查询失败")
                print("%s (%d)" % (A.CONNECT_TYPES.get(c, "?"), c))
                return EXIT_OK
            key = argv[1].lower()
            if key not in CONN_ALIAS:
                die(EXIT_ERR, "未知连接模式: %s" % key)
            v = CONN_ALIAS[key]
            print("切换中（会重建链路，约 10 秒）…", file=sys.stderr)
            dev.set_connect_option(v)
            dev.close()
            time.sleep(9)
            dev = connect()
            got = dev.get_connect_option()
            print("%s (%s)" % (A.CONNECT_TYPES.get(got, "?"), got))
            return EXIT_OK if got == v else EXIT_ERR

        if cmd == "sound":
            if len(argv) == 1:
                s = dev.get_preset_sound()
                if s is None:
                    die(EXIT_ERR, "查询失败")
                print("%s (%d)" % (A.PRESET_SOUND_NAMES.get(s, "预设"), s))
                return EXIT_OK
            try:
                v = int(argv[1])
            except Exception:
                die(EXIT_ERR, "音效编号需为 0-12 的整数")
            if not (0 <= v <= 12):
                die(EXIT_ERR, "音效编号超出已验证范围 0-12")
            got = dev.set_preset_sound(v)
            print("%s (%s)" % (A.PRESET_SOUND_NAMES.get(got, "预设"), got))
            return EXIT_OK if got == v else EXIT_ERR

        if cmd == "probe":
            rows = A.probe_capabilities(dev, wait=1.0)
            sup = [r for r in rows if r[2]]
            for c, name, ok, ln, hx in rows:
                print("0x%02X  %-18s %s  len=%-3d %s" %
                      (c, name, "有应答" if ok else "  --  ", ln, hx[:48]))
            print(">>> 支持 %d / %d" % (len(sup), len(rows)))
            return EXIT_OK

        if cmd == "raw":
            if len(argv) < 2:
                die(EXIT_ERR, "缺少 CMD, 例: raw 4E")
            try:
                c = int(argv[1].replace("0x", ""), 16)
            except Exception:
                die(EXIT_ERR, "CMD 需为十六进制")
            payload = b""
            if len(argv) > 2:
                try:
                    payload = bytes.fromhex(argv[2].replace(" ", ""))
                except Exception:
                    die(EXIT_ERR, "数据需为十六进制")
            ok, why, resp = dev.send_raw(c, payload, unlocked=False)
            if not ok:
                print(why, file=sys.stderr)
                return EXIT_BLOCKED
            print("回读: %s" % (resp.hex(" ") if resp else "(无应答)"))
            return EXIT_OK

        if cmd == "json":
            b = dev.battery() or {}
            m = dev.get_listen_mode()
            c = dev.get_connect_option()
            s = dev.get_preset_sound()
            print(json.dumps({
                "port": dev.port, "mac": dev.mac,
                "battery": b,
                "listen_mode": m, "listen_mode_name": A.LISTEN_MODES.get(m),
                "connect_option": c, "connect_option_name": A.CONNECT_TYPES.get(c),
                "preset_sound": s, "preset_sound_name": A.PRESET_SOUND_NAMES.get(s, "预设"),
                "stats": dict(dev.stats),
            }, ensure_ascii=False))
            return EXIT_OK

        die(EXIT_ERR, "未知子命令: %s" % cmd)
    finally:
        try:
            dev.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
