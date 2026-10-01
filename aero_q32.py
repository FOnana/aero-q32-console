# -*- coding: utf-8 -*-
"""
Jieli 方案真无线耳机 —— Windows SPP 控制库
适用于 1MORE AERO Q32 等采用 Jieli(杰理) 蓝牙方案的耳机。

协议来源: 通过对本机与耳机之间 SPP 链路的互操作性观测独立整理而成,
不包含任何第三方软件的代码或二进制。详见 NOTICE.md。

帧格式:
  [0]    0x11=请求 / 0x01=响应
  [1]    0x01
  [2]    0x00
  [3]    CMD
  [4:6]  payload 长度 (大端)
  [6]    0x00
  [7]    0x01
  [8]    校验 = bytes[0:8] 逐字节 XOR
  [9:]   payload

v2 修复:
  * 帧同步: 主动搜索合法帧头, 脏字节只丢弃不卡死
  * 看门狗: 记录最后有效帧时间, 供上层判断链路是否假死
  * 线程安全: 所有串口访问走同一把可重入锁
  * 端口发现: 用 hwid 里的 SPP UUID + 设备 MAC 识别, 不再猜 COM 号
"""
import serial, serial.tools.list_ports, time, threading, collections, re, json, os
import logging, logging.handlers

CMD = {
    0x4D: "SHAKE_HAND", 0x4E: "BINAURAL_INFO", 0x5E: "SET_LISTEN_MODE",
    0x5F: "GET_LISTEN_MODE", 0x3B: "SET_EQ_MODE", 0x3C: "GET_EQ_MODE",
    0x53: "SET_EQ_PARAMS", 0x54: "GET_EQ_PARAMS", 0x4C: "VOLUME",
    0x3A: "PLAY_CONTROL", 0x5A: "FIND_DEVICE", 0x64: "SET_DOUBLE_CLICK",
    0x65: "GET_DOUBLE_CLICK", 0x66: "SET_TRIPLE_CLICK", 0x67: "GET_TRIPLE_CLICK",
    0x69: "SET_PRESET_SOUND", 0x6A: "GET_PRESET_SOUND",
    0x76: "SET_DUAL_DEVICE", 0x77: "GET_DUAL_DEVICE",
    0x82: "SET_SPATIAL_AUDIO", 0x83: "GET_SPATIAL_AUDIO",
    0x6B: "SET_CONNECT_OPTION", 0x6C: "GET_CONNECT_OPTION",
    0x38: "MUSIC_STATUS",
}

# 蓝牙连接选项 (ConstKt.connectStandard/connectEntertainment/connectLDAC)
CONNECT_TYPES = {0: "标准模式", 1: "娱乐模式", 2: "LDAC"}

LISTEN_MODES = {0: "关闭", 1: "强降噪", 2: "轻度降噪", 3: "通透",
                4: "风噪降低", 5: "通透人声", 6: "人声增强", 7: "自适应"}

SOF_REQ, SOF_RSP = 0x11, 0x01
HDR = 9                 # 固定头 9 字节
MAX_PAYLOAD = 512       # 长度字段超过此值一律视为垃圾
BUF_CAP = 8192          # 缓冲区上限, 防止异常流撑爆内存
SPP_UUID = "00001101"   # 串口服务 UUID

# ---- 帧变体: 多字节字段的字节序, 以及第 8 字节的含义 ----
#
# 1MORE 不同型号的外层封装不一样。目前实测到两种:
#
#   VAR_Q32  AERO Q32:  长度**大端**, 尾字段 00 01, byte8 = XOR(前 8 字节)
#                       —— 校验算法已完全破解
#   VAR_LE   S20PRO  :  长度**小端**, 尾字段 01 00, byte8 不是校验和
#                       —— 已穷举 256 个 CRC-8 多项式 x 初值 0x00/0xFF
#                          x 位反转 x 9 种取值范围, 加上求和/异或/取反,
#                          全部不匹配。且同一命令的**应答**该字节稳定、
#                          设备**主动通知**时变值 => 疑似标记帧来源而非校验。
#
# 判别式就是尾字段: 00 01 -> Q32, 01 00 -> 小端变体。两者不会混淆。
VAR_Q32 = "q32"
VAR_LE = "le"
VARIANTS = {VAR_Q32: "AERO Q32 大端封装", VAR_LE: "小端封装 (如 S20PRO)"}
#
# 变体只影响"怎么解析收到的帧", 不影响安全闸: 小端设备的请求帧格式也已被验证
# (同一套请求帧它应答了 9 条命令), 所以写命令照常放行, 结果以回读为准。


# ============================ 帧编解码 ============================

def build(cmd, payload=b""):
    """构造请求帧"""
    if isinstance(payload, str):
        payload = bytes.fromhex(payload)
    hdr = bytes([SOF_REQ, 0x01, 0x00, cmd]) + len(payload).to_bytes(2, "big") + b"\x00\x01"
    ck = 0
    for b in hdr:
        ck ^= b
    return hdr + bytes([ck]) + payload


def checksum(body8):
    ck = 0
    for b in body8:
        ck ^= b
    return ck


