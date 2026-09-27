# -*- coding: utf-8 -*-
"""
Jieli 真无线耳机控制台 —— Windows 桌面端
深色工具型界面。布局参考同类耳机伴侣应用的一般做法:
电量概览 -> 降噪模式 -> 连接模式 -> 风格音效 -> 高级调试。

本软件为独立第三方实现, 与耳机厂商无任何隶属关系。详见 NOTICE.md。

v3 新增:
  * 设备自检 / 能力探测 (只读, 绝不写设备)
  * 全局热键 Ctrl+Alt+1/2/3 快速切降噪
  * 托盘常驻 + 右键菜单
  * 原始命令控制台 (带安全闸: OTA/删除类命令永久禁止)
  * 低电量通知
  * 异常日志落盘
"""
import sys, os, time, threading, ctypes
from ctypes import wintypes
from PySide6.QtCore import Qt, QTimer, QObject, Signal, QAbstractNativeEventFilter
from PySide6.QtGui import (QPainter, QColor, QPen, QFont, QFontDatabase,
                           QTextCursor, QIcon, QAction, QKeySequence)
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                               QLabel, QPushButton, QFrame, QScrollArea,
                               QSizePolicy, QPlainTextEdit, QLineEdit,
                               QCheckBox, QSystemTrayIcon, QMenu, QMessageBox,
                               QDialog, QComboBox, QDialogButtonBox)

import aero_q32 as A

BG, PANEL, PANEL_HI = "#17181C", "#212329", "#2A2D35"
BORDER, TEXT, MUTED = "#343841", "#E4E6EA", "#8A8F99"
ACCENT, OK, WARN = "#FB3A1F", "#4CC38A", "#E5A00D"

APP_TITLE = "Earbuds Console"   # 应用名; 连接后会自动加上识别到的设备名
APP_TITLE_ZH = "蓝牙耳机控制台"

# ── 轮询频率 ──────────────────────────────────────────────────────────
# 各查询按各自节奏进行, 而不是每轮全问一遍。
# 耳机 MCU 要同时处理音频链路和这些查询, 问得太勤会挤占它,
# 实测表现为"命令发出去了, 设备却不采纳"。官方 App 不做持续轮询。
POLL_BATTERY = 10.0       # 电量变化很慢, 不用频繁问
POLL_MODE = 4.0           # 降噪模式可能被耳机自己改(如子模式回落), 稍勤一点
POLL_CONN = 15.0
POLL_SOUND = 30.0
QUIET_AFTER_CMD = 3.0     # 用户发命令后的静默窗口: 这段时间不打扰设备

MAX_LOG_LINES = 400
LOW_BATTERY = 20          # 低电量阈值 % (可被配置覆盖)
HEARTBEAT_SEC = 60        # 无变化时的心跳日志间隔 (可被配置覆盖)

QSS = f"""
QWidget {{ background:{BG}; color:{TEXT};
   font-family:'Microsoft YaHei UI','Segoe UI','Yu Gothic UI','Malgun Gothic',sans-serif;
   font-size:13px; }}
#panel {{ background:{PANEL}; border:1px solid {BORDER}; border-radius:8px; }}
#panelTitle {{ color:{MUTED}; font-size:11px; font-weight:600; }}
#muted {{ color:{MUTED}; }}
#big {{ font-size:24px; font-weight:700; }}
QPushButton#ghost {{ background:{PANEL_HI}; color:{TEXT}; border:1px solid {BORDER}; border-radius:6px; padding:6px 14px; }}
QPushButton#ghost:hover {{ background:#343841; }}
QPlainTextEdit {{ background:#101114; border:1px solid {BORDER}; border-radius:6px; color:#9FE8B8;
   font-family:Consolas,'Cascadia Mono',monospace; font-size:11px; }}
QLineEdit {{ background:#101114; border:1px solid {BORDER}; border-radius:6px; padding:5px 8px;
   font-family:Consolas,monospace; font-size:12px; color:{TEXT}; }}
QLineEdit:focus {{ border:1px solid {ACCENT}; }}
QCheckBox {{ color:{MUTED}; font-size:12px; }}
QMenu {{ background:{PANEL}; border:1px solid {BORDER}; padding:4px; }}
QMenu::item {{ padding:6px 22px; border-radius:4px; }}
QMenu::item:selected {{ background:{ACCENT}; color:#fff; }}
QScrollArea {{ border:none; background:transparent; }}
QScrollBar:vertical {{ background:transparent; width:10px; }}
QScrollBar::handle:vertical {{ background:#3A3E47; border-radius:5px; min-height:30px; }}
QScrollBar::handle:vertical:hover {{ background:#4A4F5A; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
"""


# ---------------- 单实例保护 ----------------
# 两个实例会同时抢串口和全局热键: 后者连不上设备、热键也注册不上,
# 而且先启动的那个可能已被最小化到托盘, 用户根本看不见。
_SINGLE_MUTEX = None


def acquire_single_instance(name="AeroQ32_Control_Console_SingleInstance"):
    """返回 True 表示本进程是唯一实例; False 表示已有实例在运行。"""
    global _SINGLE_MUTEX
    try:
        # use_last_error=True 让 ctypes 在调用返回的瞬间就保存错误码,
        # 否则之后任何 Win32 调用都可能把它冲掉, 导致误判 "已在运行"。
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        ctypes.set_last_error(0)
        h = k.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not h:
            A.log().warning("CreateMutexW 失败 err=%s, 按唯一实例继续", err)
            return True
        already = (err == 183)                  # ERROR_ALREADY_EXISTS
        _SINGLE_MUTEX = h                       # 持有到进程结束, 勿释放
        A.log().info("单实例检查: already_running=%s", already)
        return not already
    except Exception:
        A.log().exception("单实例检查失败, 按唯一实例继续")
        return True