def _plausible(buf, i):
    """buf[i] 是否像一个合法帧头 (两种变体都认)"""
    if i + HDR > len(buf):
        return False
    if not (buf[i] in (SOF_REQ, SOF_RSP)
            and buf[i + 1] == 0x01
            and buf[i + 2] == 0x00):
        return False
    # 尾字段是变体判别式: 00 01 = Q32 大端写法, 01 00 = 小端变体
    return (buf[i + 6], buf[i + 7]) in ((0x00, 0x01), (0x01, 0x00))


def extract(buf, stats=None):
    """
    从 bytearray 缓冲区里切出所有完整合法帧 (原地修改 buf)。
    返回 [(sof, cmd, payload, variant), ...]
    遇到脏字节只前移一位重新同步, 绝不永久卡死。

    variant 见 VAR_Q32 / VAR_LE。大端变体仍做完整 XOR 校验;
    小端变体第 8 字节含义未知, 只能做结构性校验。
    """
    frames = []
    i = 0
    skipped = 0
    while True:
        if i + HDR > len(buf):
            break
        if not _plausible(buf, i):
            i += 1
            skipped += 1
            continue
        var = VAR_Q32 if (buf[i + 6], buf[i + 7]) == (0x00, 0x01) else VAR_LE
        ln = int.from_bytes(buf[i + 4:i + 6],
                            "big" if var == VAR_Q32 else "little")
        if ln > MAX_PAYLOAD:                 # 长度离谱 -> 当作脏字节跳过
            i += 1
            skipped += 1
            if stats is not None:
                stats["resync"] += 1
            continue
        total = HDR + ln
        if i + total > len(buf):
            break                            # 数据还不够, 等下一批
        body = bytes(buf[i:i + HDR])
        if var == VAR_Q32:
            if checksum(body[0:8]) != body[8]:
                i += 1                       # 校验失败 -> 重新同步
                skipped += 1
                if stats is not None:
                    stats["bad_ck"] += 1
                continue
        elif stats is not None:
            # 小端变体的第 8 字节无法校验(见文件头 VAR_LE 注释)。
            # 靠 前 3 字节固定 + 尾字段 + 长度上限 三重约束防误同步:
            # 随机噪声凑出 01 01 00 xx .. 01 00 且长度合理的概率极低。
            stats["le_frames"] += 1
        frames.append((body[0], body[3], bytes(buf[i + HDR:i + total]), var))
        i += total
    if i:
        del buf[:i]
        if stats is not None:
            stats["dropped"] += skipped
    # 缓冲上限保护
    if len(buf) > BUF_CAP:
        if stats is not None:
            stats["dropped"] += len(buf) - 64
        del buf[:len(buf) - 64]
    return frames


# ============================ 端口发现 ============================

def _parse_hwid(hwid):
    r"""从 hwid 解析 (是否远端设备口, 设备MAC)。

    判别依据是**地址**, 不是 hwid 里的 LOCALMFG 字样:
      * 本机传入口的地址恒为 000000000000
      * 真实设备口带着设备自己的 MAC

    ⚠ 曾经写错过: 当时用 "hwid 含 LOCALMFG 就当成本机口"。那是错的 ——
      部分设备不发布 PnP(VID/PID) 记录, Windows 会用 LOCALMFG&xxxx 顶上,
      但地址仍是真实 MAC。这类设备会被**整类漏掉**, 表现就是"搜不到耳机"。
      实测: 1MORE S20PRO 用 LOCALMFG&0046 + 真实 MAC, 就被误判成了本机口。

    三种真实写法:
      带 PnP:   BTHENUM\{00001101-...}_VID&000105D6_PID&000A\...&<MAC>_...
      无 PnP:   BTHENUM\{00001101-...}_LOCALMFG&0046\...&<MAC>_...
      本机传入: BTHENUM\{00001101-...}_LOCALMFG&0000\...&000000000000_...
    """
    h = (hwid or "").upper()
    if "BTHENUM" not in h:
        return False, None
    m = re.search(r"&([0-9A-F]{12})[_\\]", h + "\\")
    mac = m.group(1) if m else None
    if mac is None:
        # 解析不出地址时按远端处理, 交给握手去验证, 宁滥勿缺
        return True, None
    return (mac != "000000000000"), mac


def list_spp_ports():
    """
    列出所有 SPP 串口并标注是否为『远端设备口』。

    为什么不能只看名字: Windows 给本机传入口和远端设备口起的是**同一个名字**
    『蓝牙链接上的标准串行 (COMx)』, 只能靠 hwid 区分。
    """
    out = []
    for p in serial.tools.list_ports.comports():
        hwid = (p.hwid or "").upper()
        if SPP_UUID not in hwid:
            continue
        remote, mac = _parse_hwid(p.hwid)
        out.append({"device": p.device, "mac": mac, "desc": p.description,
                    "hwid": p.hwid, "remote": remote})
    return out


def find_port(prefer_mac=None, prefer_port=None):
    """挑一个可用端口。

    优先级: 显式指定 > 指定的 MAC > 唯一的远端设备口。
    有歧义时返回 None, 交给 discover() 逐个握手探测。
    """
    cands = [c for c in list_spp_ports() if c["remote"]]
    if prefer_port:
        want = str(prefer_port).upper()
        for c in cands:
            if c["device"].upper() == want:
                return c["device"]
    if prefer_mac:
        want = str(prefer_mac).upper().replace(":", "").replace("-", "")
        for c in cands:
            if c["mac"] == want:
                return c["device"]
    if len(cands) == 1:
        return cands[0]["device"]
    return None


def probe_port(port, timeout=1.0, settle=10.0):
    """开这个口试握手, 判断是不是我们的设备。

    返回 'ok'(是本设备) / 'busy'(口被别的进程占用) / 'no'(能开但无应答)

    ⚠ 关键(实测踩过的坑): Windows 蓝牙串口在 serial.Serial() 返回时
      **链路可能还没建立**。实测某耳机要 6 秒以上才通, 这段时间发出去的数据
      全部丢失。如果只发一次握手、等两秒就判定"无应答", 会把本来兼容的设备
      误判成"找不到", 并且每次重试都重复这个错误 —— 表现就是**无限搜索失败循环**。

      所以这里在 settle 秒内**反复重试**握手, 收到合法应答立刻返回。
    """
    try:
        s = serial.Serial(port, 9600, timeout=0.3, write_timeout=2)
    except Exception as e:
        if _is_busy(e):
            return "busy"                        # 被别的进程占用
        if _is_ghost(e):
            return "ghost"                       # 端口"存在"但设备对象没了
        return "no"
    try:
        s.reset_input_buffer()
        end = time.time() + max(settle, timeout)
        buf = bytearray()
        while time.time() < end:
            try:
                s.write(build(0x4D, "01"))
                s.flush()
            except Exception:
                return "no"          # 写失败: 链路断了, 不值得继续
            step = time.time() + timeout
            while time.time() < step:
                n = s.in_waiting
                if n:
                    buf += s.read(n)
                    for sof, cmd, _pl, _var in extract(buf):
                        if sof == SOF_RSP and cmd == 0x4D:
                            return "ok"
                time.sleep(0.05)
        return "no"
    finally:
        try:
            s.close()
        except Exception:
            pass


def discover(timeout=2.0, prefer_port=None, prefer_mac=None, settle=10.0):
    """逐个探测远端 SPP 口, 找出真正会应答握手的那一个。

    返回 (port, mac); 找不到返回 (None, None);
    候选口全被占用则抛 PortBusy —— 这和"没配对"是两码事, 必须区分。
    """
    cands = [c for c in list_spp_ports() if c["remote"]]
    if prefer_port:
        want = str(prefer_port).upper()
        cands.sort(key=lambda c: 0 if c["device"].upper() == want else 1)
    if prefer_mac:
        want = str(prefer_mac).upper().replace(":", "")
        cands.sort(key=lambda c: 0 if c["mac"] == want else 1)
    busy, ghost = [], []
    for c in cands:
        r = probe_port(c["device"], timeout=1.0, settle=settle)
        if r == "ok":
            return c["device"], c["mac"]
        if r == "busy":
            busy.append(c["device"])
        elif r == "ghost":
            ghost.append(c["device"])
    if busy:
        raise PortBusy("串口 %s 被占用 (可能有另一个实例在运行)" % ", ".join(busy))
    if ghost:
        raise GhostPort(
            "串口 %s 存在于列表中但底层设备不存在 —— 通常是 Windows 蓝牙端口分配"
            "残留(多个设备占了同一个 COM 号)。重启电脑, 或在设备管理器里"
            "「显示隐藏的设备」后卸载陈旧的『蓝牙链接上的标准串行』, 再重新配对。"
            % ", ".join(ghost))
    return None, None


# ============================ 客户端 ============================

class WriteBlocked(Exception):
    """写命令被安全闸拦下: 设备只支持只读, 或命令未经验证。"""


class PortBusy(Exception):
    """串口被别的进程占用 —— 与"没找到设备"是两回事, 必须分开报。"""


class DeviceNotFound(Exception):
    """没有找到会应答的耳机串口(未配对 / 未开机 / 不支持 SPP)。"""


class GhostPort(Exception):
    """串口在列表里但底层设备对象不存在(Windows 蓝牙端口分配残留)。"""


def _is_ghost(exc):
    """端口在列表里, 但底层设备对象不存在 —— 打不开。

    典型场景: Windows 的 SERIALCOMM 映射里残留了已卸载的 BthModem 设备,
    而它和当前设备**占用了同一个 COM 号**。此时 CreateFile 会解析到那个
    幽灵设备, 返回"系统找不到指定的文件"。

    这不是耳机的问题, 是 Windows 蓝牙端口分配残留。修复办法见
    docs/TROUBLESHOOTING.md —— 通常重启或重新配对即可。
    """
    m = str(exc).lower()
    return ("filenotfound" in m or "找不到" in m or "does not exist" in m
            or "系统找不到" in str(exc))