def res_path(name):
    """兼容开发运行与 PyInstaller 打包后的资源路径"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def mkpen(color, width, cap=Qt.RoundCap):
    p = QPen(QColor(color)); p.setWidthF(float(width))
    p.setCapStyle(cap); p.setJoinStyle(Qt.RoundJoin)
    return p


# ---------------- 全局热键 (Win32 RegisterHotKey) ----------------
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY = 0x0312


class MSG(ctypes.Structure):
    _fields_ = [("hWnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt_x", wintypes.LONG),
                ("pt_y", wintypes.LONG)]


class HotkeyManager(QObject, QAbstractNativeEventFilter):
    fired = Signal(int)

    def __init__(self, parent=None):
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self.registered = []
        self.ok = False
        self.err = ""

    def install(self, bindings):
        """bindings: [{'mode':1,'ctrl':True,'alt':True,'shift':False,'key':'1'}, ...]
        失败只记录, 不影响主功能。"""
        self.uninstall()
        failed = []
        try:
            u = ctypes.windll.user32
            for i, hb in enumerate(bindings, start=1):
                vk = A.key_to_vk(hb.get("key"))
                if vk is None:
                    failed.append(A.hotkey_desc(hb))
                    continue
                mods = MOD_NOREPEAT
                if hb.get("ctrl"): mods |= MOD_CONTROL
                if hb.get("alt"): mods |= MOD_ALT
                if hb.get("shift"): mods |= 0x0004
                if u.RegisterHotKey(None, i, mods, vk):
                    self.registered.append({"id": i, "vk": vk, "mode": hb.get("mode")})
                else:
                    failed.append(A.hotkey_desc(hb))
            if self.registered:
                QApplication.instance().installNativeEventFilter(self)
                self.ok = True
            self.err = ("被占用/无效: " + ", ".join(failed)) if failed else ""
            A.log().info("热键注册 ok=%s registered=%s err=%s",
                         self.ok, self.registered, self.err)
        except Exception as e:
            self.err = str(e)
            A.log().exception("热键注册失败")
        return self.ok

    def uninstall(self):
        try:
            u = ctypes.windll.user32
            for r in self.registered:
                u.UnregisterHotKey(None, r["id"])
        except Exception:
            pass
        self.registered = []

    def nativeEventFilter(self, etype, message):
        try:
            if etype == b"windows_generic_MSG":
                m = ctypes.cast(int(message), ctypes.POINTER(MSG)).contents
                if m.message == WM_HOTKEY:
                    self.fired.emit(int(m.wParam))
        except Exception:
            A.log().exception("nativeEventFilter 异常")
        return False, 0


# ---------------- 控件 ----------------
class Ring(QWidget):
    def __init__(self, label, parent=None):
        super().__init__(parent)
        self.label, self.value, self._shown = label, None, 0.0
        self.setFixedSize(96, 118)
        self.anim = QTimer(self); self.anim.timeout.connect(self._tick); self.anim.start(28)

    def set_value(self, v): self.value = v

    def _tick(self):
        tgt = float(self.value or 0)
        if abs(self._shown - tgt) > 0.5:
            self._shown += (tgt - self._shown) * 0.18
            self.update()

    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        cx, cy, r = self.width()/2, 46, 36
        p.setPen(mkpen("#2E323A", 8))
        p.drawArc(int(cx-r), int(cy-r), int(2*r), int(2*r), 0, 360*16)
        if self.value is not None:
            frac = max(0.0, min(1.0, self._shown/100.0))
            col = ACCENT if self._shown < 20 else (OK if self._shown > 60 else WARN)
            p.setPen(mkpen(col, 8))
            p.drawArc(int(cx-r), int(cy-r), int(2*r), int(2*r), 90*16, -int(360*16*frac))
        f = QFont(); f.setPointSize(15); f.setBold(True); p.setFont(f)
        p.setPen(QColor(TEXT))
        p.drawText(self.rect().adjusted(0, 0, 0, -22), Qt.AlignCenter,
                   "--" if self.value is None else str(int(round(self._shown))))
        p.setPen(QColor(MUTED))
        f2 = QFont(); f2.setPointSize(9); p.setFont(f2)
        p.drawText(self.rect().adjusted(0, 34, 0, 0), Qt.AlignHCenter | Qt.AlignTop, self.label)


class ModeButton(QPushButton):
    def __init__(self, value, label, parent=None):
        super().__init__(parent)
        self.value, self.label = value, label
        self.setCheckable(True); self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(92); self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._restyle()

    def _restyle(self):
        if self.isChecked():
            self.setStyleSheet("QPushButton{border:2px solid %s;border-radius:8px;background:#2E1A16;}" % ACCENT)
        else:
            self.setStyleSheet("QPushButton{border:1px solid %s;border-radius:8px;background:%s;}"
                               "QPushButton:hover{border:1px solid #4A4F5A;background:#31353E;}" % (BORDER, PANEL_HI))
        self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        col = QColor(ACCENT) if self.isChecked() else QColor(TEXT)
        cx, cy = self.width()/2, 32
        p.setPen(mkpen(col, 2))
        p.drawArc(int(cx-10), int(cy-10), 20, 20, 0, 360*16)
        p.drawArc(int(cx-18), int(cy-18), 36, 36, 300*16, 120*16)
        p.drawArc(int(cx-18), int(cy-18), 36, 36, 60*16, 120*16)
        f = QFont(); f.setPointSize(10); f.setBold(True); p.setFont(f)
        p.setPen(col)
        p.drawText(self.rect().adjusted(0, 52, 0, 0), Qt.AlignHCenter | Qt.AlignTop, self.label)


class Chip(QPushButton):
    def __init__(self, value, label, parent=None):
        super().__init__(label, parent)
        self.value = value
        self.setCheckable(True); self.setCursor(Qt.PointingHandCursor); self.setFixedHeight(30)
        self._restyle()

    def _restyle(self):
        if self.isChecked():
            self.setStyleSheet("QPushButton{background:%s;color:#fff;border:none;border-radius:15px;padding:0 14px;font-size:12px;font-weight:600;}"
                               "QPushButton:hover{background:#FF5137;}" % ACCENT)
        else:
            self.setStyleSheet("QPushButton{background:%s;color:%s;border:1px solid %s;border-radius:15px;padding:0 14px;font-size:12px;}"
                               "QPushButton:hover{background:#31353E;color:%s;}" % (PANEL_HI, MUTED, BORDER, TEXT))


class Card(QFrame):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(16, 12, 16, 16); self.v.setSpacing(12)
        row = QHBoxLayout()
        t = QLabel(title); t.setObjectName("panelTitle"); row.addWidget(t)
        row.addStretch(1)
        self.head = row
        self.v.addLayout(row)

    def add_head_widget(self, w):
        self.head.addWidget(w)


# ---------------- 工作线程 ----------------
class Worker(QObject):
    battery = Signal(object)
    mode = Signal(object)
    connect = Signal(object)
    sound = Signal(object)
    log = Signal(str)
    status = Signal(object)
    stats = Signal(object)
    probe_row = Signal(object)
    probe_done = Signal(object)

    def __init__(self):
        super().__init__()
        self.dev = None
        self.stop = False
        self.wake = threading.Event()
        self._last_batt = self._last_mode = self._last_conn = None
        self.reconnect_requested = False
        self.busy_probe = False
        self._last_sound = None
        self._last_hb = 0.0
        self.hb_sec = HEARTBEAT_SEC
        self._next_batt = 0.0
        self._next_mode = 0.0
        self._next_conn = 0.0
        self._next_sound = 0.0
        self._quiet_until = 0.0

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def shutdown(self, timeout=4.0):
        """先停循环并等它退出, 再拆设备 —— 避免主线程拆的时候 worker 正在读。"""
        self.stop = True
        self.wake.set()
        t = getattr(self, "_thread", None)
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)
        self._teardown()

    def _teardown(self):
        try:
            if self.dev: self.dev.close()
        except Exception:
            A.log().exception("关闭设备失败")
        self.dev = None

    def _run(self):
        backoff = 2.0
        while not self.stop:
            if self.reconnect_requested:
                self.reconnect_requested = False
                self._teardown()
                self.log.emit("正在重新连接…")
            try:
                if self.dev is None:
                    self.status.emit({"state": "connecting"})
                    self.log.emit("正在探测 SPP 串口…")
                    dev = A.AeroQ32()
                    port = dev.open()
                    self.log.emit("已打开 %s%s" % (port, "  MAC " + dev.mac if dev.mac else ""))
                    if dev.handshake() is None:
                        raise IOError("握手无应答, 可能不是本耳机")
                    self.dev = dev
                    self.status.emit({"state": "connected", "port": port, "mac": dev.mac})
                    self.log.emit("握手完成")
                    A.log().info("已连接 %s MAC=%s", port, dev.mac)
                    self._last_batt = self._last_mode = self._last_conn = None
                    self._last_sound = None
                    self._last_hb = 0.0
                    # 刚连上先全量取一次, 之后按各自节奏轮询
                    self._next_batt = self._next_mode = 0.0
                    self._next_conn = self._next_sound = 0.0
                    self._quiet_until = time.time() + 0.5
                    backoff = 2.0

                if not self.dev.healthy(6.0):
                    raise IOError("链路假死 (6 秒无有效帧)")

                # ── 分频轮询 ──────────────────────────────────────────────
                # 每个查询按自己的节奏走, 而不是每轮全问一遍。
                # 耳机 MCU 要同时处理音频链路和这些查询, 问得太勤会挤占它,
                # 实测表现为"命令发出去了, 设备却不采纳"。
                # 用户发命令后还有一段静默窗口, 期间完全不打扰设备。
                if not self.busy_probe and time.time() >= self._quiet_until:
                    now = time.time()
                    if now >= self._next_batt:
                        self._next_batt = now + POLL_BATTERY
                        b = self.dev.battery()
                        if b:
                            self.battery.emit(b)
                            if b != self._last_batt:
                                self.log.emit("RX 电量   左%d%%   右%d%%   盒%s" %
                                              (b["left"], b["right"],
                                               "%d%%" % b["case"] if b.get("case") is not None else "--"))
                                # 原始报文只进文件日志, 用于排查数值异常跳动
                                A.log().info("电量变化 -> 左%s 右%s 盒%s | payload=[%s] len=%s",
                                             b.get("left"), b.get("right"), b.get("case"),
                                             b.get("raw"), b.get("len"))
                                self._last_batt = b
                    if now >= self._next_mode:
                        self._next_mode = now + POLL_MODE
                        m = self.dev.get_listen_mode()
                        if m is not None:
                            self.mode.emit(m)
                            if m != self._last_mode:
                                self.log.emit("RX 降噪模式   %s (%d)" % (A.LISTEN_MODES.get(m, "?"), m))
                                self._last_mode = m
                    if now >= self._next_conn:
                        self._next_conn = now + POLL_CONN
                        c = self.dev.get_connect_option()
                        if c is not None:
                            self.connect.emit(c)
                            if c != self._last_conn:
                                self.log.emit("RX 连接模式   %s (%d)" % (A.CONNECT_TYPES.get(c, "?"), c))
                                self._last_conn = c
                    if now >= self._next_sound:
                        self._next_sound = now + POLL_SOUND
                        snd = self.dev.get_preset_sound()
                        if snd is not None:
                            self.sound.emit(snd)
                            if snd != self._last_sound:
                                self.log.emit("RX 风格音效   %s (%d)"
                                              % (A.PRESET_SOUND_NAMES.get(snd, "预设"), snd))
                                self._last_sound = snd
                    # 心跳: 数值没变化时也定期报一次, 证明链路是活的
                    if now - self._last_hb >= self.hb_sec:
                        self._last_hb = now
                        st2 = self.dev.stats
                        bb = self._last_batt or {}
                        self.log.emit("♥ 链路正常 · 电量 %s/%s/%s · 降噪 %s · 音效 %s · 帧%s 丢弃%s 超时%s"
                                      % (bb.get("left", "--"), bb.get("case", "--"), bb.get("right", "--"),
                                         A.LISTEN_MODES.get(self._last_mode, "--"),
                                         self._last_sound if self._last_sound is not None else "--",
                                         st2.get("frames", 0), st2.get("dropped", 0), st2.get("timeouts", 0)))
                    self.stats.emit(dict(self.dev.stats))
                self.wake.wait(1.0); self.wake.clear()
                continue

            except A.PortBusy as ex:
                self.log.emit("⚠ %s" % ex)
                self.log.emit("   另一个实例可能正在运行; 也可能是别的程序占用了该串口。")
                A.log().warning("%s", ex)
                self.status.emit({"state": "busy"})
                self._teardown()
            except A.DeviceNotFound as ex:
                self.log.emit("⚠ %s" % ex)
                self.log.emit("   排查步骤: 1) 确认耳机已开机并已与电脑配对; "
                              "2) 运行 aero_cli.py doctor 查看环境; "
                              "3) 确认没有第二个实例在运行。")
                A.log().warning("未找到设备: %s", ex)
                self.status.emit({"state": "notfound"})
                self._teardown()
            except Exception as ex:
                self.log.emit("⚠ %s" % ex)
                A.log().exception("连接循环异常")
                self.status.emit({"state": "error", "msg": str(ex)})
                self._teardown()

            self.wake.wait(backoff); self.wake.clear()
            backoff = min(backoff * 1.6, 15.0)

    def request_reconnect(self, why=""):
        if why: self.log.emit(why)
        self.reconnect_requested = True
        self.wake.set()

    def set_mode(self, v, tag="点击"):
        try:
            if not self.dev:
                self.log.emit("尚未连接，无法切换"); return
            # 发命令前先安静下来: 期间不再轮询, 把链路让给这条命令
            self._quiet_until = time.time() + QUIET_AFTER_CMD
            self.log.emit("TX 降噪模式 -> %s  (%s)" % (A.LISTEN_MODES.get(v, v), tag))
            got = self.dev.set_listen_mode(v)
            if got is None:
                self.log.emit("⚠ 设备未确认，稍后自动校正")
            else:
                if got != v:
                    # 设备未采纳。如实告知, 不假装成功; 并给出最可能的原因,
                    # 否则用户只会看到"没反应", 无从判断是软件问题还是耳机状态问题。
                    self.log.emit("  设备未采纳，当前为 %s (%d)"
                                  % (A.LISTEN_MODES.get(got, "?"), got))
                    self.log.emit("  常见原因: 耳机未佩戴或在充电盒中 / 电量偏低 / "
                                  "与手机同时连接时手机端优先 / "
                                  "「风噪降低」这类子模式需要降噪已开启。")
                self.mode.emit(got)
        except Exception as ex:
            self.log.emit("⚠ 写入失败: %s" % ex); A.log().exception("设置降噪失败")

    def set_preset_sound(self, v):
        """设置风格音效(声效)。与降噪互不影响。"""
        try:
            if not self.dev:
                self.log.emit("尚未连接，无法切换"); return
            self._quiet_until = time.time() + QUIET_AFTER_CMD
            self.log.emit("TX 风格音效 -> %s (%d)" % (A.PRESET_SOUND_NAMES.get(v, "预设"), v))
            got = self.dev.set_preset_sound(v)
            if got is None:
                self.log.emit("⚠ 设备未确认")
            elif got != v:
                self.log.emit("  设备未采纳，当前为 %s" % got)
            else:
                self.sound.emit(got)
        except Exception as ex:
            self.log.emit("⚠ 写入失败: %s" % ex); A.log().exception("设置风格音效失败")

    def set_connect_option(self, v):
        try:
            if not self.dev:
                self.log.emit("尚未连接，无法切换"); return
            self._quiet_until = time.time() + QUIET_AFTER_CMD
            self.log.emit("TX 连接模式 -> %s" % A.CONNECT_TYPES.get(v, v))
            self.log.emit("   (切换会重建蓝牙链路，控制通道将中断约 10 秒)")
            self.dev.set_connect_option(v)
        except Exception as ex:
            self.log.emit("⚠ %s" % ex); A.log().exception("设置连接模式失败")
        self.request_reconnect("链路重建中…")

    def run_probe(self):
        if not self.dev or self.busy_probe:
            self.log.emit("尚未连接或正在自检"); return
        self.busy_probe = True
        try:
            self.log.emit("=== 设备自检开始 (只发送只读命令) ===")
            rows = A.probe_capabilities(self.dev, on_result=self.probe_row.emit, wait=1.0)
            sup = [r for r in rows if r[2]]
            self.log.emit("=== 自检完成: 支持 %d / %d 项 ===" % (len(sup), len(rows)))
            self.probe_done.emit(rows)
            A.log().info("自检完成, 支持 %d/%d", len(sup), len(rows))
        except Exception as ex:
            self.log.emit("⚠ 自检失败: %s" % ex); A.log().exception("自检失败")
        finally:
            self.busy_probe = False

    def send_raw(self, cmd, payload, unlocked):
        try:
            if not self.dev:
                self.log.emit("尚未连接"); return
            ok, why, resp = self.dev.send_raw(cmd, payload, unlocked=unlocked)
            tag = "✓" if ok else "⛔"
            self.log.emit("%s 原始命令 0x%02X [%s]  %s" % (tag, cmd, payload.hex(" "), why))
            if ok:
                self.log.emit("   回读: %s" % (resp.hex(" ") if resp else "(无应答)"))
        except Exception as ex:
            self.log.emit("⚠ 发送失败: %s" % ex); A.log().exception("原始命令失败")


# ---------------- 热键自定义对话框 ----------------
class KeyCaptureButton(QPushButton):
    captured = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.capturing = False
        self.setFocusPolicy(Qt.StrongFocus)
        self.clicked.connect(self.start_capture)

    def start_capture(self):
        self.capturing = True
        self.setText("按下组合键… (Esc 取消)")
        self.setFocus()

    def keyPressEvent(self, e):
        if not self.capturing:
            super().keyPressEvent(e); return
        k = e.key()
        if k in (Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift, Qt.Key_Meta):
            return                      # 只按修饰键不算
        if k == Qt.Key_Escape:
            self.capturing = False; self.setText("(未设置)"); return
        mods = e.modifiers()
        name = QKeySequence(k).toString()
        if not name:
            return
        if not (mods & (Qt.ControlModifier | Qt.AltModifier | Qt.ShiftModifier)):
            self.setText("需要至少一个修饰键")
            self.capturing = False
            return
        self.capturing = False
        self.captured.emit({"ctrl": bool(mods & Qt.ControlModifier),
                            "alt": bool(mods & Qt.AltModifier),
                            "shift": bool(mods & Qt.ShiftModifier),
                            "key": name})


class HotkeyDialog(QDialog):
    """全局热键自定义: 动作(降噪模式) + 组合键"""

    def __init__(self, bindings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("自定义全局热键")
        self.setStyleSheet(QSS)
        self.rows = []
        v = QVBoxLayout(self); v.setSpacing(10)
        tip = QLabel("点击右侧按钮后，按下你想要的组合键（至少含 Ctrl / Alt / Shift 之一）。")
        tip.setWordWrap(True); tip.setStyleSheet("color:%s;font-size:11px;" % MUTED)
        v.addWidget(tip)

        # 固定 5 行: 否则用户永远无法新增热键, 且清空一条保存后会永久少一条
        bl = list(bindings)[:5]
        while len(bl) < 5:
            bl.append({})
        for b in bl:
            r = QHBoxLayout(); r.setSpacing(6)
            combo = QComboBox()
            for val in (1, 0, 3, 2, 7, 4, 5, 6):
                combo.addItem(A.LISTEN_MODES[val], val)
            idx = combo.findData(b.get("mode", 1))
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            btn = KeyCaptureButton()
            btn.setText(A.hotkey_desc(b) if b else "(未设置)")
            btn.setMinimumWidth(150)
            cur = {"h": dict(b)}
            def mk(btn, cur):
                def on_cap(h):
                    cur["h"] = h
                    btn.setText(A.hotkey_desc(h))
                return on_cap
            btn.captured.connect(mk(btn, cur))
            clr = QPushButton("清除"); clr.setObjectName("ghost"); clr.setFixedWidth(56)
            def mkclr(btn, cur):
                def do():
                    cur["h"] = {}
                    btn.setText("(未设置)")
                return do
            clr.clicked.connect(mkclr(btn, cur))
            r.addWidget(combo); r.addWidget(btn); r.addWidget(clr)
            v.addLayout(r)
            self.rows.append((combo, cur))

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel,
                              parent=self)
        bb.button(QDialogButtonBox.Save).setText("保存")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def result_bindings(self):
        out = []
        for combo, cur in self.rows:
            h = cur.get("h") or {}
            if not h.get("key"):
                continue
            b = dict(h); b["mode"] = combo.currentData()
            out.append(b)
        return out


# ---------------- 主窗口 ----------------
class Window(QWidget):
    def __init__(self):
        super().__init__()
        self._dev_name = None
        self.setWindowTitle(APP_TITLE)
        # 演示模式(AERO_NODEV)只影响界面, 绝不写进生产日志,
        # 否则日志里会混入假数据, 排查真实问题时产生误导。
        self._demo = os.environ.get("AERO_NODEV") == "1"
        self.setMinimumSize(760, 540)
        self.setStyleSheet(QSS)
        self._build()

        scr = QApplication.primaryScreen().availableGeometry()
        self.resize(max(760, min(1000, scr.width() - 80)),
                    max(540, min(740, scr.height() - 80)))

        self.w = Worker()
        self.w.battery.connect(self.on_battery)
        self.w.mode.connect(self.on_mode)
        self.w.connect.connect(self.on_connect)
        self.w.log.connect(self.on_log)
        self.w.status.connect(self.on_status)
        self.w.stats.connect(self.on_stats)
        self.w.probe_row.connect(self.on_probe_row)
        self.w.sound.connect(self.on_sound)
        self.w.probe_done.connect(self.on_probe_done)

        self._low_warned = False
        self._tray_hinted = False
        self._last_data = 0.0          # 有真实数据前不谎报"刚刚更新"
        self._last_probe = []
        self.cfg = A.load_settings()
        self.LOW = int(self.cfg.get("low_battery", LOW_BATTERY))
        self.w.hb_sec = int(self.cfg.get("heartbeat_sec", HEARTBEAT_SEC))
        self._build_tray()

        self.hk = HotkeyManager(self)
        self.hk.fired.connect(self.on_hotkey)
        self.hk.install(self.cfg.get("hotkeys") or [])
        if self.hk.err:
            self.on_log("⚠ 热键: %s" % self.hk.err)

        # 「最后更新」指示: 每秒刷新, 直观证明链路活着
        self.tick = QTimer(self)
        self.tick.timeout.connect(self.on_tick)
        self.tick.start(1000)

        if os.environ.get("AERO_NODEV") == "1":
            # 演示/截图模式: 不连设备, 用假设备名, 避免把真实型号写死进代码
            self.on_status({"state": "connected", "port": "COM1", "mac": None})
            self._dev_name = "Demo Earbuds"
            self.lblDev.setText(self._dev_name)
            self.setWindowTitle("%s · %s" % (self._dev_name, APP_TITLE))
            self.tray.setToolTip(self._dev_name)
            self.on_battery({"left": 100, "right": 100, "case": 95})
            self.on_mode(1); self.on_connect(0)
            for s in ("已打开 COM1", "握手完成", "RX 电量   左100%   右100%   盒95%"):
                self.on_log(s)
        else:
            self.w.start()

    # ---- 构建 ----
    def _build(self):
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        page = QWidget(); scroll.setWidget(page)
        root = QVBoxLayout(page); root.setContentsMargins(16, 16, 16, 16); root.setSpacing(12)

        top = QHBoxLayout()
        self.lblDev = QLabel(APP_TITLE); self.lblDev.setObjectName("big")
        top.addWidget(self.lblDev)
        top.addSpacing(8)
        self.dot = QLabel("●"); self.dot.setStyleSheet("color:%s;font-size:16px;" % WARN); top.addWidget(self.dot)
        self.st = QLabel("连接中…"); self.st.setObjectName("muted"); top.addWidget(self.st)
        top.addStretch(1)
        self.stat = QLabel(""); self.stat.setStyleSheet("color:%s;font-size:11px;" % MUTED); top.addWidget(self.stat)
        top.addSpacing(8)
        self.uptodate = QLabel(""); self.uptodate.setStyleSheet("color:%s;font-size:11px;" % MUTED)
        top.addWidget(self.uptodate)
        top.addSpacing(8)
        self.btnHk = QPushButton("热键设置"); self.btnHk.setObjectName("ghost")
        self.btnHk.clicked.connect(self.open_hotkey_dialog)
        top.addWidget(self.btnHk)
        self.btnProbe = QPushButton("设备自检"); self.btnProbe.setObjectName("ghost")
        self.btnProbe.setToolTip("只发送只读查询命令，探测本机支持哪些功能")
        self.btnProbe.clicked.connect(lambda: threading.Thread(target=self.w.run_probe, daemon=True).start())
        top.addWidget(self.btnProbe)
        self.btnRe = QPushButton("重新连接"); self.btnRe.setObjectName("ghost")
        self.btnRe.clicked.connect(lambda: self.w.reconnect()); top.addWidget(self.btnRe)
        root.addLayout(top)

        c0 = Card("电  量")
        rings = QHBoxLayout(); rings.setSpacing(36); rings.addStretch(1)
        self.rL = Ring("左耳"); self.rC = Ring("充电盒"); self.rR = Ring("右耳")
        for r in (self.rL, self.rC, self.rR): rings.addWidget(r)
        rings.addStretch(1); c0.v.addLayout(rings); root.addWidget(c0)

        c1 = Card("降噪模式")
        row = QHBoxLayout(); row.setSpacing(10); self.big = []
        for val, lab in ((0, "关闭"), (1, "强降噪"), (3, "通透")):
            b = ModeButton(val, lab)
            b.clicked.connect(lambda _=False, v=val: self.pick(v))
            self.big.append(b); row.addWidget(b)
        c1.v.addLayout(row)
        sub = QLabel("更多模式"); sub.setObjectName("panelTitle"); c1.v.addWidget(sub)
        chips = QHBoxLayout(); chips.setSpacing(8); self.chips = []
        for val in (2, 7, 4, 5, 6):
            ch = Chip(val, A.LISTEN_MODES[val])
            ch.clicked.connect(lambda _=False, v=val: self.pick(v))
            self.chips.append(ch); chips.addWidget(ch)
        chips.addStretch(1); c1.v.addLayout(chips)
        hk = QLabel("全局热键：Ctrl+Alt+1 强降噪 · Ctrl+Alt+2 关闭 · Ctrl+Alt+3 通透")
        hk.setStyleSheet("color:%s;font-size:11px;" % MUTED); c1.v.addWidget(hk)
        root.addWidget(c1)

        c15 = Card("蓝牙连接模式")
        crow = QHBoxLayout(); crow.setSpacing(8); self.conn = []
        for val in (0, 1, 2):
            ch = Chip(val, A.CONNECT_TYPES[val])
            ch.clicked.connect(lambda _=False, v=val: self.pick_conn(v))
            self.conn.append(ch); crow.addWidget(ch)
        crow.addStretch(1); c15.v.addLayout(crow)
        hint = QLabel("切换到 LDAC 会重建蓝牙链路，控制通道中断约 10 秒后自动恢复。"
                      "注意：Windows 不支持 LDAC，本机仍会回落到 SBC/AAC；"
                      "该设置对之后连接 LDAC 音源（如手机）才生效。")
        hint.setWordWrap(True); hint.setStyleSheet("color:%s;font-size:11px;" % MUTED)
        c15.v.addWidget(hint); root.addWidget(c15)

        # 风格音效 (声效) —— 与降噪是两套独立的东西
        c16 = Card("风格音效")
        srow = QHBoxLayout(); srow.setSpacing(8)
        srow.addWidget(self._lab("预设"))
        self.soundCombo = QComboBox()
        for v in range(0, 13):
            nm = A.PRESET_SOUND_NAMES.get(v)
            self.soundCombo.addItem(("%d   %s" % (v, nm)) if nm else ("%d   预设" % v), v)
        self.soundCombo.setFixedWidth(190)
        self.soundCombo.activated.connect(self.on_sound_pick)
        srow.addWidget(self.soundCombo)
        srow.addStretch(1)
        c16.v.addLayout(srow)
        shit = QLabel("声效 = 音色曲线（官方「风格音效」），降噪 = 环境声消除，两者独立、互不影响。"
                      "带名称的几项取自官方代码 getStyleCmd()，其余只有编号，未做臆测命名。")
        shit.setWordWrap(True); shit.setStyleSheet("color:%s;font-size:11px;" % MUTED)
        c16.v.addWidget(shit); root.addWidget(c16)

        # 原始命令控制台
        c3 = Card("原始命令 (调试)")
        c3.add_head_widget(self._mk_unlock())
        r1 = QHBoxLayout(); r1.setSpacing(8)
        r1.addWidget(self._lab("CMD"))
        self.inCmd = QLineEdit(); self.inCmd.setPlaceholderText("如 4E  (十六进制)")
        self.inCmd.setFixedWidth(90); r1.addWidget(self.inCmd)
        r1.addWidget(self._lab("数据"))
        self.inData = QLineEdit(); self.inData.setPlaceholderText("如 01 02  (可留空)")
        r1.addWidget(self.inData, 1)
        self.btnSend = QPushButton("发送"); self.btnSend.setObjectName("ghost")
        self.btnSend.clicked.connect(self.on_send_raw); r1.addWidget(self.btnSend)
        c3.v.addLayout(r1)
        warn = QLabel("安全闸：OTA 刷写(0x71/0x72/0x73)与不可逆删除(0x49)永久禁止，"
                      "解锁也不放行；其它未验证命令默认拦截。")
        warn.setWordWrap(True); warn.setStyleSheet("color:%s;font-size:11px;" % MUTED)
        c3.v.addWidget(warn); root.addWidget(c3)

        c2 = Card("命令监视")
        c2.add_head_widget(self._mk_logbtn())
        self.logBox = QPlainTextEdit(); self.logBox.setReadOnly(True)
        self.logBox.setMinimumHeight(120)
        c2.v.addWidget(self.logBox); root.addWidget(c2)
        root.addStretch(1)

    def _lab(self, s):
        l = QLabel(s); l.setObjectName("muted"); return l

    def _mk_unlock(self):
        self.chkUnlock = QCheckBox("解锁未验证命令")
        self.chkUnlock.setToolTip("OTA 与删除类命令即使解锁也仍然禁止")
        self.chkUnlock.stateChanged.connect(self._on_unlock)
        return self.chkUnlock

    def _mk_logbtn(self):
        b = QPushButton("打开日志"); b.setObjectName("ghost")
        b.clicked.connect(self.open_logfile); return b

    # ---- 托盘 ----
    def _build_tray(self):
        icon = QIcon(res_path("icon.ico"))
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(APP_TITLE)
        m = QMenu()
        act_show = QAction("显示窗口", self); act_show.triggered.connect(self.show_normal)
        m.addAction(act_show); m.addSeparator()
        self.menu_mode = m.addMenu("降噪模式")
        for val in (1, 0, 3, 2, 7, 4, 5, 6):
            a = QAction(A.LISTEN_MODES[val], self, checkable=True)
            a.triggered.connect(lambda _=False, v=val: self.pick(v, tag="托盘"))
            self.menu_mode.addAction(a)
        self.menu_sound = m.addMenu("风格音效(声效)")
        for v in range(0, 13):
            nm = A.PRESET_SOUND_NAMES.get(v)
            a2 = QAction(("%d  %s" % (v, nm)) if nm else ("%d  预设" % v), self, checkable=True)
            a2.triggered.connect(lambda _=False, vv=v: self.pick_sound(vv))
            self.menu_sound.addAction(a2)
        self.menu_batt = m.addAction("电量 --")
        self.menu_batt.setEnabled(False)
        m.addSeparator()
        a_re = QAction("重新连接", self); a_re.triggered.connect(lambda: self.w.reconnect())
        m.addAction(a_re)
        a_log = QAction("打开日志", self); a_log.triggered.connect(self.open_logfile)
        m.addAction(a_log)
        m.addSeparator()
        a_q = QAction("退出", self); a_q.triggered.connect(self.really_quit)
        m.addAction(a_q)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(
            lambda r: self.show_normal() if r == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

    def show_normal(self):
        self.show(); self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_(); self.activateWindow()

    def open_logfile(self):
        p = A.setup_logging()
        try:
            os.startfile(p)
        except Exception:
            QMessageBox.information(self, "日志位置", p)

    def closeEvent(self, e):
        # 没有系统托盘时不能隐藏, 否则窗口消失且无法恢复 => 隐形僵尸进程
        if self.tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable():
            e.ignore(); self.hide()
            if not self._tray_hinted:
                self._tray_hinted = True
                self.tray.showMessage("仍在后台运行",
                                      "已最小化到托盘。右键托盘图标可切降噪模式；"
                                      "选「退出」才会真正关闭。",
                                      QSystemTrayIcon.Information, 4000)
        else:
            self.really_quit()

    def really_quit(self):
        try:
            self.tick.stop()
            self.hk.uninstall()
            self.tray.hide()
            self.w.shutdown()
        except Exception:
            A.log().exception("退出清理异常")
        A.log().info("应用退出")
        A.close_logging()          # 释放日志文件句柄
        QApplication.quit()

    # ---- 槽 ----
    def on_battery(self, b):
        self.rL.set_value(b.get("left")); self.rR.set_value(b.get("right")); self.rC.set_value(b.get("case"))
        lo = min([v for v in (b.get("left"), b.get("right"), b.get("case")) if v is not None] or [100])
        self.menu_batt.setText("电量  左%s%%  盒%s%%  右%s%%" %
                               (b.get("left"), b.get("case"), b.get("right")))
        self.tray.setToolTip("%s\n电量 左%s%% / 盒%s%% / 右%s%%"
                             % (self._dev_name or APP_TITLE,
                                b.get("left"), b.get("case"), b.get("right")))
        self._last_data = time.time()
        if lo <= self.LOW and not self._low_warned:
            self._low_warned = True
            self.tray.showMessage("电量偏低", "当前最低 %d%%，建议充电" % lo,
                                  QSystemTrayIcon.Warning, 5000)
            self.on_log("⚠ 低电量提醒: %d%%" % lo)
            A.log().warning("低电量 %d%%", lo)
        elif lo > LOW_BATTERY + 5:
            self._low_warned = False

    def on_mode(self, m):
        if m is None: return
        self._last_data = time.time()
        for b in self.big:
            b.blockSignals(True); b.setChecked(b.value == m); b._restyle(); b.blockSignals(False)
        for c in self.chips:
            c.blockSignals(True); c.setChecked(c.value == m); c._restyle(); c.blockSignals(False)
        for a in self.menu_mode.actions():
            a.blockSignals(True)
            a.setChecked(a.text() == A.LISTEN_MODES.get(m))
            a.blockSignals(False)

    def on_connect(self, c):
        if c is None: return
        self._last_data = time.time()
        for ch in self.conn:
            ch.blockSignals(True); ch.setChecked(ch.value == c); ch._restyle(); ch.blockSignals(False)

    def on_probe_done(self, rows):
        sup = [r for r in rows if r[2]]
        self.on_log("自检汇总: 支持 %d 项" % len(sup))
        self._last_probe = rows

    def on_probe_row(self, row):
        cmd, name, ok, ln, hx = row
        self.on_log("  0x%02X %-16s %s len=%-3d %s" %
                    (cmd, name, "有应答" if ok else "  --  ", ln, hx[:48]))

    def on_log(self, s):
        if self._demo:
            s = "[演示] " + s
        self.logBox.appendPlainText(time.strftime("[%H:%M:%S] ") + s)
        doc = self.logBox.document()
        if doc.blockCount() > MAX_LOG_LINES:
            cur = QTextCursor(doc)
            cur.movePosition(QTextCursor.Start)
            cur.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor,
                             doc.blockCount() - MAX_LOG_LINES)
            cur.removeSelectedText(); cur.deleteChar()
        sb = self.logBox.verticalScrollBar(); sb.setValue(sb.maximum())
        if not self._demo:
            A.log().info("[UI] %s", s)

    def on_stats(self, st):
        self.stat.setText("帧 %s · 丢弃 %s · 超时 %s" %
                          (st.get("frames", 0), st.get("dropped", 0), st.get("timeouts", 0)))

    def on_status(self, s):
        state = s.get("state")
        if state == "connected":
            self.dot.setStyleSheet("color:%s;font-size:16px;" % OK)
            self.st.setText("已连接" + ("  ·  " + s["port"] if s.get("port") else ""))
            # 设备名从系统注册表读, 读不到就退回 MAC, 再不济用通用名
            self._dev_name = A.device_name(s.get("mac")) or (s.get("mac") or None)
            shown = self._dev_name or APP_TITLE
            self.lblDev.setText(shown)
            self.setWindowTitle(shown if shown == APP_TITLE
                                else "%s · %s" % (shown, APP_TITLE))
            self.tray.setToolTip(shown)
        elif state == "busy":
            self.dot.setStyleSheet("color:%s;font-size:16px;" % ACCENT); self.st.setText("串口被占用")
        elif state == "connecting":
            self.dot.setStyleSheet("color:%s;font-size:16px;" % WARN); self.st.setText("搜索中…")
        elif state == "notfound":
            self.dot.setStyleSheet("color:%s;font-size:16px;" % ACCENT)
            self.st.setText("未找到耳机，重试中…")
        else:
            self.dot.setStyleSheet("color:%s;font-size:16px;" % ACCENT); self.st.setText("未连接，重试中…")

    def on_hotkey(self, hid):
        for r in self.hk.registered:
            if r["id"] == hid:
                self.pick(r["mode"], tag="热键")
                return

    def _on_unlock(self, st):
        un = bool(st)
        self.on_log("危险模式%s" % ("已解锁 (OTA/删除类仍禁止)" if un else "已锁定"))
        A.log().warning("危险模式 %s", "解锁" if un else "锁定")

    def on_send_raw(self):
        try:
            cmd = int(self.inCmd.text().strip().replace("0x", ""), 16)
        except Exception:
            self.on_log("⚠ CMD 需为十六进制, 如 4E"); return
        if not (0 <= cmd <= 0xFF):
            self.on_log("⚠ CMD 需在 00–FF 之间 (收到 0x%X)" % cmd); return
        try:
            data = bytes.fromhex(self.inData.text().strip().replace(" ", ""))
        except Exception:
            self.on_log("⚠ 数据需为十六进制, 如 0102"); return
        unlocked = self.chkUnlock.isChecked()
        threading.Thread(target=self.w.send_raw, args=(cmd, data, unlocked), daemon=True).start()

    def on_tick(self):
        """每秒刷新『最后更新』, 让链路是否活着一目了然"""
        age = time.time() - self._last_data
        if age < 3:
            self.uptodate.setText("● 数据 %d 秒前" % int(age))
            self.uptodate.setStyleSheet("color:%s;font-size:11px;" % OK)
        elif age < 10:
            self.uptodate.setText("● 数据 %d 秒前" % int(age))
            self.uptodate.setStyleSheet("color:%s;font-size:11px;" % WARN)
        else:
            txt = "● 已 %d 秒无数据" % int(age) if age < 3600 else "● 长时间无数据"
            self.uptodate.setText(txt)
            self.uptodate.setStyleSheet("color:%s;font-size:11px;" % ACCENT)

    def on_sound(self, s):
        if s is None: return
        self._last_data = time.time()
        i = self.soundCombo.findData(s)
        if i >= 0:
            self.soundCombo.blockSignals(True)
            self.soundCombo.setCurrentIndex(i)
            self.soundCombo.blockSignals(False)
        for a in self.menu_sound.actions():
            a.blockSignals(True)
            a.setChecked(a.text().startswith("%d " % s))
            a.blockSignals(False)

    def on_sound_pick(self, idx):
        v = self.soundCombo.itemData(idx)
        if v is not None:
            threading.Thread(target=self.w.set_preset_sound, args=(v,), daemon=True).start()

    def pick_sound(self, v):
        self.on_sound(v)
        threading.Thread(target=self.w.set_preset_sound, args=(v,), daemon=True).start()

    def open_hotkey_dialog(self):
        dlg = HotkeyDialog(self.cfg.get("hotkeys") or [], self)
        if dlg.exec() != QDialog.Accepted:
            return
        binds = dlg.result_bindings()
        self.cfg["hotkeys"] = binds
        if A.save_settings(self.cfg):
            self.on_log("热键已保存到 %s" % A.config_path())
        self.hk.install(binds)
        if self.hk.ok:
            self.on_log("热键生效: " + " · ".join(A.hotkey_desc(b) for b in binds))
        else:
            self.on_log("⚠ 热键未生效: %s" % (self.hk.err or "未知原因"))

    def pick(self, v, tag="点击"):
        self.on_mode(v)
        threading.Thread(target=self.w.set_mode, args=(v, tag), daemon=True).start()

    def pick_conn(self, v):
        self.on_connect(v)
        threading.Thread(target=self.w.set_connect_option, args=(v,), daemon=True).start()


# 按优先级尝试的字体: 先中文, 再日韩, 最后西文兜底。
# 不假设用户一定在中文 Windows 上。
FONT_CANDIDATES = (
    "msyh.ttc", "msyhbd.ttc",          # 微软雅黑 (简体中文)
    "msjh.ttc",                        # 微软正黑 (繁体中文)
    "YuGothM.ttc", "YuGothR.ttc",      # 游ゴシック (日文)
    "malgun.ttf",                      # 맑은 고딕 (韩文)
    "segoeui.ttf", "seguisb.ttf",      # Segoe UI (西文兜底)
)


def load_fonts():
    """加载系统可用字体。

    目的: 在非中文 Windows 上也能正常显示, 而不是硬编码单一字体后花屏。
    返回成功加载的字体文件名列表。
    """
    fontdir = os.path.join(os.environ.get("WINDIR", "C:") + os.sep, "Fonts")
    loaded = []
    for name in FONT_CANDIDATES:
        p = os.path.join(fontdir, name)
        try:
            if os.path.exists(p):
                if QFontDatabase.addApplicationFont(p) >= 0:
                    loaded.append(name)
        except Exception:
            pass
    try:
        A.log().info("已加载字体: %s", loaded or "(使用系统默认)")
    except Exception:
        pass
    return loaded


if __name__ == "__main__":
    A.setup_logging()
    A.log().info("=== 应用启动 ===")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)      # 关窗口不退出, 走托盘
    load_fonts()
    if not acquire_single_instance():
        A.log().warning("检测到已有实例, 本次启动退出")
        QMessageBox.information(
            None, "已在运行",
            "%s 已经在运行了。\n\n" % APP_TITLE +
            "同时开两个实例会互相抢占蓝牙串口和全局热键，"
            "导致其中一个连不上耳机。\n\n"
            "请从系统托盘图标（右下角）操作：右键可直接切换模式，"
            "选「显示窗口」可打开界面，选「退出」才是真正关闭。")
        sys.exit(0)
    win = Window()
    win.show()
    if "--shot" in sys.argv:
        path = sys.argv[sys.argv.index("--shot") + 1]
        QTimer.singleShot(1200, lambda: (win.grab().save(path), app.quit()))
    sys.exit(app.exec())