def _is_busy(exc):
    """pyserial 会把 Windows 的 PermissionError 包成 SerialException,
    所以不能只看异常类型, 必须看内容。"""
    if isinstance(exc, PermissionError):
        return True
    m = str(exc).lower()
    return any(k in m for k in ("permissionerror", "拒绝访问", "access is denied",
                                "being used by another process", "access denied"))


class AeroQ32:
    def __init__(self, port=None, prefer_mac=None, prefer_port=None, timeout=0.4):
        self.port = port
        self.mac = prefer_mac
        self.prefer_port = prefer_port
        self.timeout = timeout
        self._lock = threading.RLock()        # 串口访问互斥
        self._rx = collections.deque(maxlen=64)
        self._buf = bytearray()
        self._stop = False
        self._thread = None
        self.stats = collections.Counter()
        self.last_rx = 0.0                    # 最后一个有效帧的时间
        self.opened_at = 0.0
        self.ser = None
        # 首次收到合法帧后确定; None = 还没见过帧(按 VAR_Q32 处理)
        self.variant = None
        self._unverified_noted = False

    # ---- 生命周期 ----
    def open(self):
        if self.port is None:
            # 上次成功的端口/MAC 只当"提示", 仍会逐个握手验证,
            # 因此换机器、重新配对、COM 号漂移都不会连错设备。
            cfg = load_settings()
            p, m = discover(prefer_port=self.prefer_port or cfg.get("last_port"),
                            prefer_mac=self.mac or cfg.get("last_mac"))
            if p is None:
                raise DeviceNotFound(
                    "未找到可应答的耳机串口。请运行 aero_cli.py doctor 查看环境诊断。")
            self.port, self.mac = p, m
        try:
            self.ser = serial.Serial(self.port, 9600, timeout=self.timeout, write_timeout=2)
        except Exception as e:
            if _is_busy(e):
                raise PortBusy("串口 %s 被占用 (可能有另一个实例在运行)" % self.port) from e
            raise
        self._stop = False
        self._buf.clear()
        self._rx.clear()
        self.opened_at = time.time()
        self.last_rx = self.opened_at
        # 丢弃打开瞬间的残留
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        # 记下本次成功的端口, 下次优先尝试(仅作提示, 仍会验证)
        try:
            cfg = load_settings()
            if cfg.get("last_port") != self.port or cfg.get("last_mac") != self.mac:
                cfg["last_port"] = self.port
                cfg["last_mac"] = self.mac
                save_settings(cfg)
        except Exception:
            log().exception("记录 last_port 失败(不影响使用)")
        return self.port

    def close(self):
        self._stop = True
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.5)
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    def _reader(self):
        while not self._stop:
            try:
                n = self.ser.in_waiting if self.ser else 0
                if n:
                    data = self.ser.read(n)
                    self._buf += data
                    for sof, cmd, pl, var in extract(self._buf, self.stats):
                        if self.variant is None:
                            self.variant = var
                            log().info("检测到帧变体: %s (%s)",
                                       var, VARIANTS.get(var, "?"))
                        self._rx.append((time.time(), sof, cmd, pl))
                        self.last_rx = time.time()
                        self.stats["frames"] += 1
                else:
                    time.sleep(0.02)
            except Exception:
                time.sleep(0.1)

    # ---- 健康度 ----
    def healthy(self, max_age=6.0):
        return self.ser is not None and (time.time() - self.last_rx) < max_age

    # ---- 收发 ----
    def send(self, cmd, payload=b""):
        with self._lock:
            if not self.ser:
                raise serial.SerialException("串口未打开")
            self.ser.write(build(cmd, payload))
            self.ser.flush()

    def request(self, cmd, payload=b"", wait=2.0, want=None):
        """加锁完成一次 发送->等待应答, 保证响应不会被别的线程吃掉"""
        want = cmd if want is None else want
        with self._lock:
            if not self.ser:
                raise serial.SerialException("串口未打开")
            self._rx.clear()
            t0 = time.time()
            self.ser.write(build(cmd, payload))
            self.ser.flush()
            end = t0 + wait
            while time.time() < end:
                for ts, sof, c, pl in list(self._rx):
                    if sof == SOF_RSP and c == want and ts >= t0:
                        return pl
                time.sleep(0.03)
            self.stats["timeouts"] += 1
            return None

    # ---- 业务 ----
    def handshake(self, wait=2.0, retries=4):
        """握手。链路刚建立时前几次可能丢, 因此重试若干次。"""
        for i in range(max(1, retries)):
            p = self.request(0x4D, "01", wait=wait)
            if p is not None:
                return p
            if i < retries - 1:
                time.sleep(0.7)
        return None

    def battery(self):
        """读取电量。附带 raw 原始报文, 便于排查数值异常跳动。"""
        p = self.request(0x4E, wait=2.5)
        if not p or len(p) < 9:
            return None
        return {"left": p[4], "right": p[8], "case": p[9] if len(p) > 9 else None,
                "raw": p.hex(" "), "len": len(p)}

    def get_listen_mode(self):
        p = self.request(0x5F, wait=2.0)
        return p[0] if p else None

    # ---- 写命令闸门 ----
    @property
    def writes_unverified(self):
        """这台设备的**写入**方向还没验证过(实测小端变体如 1MORE S20PRO)。

        注意区别: 它的**请求帧格式是验证过的** —— 对同一套请求帧, 它正确应答了
        9 条不同命令(0x4D/0x4E/0x6C/0x6A/0x3C/0x54/0x77/0x83/0x87)。
        没验证的只是"发过去的设置会不会生效"。

        所以这里**不拦截**, 只用于界面提示和首次写入时记一笔日志。
        真正的变砖路径(OTA/不可逆删除)由 DENY_COMMANDS 单独焊死, 与此无关。
        """
        return self.variant == VAR_LE

    def require_write(self, cmd):
        """写命令放行检查, 不放行直接抛 WriteBlocked。

        set_listen_mode / set_preset_sound / set_connect_option 这几个方法
        以前是**直接 send** 的, 绕过了安全闸 —— 点按钮或按热键照样会把命令
        发出去。现在它们和 send_raw 走同一道门, DENY_COMMANDS 对它们同样生效。
        """
        ok, why = guard_command(cmd, variant=self.variant or VAR_Q32)
        if not ok:
            log().warning("拦截写命令 0x%02X: %s", cmd, why)
            raise WriteBlocked(why)
        if self.writes_unverified and not self._unverified_noted:
            self._unverified_noted = True
            log().warning("首次向未验证写入的设备发送 0x%02X: 该方向的帧格式没验证过,"
                          " 是否生效以设备回读为准", cmd)

    def send_raw(self, cmd, payload=b"", unlocked=False, wait=2.0):
        """原始命令发送(控制台用)。强制经过安全闸。
        返回 (是否放行, 说明, 回读payload 或 None)"""
        ok, why = guard_command(cmd, unlocked=unlocked,
                               variant=self.variant or VAR_Q32)
        if not ok:
            log().warning("控制台拦截 0x%02X: %s", cmd, why)
            return False, why, None
        log().info("控制台发送 0x%02X payload=[%s] (%s)", cmd, payload.hex(" "), why)
        try:
            p = self.request(cmd, payload, wait=wait)
            log().info("控制台回读 0x%02X -> %s", cmd, p.hex(" ") if p else "(无应答)")
            return True, why, p
        except Exception as e:
            log().exception("控制台发送 0x%02X 失败", cmd)
            return True, why + " (发送异常 %s)" % type(e).__name__, None

    def get_connect_option(self):
        """查询蓝牙连接选项: 0=标准 1=娱乐 2=LDAC; 机型不支持返回 None"""
        p = self.request(0x6C, wait=2.0)
        return p[0] if p else None

    def get_preset_sound(self):
        """查询风格音效(预设音效) ID; 机型不支持返回 None"""
        p = self.request(PRESET_SOUND_CMD_GET, wait=1.5)
        return p[0] if p else None

    def set_preset_sound(self, v, verify_timeout=2.0):
        """设置风格音效并轮询回读确认。

        注意: 这是「声效」, 与「降噪」(0x5E/0x5F)是两套独立的东西,
        互不影响: 降噪管环境声, 声效管音色曲线。
        """
        self.require_write(PRESET_SOUND_CMD_SET)
        with self._lock:
            self.send(PRESET_SOUND_CMD_SET, bytes([v]))
            time.sleep(0.30)
            end = time.time() + verify_timeout
            got = None
            while True:
                got = self.get_preset_sound()
                if got == v:
                    return got
                if time.time() >= end:
                    return got
                time.sleep(0.20)

    def set_connect_option(self, v, settle=3.0):
        """设置蓝牙连接选项 (0=标准 1=娱乐 2=LDAC)。

        ⚠ 切换到不同选项会触发耳机重建 A2DP 链路，期间 SPP 控制通道会中断，
          写入抛 SerialTimeoutException 属**预期行为**，不是故障。
          本方法只负责发出；回读需上层重连后再做。
        """
        self.require_write(0x6B)
        with self._lock:
            if not self.ser:
                raise serial.SerialException("串口未打开")
            try:
                self.ser.write(build(0x6B, bytes([v])))
                self.ser.flush()
            except Exception:
                pass                       # 链路被打断, 预期内
            time.sleep(settle)

    def set_listen_mode(self, mode, verify_timeout=3.0):
        """写后回读确认。

        耳机切换降噪模式是**异步**的: 自适应(7)这类模式切得慢, 过早回读会读到
        过渡中的旧值。所以这里要回读确认, 但**读得不能太密** ——
        耳机 MCU 同时还要跑音频链路, 密集查询会挤占它, 反而让切换更容易失败。

        因此: 先给 0.45s 静默应用时间, 之后每 0.65s 读一次, 最多约 4 次。
        未生效时返回最后一次观察到的值, 并把原始报文写进日志以便排查。
        """
        self.require_write(0x5E)
        with self._lock:
            self.send(0x5E, bytes([mode]))
            time.sleep(0.45)
            end = time.time() + verify_timeout
            got = None
            tries = 0
            while True:
                p = self.request(0x5F, wait=2.0)
                got = p[0] if p else None
                tries += 1
                if got == mode:
                    return got
                if time.time() >= end:
                    log().warning("设置降噪 %d 未被采纳: 回读=%s raw=%s 探测 %d 次",
                                  mode, got, p.hex(" ") if p else "(无应答)", tries)
                    return got
                time.sleep(0.65)


# ============================ 安全闸 ============================
# 只读命令: 只查询、无副作用。能力探测与原始控制台均可无风险发送。
READ_COMMANDS = {
    0x4E: "电量(双耳信息)",
    0x5F: "降噪模式",
    0x6C: "连接/娱乐模式",
    0x3C: "EQ 模式",
    0x54: "EQ 参数",
    0x63: "自动播放",
    0x65: "双击自定义",
    0x67: "三击自定义",
    0x6A: "预设音效",
    0x70: "睡眠检测",
    0x77: "双设备连接",
    0x81: "热词监听",
    0x83: "空间音频",
    0x87: "自定义按键顺序",
    0x91: "佩戴检测",
    0x93: "佩戴检测深度",
    0x95: "最大音量限制",
    0x97: "人声增强",
    0x38: "音乐状态",
    0x39: "音乐列表",
}

# 永久禁止: 固件刷写 / 不可逆删除 —— 任何情况下都不放行
DENY_COMMANDS = {
    0x71: "OTA 起始/音乐同步帧(刷写固件)",
    0x72: "OTA 传输帧(刷写固件)",
    0x73: "OTA 完成帧(刷写固件)",
    0x49: "删除耳机内音乐(不可逆)",
}

# 本项目已实测验证过的写命令
SAFE_WRITE = {
    0x5E: "设置降噪模式", 0x6B: "设置连接/娱乐模式", 0x4C: "音量",
    0x3A: "播放控制", 0x5A: "查找耳机", 0x64: "设置双击", 0x66: "设置三击",
    0x69: "设置预设音效", 0x76: "设置双设备连接", 0x82: "设置空间音频",
    0x80: "设置热词", 0x84: "设置自定义顺序", 0x90: "设置佩戴检测",
    0x92: "设置佩戴深度", 0x94: "设置最大音量限制", 0x96: "设置人声增强",
    0x62: "设置自动播放", 0x68: "设置播放时长", 0x6F: "设置睡眠检测",
}


def guard_command(cmd, unlocked=False, variant=VAR_Q32):
    """安全闸唯一入口。返回 (是否放行, 说明)。

    变体(variant)不影响放行判断: 小端设备的**请求**帧格式已经被验证过 ——
    对同一套请求帧它正确应答了 9 条不同命令。没验证的只是写入是否生效,
    那不是安全问题(命令本身非破坏性), 如实报告结果即可。
    """
    if cmd in DENY_COMMANDS:
        return False, "⛔ 永久禁止: %s" % DENY_COMMANDS[cmd]
    if cmd in READ_COMMANDS:
        return True, "只读查询: %s" % READ_COMMANDS[cmd]
    if cmd in SAFE_WRITE:
        return True, "已实测写命令: %s" % SAFE_WRITE[cmd]
    if unlocked:
        return True, "⚠ 未验证命令 (危险模式已解锁)"
    return False, "⚠ 未验证命令 0x%02X，默认拦截。确需发送请先解锁危险模式" % cmd


# ============================ 能力探测 ============================

def probe_capabilities(dev, on_result=None, wait=1.2):
    """逐条发送**只读**命令, 探测设备实际支持哪些功能。

    安全保证: 只遍历 READ_COMMANDS, 绝不发送任何写命令。
    返回 [(cmd, 名称, 是否有应答, 回读长度, 回读hex), ...]
    """
    out = []
    for cmd in sorted(READ_COMMANDS):
        name = READ_COMMANDS[cmd]
        try:
            p = dev.request(cmd, wait=wait)
        except Exception as e:
            p = None
            name = "%s (异常 %s)" % (name, type(e).__name__)
            log().warning("能力探测 %02X 异常: %s", cmd, e)
        row = (cmd, name, p is not None, len(p) if p else 0, p.hex(" ") if p else "")
        out.append(row)
        if on_result:
            on_result(row)
    return out


# ============================ 日志 ============================

_LOG = None


def setup_logging(logdir=None, name="aero_q32", level=logging.INFO):
    """日志落盘(滚动: 单文件 1MB, 保留 3 份)。返回日志文件路径。

    可重复调用。若目标路径变化会**关闭旧句柄再切换**, 避免两个问题:
      1) 改了数据目录, 日志却仍写进老目录;
      2) 文件句柄泄漏 —— Windows 上句柄未关会导致该文件无法删除。
    """
    global _LOG
    if logdir is None:
        logdir = os.path.join(config_dir(), "logs")
    os.makedirs(logdir, exist_ok=True)
    path = os.path.join(logdir, "aero_q32.log")
    lg = logging.getLogger(name)
    lg.setLevel(level)
    lg.propagate = False

    want = os.path.abspath(path)
    for h in list(lg.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            have = os.path.abspath(getattr(h, "baseFilename", "") or "")
            if have == want:                 # 已经指向同一文件, 直接复用
                _LOG = lg
                return path

    close_logging(name)                      # 目标变了或首次调用
    h = logging.handlers.RotatingFileHandler(path, maxBytes=1 << 20,
                                             backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s"))
    lg.addHandler(h)
    _LOG = lg
    return path


def close_logging(name="aero_q32"):
    """关闭并移除日志句柄, 释放文件占用。

    退出前、切换数据目录前、测试清理时都应调用。
    """
    global _LOG
    lg = logging.getLogger(name)
    for h in list(lg.handlers):
        try:
            h.flush()
        except Exception:
            pass
        try:
            h.close()
        except Exception:
            pass
        lg.removeHandler(h)
    if _LOG is lg:
        _LOG = None


def log():
    """取日志器; 未初始化则自动初始化, 任何情况下都不抛异常。"""
    global _LOG
    if _LOG is None:
        try:
            setup_logging()
        except Exception:
            lg = logging.getLogger("aero_q32.fallback")
            lg.addHandler(logging.NullHandler())
            _LOG = lg
    return _LOG

# ============================ 风格音效 (声效) ============================
# 官方 SoundOptionsActivity「风格音效」页 -> 默认音效(预设) 子项
# 与降噪(0x5E/0x5F)是**两套独立的东西**: 降噪管环境声, 声效管音色曲线
PRESET_SOUND_CMD_GET = 0x6A
PRESET_SOUND_CMD_SET = 0x69
# 名称来自 DefaultSoundActivity.getStyleCmd() 的代码标识, 非猜测
PRESET_SOUND_NAMES = {3: "声学(默认)", 11: "响亮", 12: "自定义 EQ", 13: "THX"}


# ============================ 配置持久化 ============================

def config_dir():
    """数据目录(配置与日志)。

    可用环境变量 AEROQ32_DATA_DIR 覆盖, 便于:
      * 便携模式(绿色版, 数据跟程序走)
      * 多份配置并存 / 测试隔离
    """
    d = os.environ.get("AEROQ32_DATA_DIR")
    if not d:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "AeroQ32")
    d = os.path.abspath(d)
    os.makedirs(d, exist_ok=True)
    return d


def config_path():
    return os.path.join(config_dir(), "settings.json")


def log_path():
    return os.path.join(config_dir(), "logs", "aero_q32.log")


DEFAULT_SETTINGS = {
    "hotkeys": [
        {"mode": 1, "ctrl": True, "alt": True, "shift": False, "key": "1"},
        {"mode": 0, "ctrl": True, "alt": True, "shift": False, "key": "2"},
        {"mode": 3, "ctrl": True, "alt": True, "shift": False, "key": "3"},
    ],
    "low_battery": 20,
    "heartbeat_sec": 60,
}


def load_settings():
    """读配置; 任何异常都退回默认值, 绝不影响启动。"""
    p = config_path()
    try:
        if os.path.exists(p):
            d = json.loads(open(p, encoding="utf-8").read())
            out = dict(DEFAULT_SETTINGS)
            out.update(d or {})
            return out
    except Exception:
        log().exception("读取配置失败, 使用默认值")
    return dict(DEFAULT_SETTINGS)


def save_settings(d):
    try:
        open(config_path(), "w", encoding="utf-8").write(
            json.dumps(d, ensure_ascii=False, indent=2))
        return True
    except Exception:
        log().exception("保存配置失败")
        return False


# ============================ 热键 <-> Win32 VK ============================
VK_SPECIAL = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "Space": 0x20, "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22, "Up": 0x26, "Down": 0x28,
    "Left": 0x25, "Right": 0x27, "Tab": 0x09, "Backspace": 0x08,
}
VK_PUNCT = {"-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, ";": 0xBA,
            "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0}


def key_to_vk(key):
    """Qt 键名 -> Win32 虚拟键码; 不支持返回 None"""
    if not key:
        return None
    key = str(key)
    if key in VK_SPECIAL:
        return VK_SPECIAL[key]
    if key in VK_PUNCT:
        return VK_PUNCT[key]
    if len(key) == 1:
        ch = key.upper()
        if ("A" <= ch <= "Z") or ("0" <= ch <= "9"):
            return ord(ch)
    return None


def hotkey_desc(h):
    parts = []
    if h.get("ctrl"): parts.append("Ctrl")
    if h.get("alt"): parts.append("Alt")
    if h.get("shift"): parts.append("Shift")
    parts.append(str(h.get("key", "?")))
    return "+".join(parts)

# ============================ 版本与环境诊断 ============================

VERSION = '1.1.0'


def env_report():
    '''收集环境信息, 供跨机器排查。

    安全保证: 只枚举, 不打开串口, 不发送任何命令。
    可安全地在任何机器上运行, 不会打扰耳机。
    '''
    import platform, sys
    rep = {
        'app_version': VERSION,
        'os': platform.platform(),
        'python': sys.version.split()[0],
        'pyserial': getattr(serial, 'VERSION', 'unknown'),
        'data_dir': config_dir(),
        'config_path': config_path(),
        'log_path': log_path(),
        'all_com_ports': [],
        'spp_remote': [],
        'spp_local': [],
        'verdict': '',
        'hints': [],
    }
    try:
        ports = list(serial.tools.list_ports.comports())
    except Exception as e:
        rep['verdict'] = '枚举串口失败: %s' % e
        rep['hints'].append('pyserial 无法枚举串口, 请确认已正确安装 pyserial。')
        return rep

    for p in ports:
        rep['all_com_ports'].append({'device': p.device,
                                     'desc': p.description,
                                     'hwid': p.hwid})
    for c in list_spp_ports():
        (rep['spp_remote'] if c['remote'] else rep['spp_local']).append(c)

    bt_any = any('BTHENUM' in (x['hwid'] or '').upper()
                 for x in rep['all_com_ports'])
    if not bt_any:
        rep['verdict'] = '未发现任何蓝牙串口'
        rep['hints'] += [
            '1) 确认系统蓝牙已打开, 且耳机已在 设置 > 蓝牙和设备 中配对完成。',
            '2) 部分第三方蓝牙驱动(旧版 CSR / Broadcom 栈)不提供 SPP 串口;',
            '   可在设备管理器里把蓝牙适配器换成微软自带驱动后重试。',
            '3) 若曾配对过但串口未生成, 可删除该设备后重新配对。',
        ]
    elif not rep['spp_remote']:
        rep['verdict'] = '有蓝牙串口, 但没有远端设备口'
        rep['hints'] += [
            '只找到本机传入口(hwid 含 LOCALMFG)。通常意味着:',
            '1) 耳机尚未配对, 或该型号不提供 SPP 串口;',
            '2) 耳机未开机 / 不在范围内。',
        ]
    elif len(rep['spp_remote']) == 1:
        c = rep['spp_remote'][0]
        rep['verdict'] = '环境正常, 远端设备口 %s (MAC %s)' % (c['device'], c['mac'])
    else:
        rep['verdict'] = ('发现 %d 个远端设备口, 连接时会逐个握手探测'
                          % len(rep['spp_remote']))
    return rep


def format_report(rep):
    '''把 env_report() 的结果渲染成可读文本'''
    L = []
    L.append('应用版本   : %s' % rep.get('app_version'))
    L.append('系统       : %s' % rep.get('os'))
    L.append('Python     : %s' % rep.get('python'))
    L.append('pyserial   : %s' % rep.get('pyserial'))
    L.append('数据目录   : %s' % rep.get('data_dir'))
    L.append('日志文件   : %s' % rep.get('log_path'))
    L.append('')
    L.append('全部串口 (%d):' % len(rep.get('all_com_ports', [])))
    for p in rep.get('all_com_ports', []):
        L.append('  %-8s %s' % (p['device'], p['desc']))
        L.append('           %s' % p['hwid'])
    L.append('')
    L.append('远端设备口 (%d):' % len(rep.get('spp_remote', [])))
    for c in rep.get('spp_remote', []):
        L.append('  %-8s MAC=%s' % (c['device'], c['mac']))
    L.append('本机传入口 (%d):' % len(rep.get('spp_local', [])))
    for c in rep.get('spp_local', []):
        L.append('  %-8s' % c['device'])
    L.append('')
    L.append('结论: %s' % rep.get('verdict'))
    for h in rep.get('hints', []):
        L.append('  - ' + h)
    return chr(10).join(L)

# ============================ 设备名识别 ============================

def device_name(mac):
    '''由设备 MAC 查出系统记录的蓝牙设备友好名。

    用途: 窗口标题 / 托盘提示 显示真实设备名, 而不是写死某个型号。
    只读注册表, 不需要管理员权限; 查不到返回 None。
    '''
    if not mac:
        return None
    key = str(mac).upper().replace(':', '').replace('-', '')
    if len(key) != 12:
        return None
    try:
        import winreg
    except Exception:
        return None                      # 非 Windows

    # 方法 A: 设备节点的 FriendlyName (最稳定, 无需管理员)
    try:
        base = 'SYSTEM' + chr(92) + 'CurrentControlSet' + chr(92) + 'Enum' + chr(92) + \
               'BTHENUM' + chr(92) + 'DEV_' + key
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                sub = winreg.EnumKey(k, i)
                try:
                    with winreg.OpenKey(k, sub) as sk:
                        v = winreg.QueryValueEx(sk, 'FriendlyName')[0]
                        if v:
                            return str(v)
                except FileNotFoundError:
                    continue
    except Exception:
        pass

    # 方法 B: BTHPORT 下存的 Name (REG_BINARY, UTF-8, 末尾带 NUL)
    try:
        p = 'SYSTEM' + chr(92) + 'CurrentControlSet' + chr(92) + 'Services' + chr(92) + \
            'BTHPORT' + chr(92) + 'Parameters' + chr(92) + 'Devices' + chr(92) + key
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, p) as k:
            d, typ = winreg.QueryValueEx(k, 'Name')
            if isinstance(d, bytes):
                v = d.rstrip(b'\x00').decode('utf-8', 'replace')
                if v:
                    return v
    except Exception:
        pass
    return None
