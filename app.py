# -*- coding: utf-8 -*-
"""UIA XPath 测试工具 GUI（PySide6）。

功能：
- 顶层窗口选择 + 后台线程建快照
- 控件树（懒加载）+ 上方过滤框（按名称/类型筛选，保留父链）+ 可复制属性面板
- XPath 编辑器：匹配数/耗时/结果列表，点击结果高亮并滚动定位树节点
- 工具栏「拾取」+ 左 Ctrl 锁定：自动捕获鼠标下窗口、定位树节点、回填路径、闪烁高亮
- 候选 XPath 面板：点击即回填并执行
- 高亮用 PySide6 透明置顶窗实现（替代 tkinter 版）

冒烟：python -X utf8 app.py --smoke  （自动快照首个窗口、跑一次查询、截图退出）
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes

import uiautomation as auto
from PySide6.QtCore import Qt, QObject, QSettings, QStringListModel, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QCompleter, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QStatusBar, QTabWidget, QTableWidget,
    QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from uiaxpath import (
    XPathSyntaxError, find_all, generate, locate_in_snapshot,
    node_path, snapshot,
)
from uiaxpath.snapshot import UiaNode, short_name

ROLE_NODE = Qt.ItemDataRole.UserRole
ROLE_PATH_SHOWN = int(Qt.ItemDataRole.UserRole) + 1   # 结果行是否已算出完整路径
# 软件图标：以脚本所在目录为基准，从任何工作目录启动都能找到
_APP_ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")
# 结果列表的格式说明：和查询统计合并成一行，少占一行垂直空间
_RESULT_HINT = "每行：控件；选中后显示完整路径"
# 大结果集截断：一次性往 QListWidget 塞几千行会很卡，超出的只统计不展示
_RESULT_LIMIT = 100
_INFO_STYLE = "color: gray; font-size: 11px;"
# 语法错误：输入框描红，而不是只在下面灰字行写一句
_EDIT_ERROR_STYLE = "QLineEdit { border: 1px solid #d93025; border-radius: 2px; }"
_INFO_ERROR_STYLE = "color: #d93025; font-size: 11px;"

# 控件类型小色块：一眼区分 Window/Button/Edit 等，比纯文字快
_TYPE_COLORS = {
    "WindowControl": "#4A90D9",
    "PaneControl": "#A0AAB4",
    "ButtonControl": "#E8833A",
    "EditControl": "#3FA45B",
    "DocumentControl": "#3FA45B",
    "TextControl": "#8E8E93",
    "ImageControl": "#B07CC6",
    "ListControl": "#D9A441",
    "ListItemControl": "#D9A441",
    "TabControl": "#5AA9D6",
    "TabItemControl": "#5AA9D6",
    "ToolBarControl": "#7F8C8D",
    "MenuControl": "#7F8C8D",
}
_DEFAULT_TYPE_COLOR = "#C3C9D0"
_icon_cache: dict[str, QIcon] = {}


def _dot_icon(color: str) -> QIcon:
    """按颜色生成（并缓存）圆角色块图标。"""
    icon = _icon_cache.get(color)
    if icon is None:
        pix = QPixmap(10, 10)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(1, 1, 8, 8, 3, 3)
        p.end()
        icon = _icon_cache[color] = QIcon(pix)
    return icon


def _type_icon(control_type: str) -> QIcon:
    return _dot_icon(_TYPE_COLORS.get(control_type, _DEFAULT_TYPE_COLOR))


# ---------------- Win32 ----------------

_user32 = ctypes.windll.user32
_dwmapi = ctypes.windll.dwmapi
_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_GW_OWNER = 4
_DWMWA_CLOAKED = 14
_SELF_PID = os.getpid()   # 本工具进程：枚举/拾取都要排除，避免把自己当目标


# ---------------- 高亮覆盖层 ----------------


def _make_click_through(widget):
    """给顶层窗口加 WS_EX_LAYERED|WS_EX_TRANSPARENT，鼠标点击穿透到下层。"""
    hwnd = int(widget.winId())
    if not hwnd:
        return
    style = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    _user32.SetWindowLongW(
        hwnd, _GWL_EXSTYLE,
        style | _WS_EX_LAYERED | _WS_EX_TRANSPARENT,
    )
    # SWP_FRAMECHANGED 让样式立即生效
    _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)


def _offscreen(rect) -> bool:
    """矩形是否不可见（宽高非正、或最小化后的哨兵坐标）。

    最小化窗口的 UIA 矩形是 (-32000,-32000,...) 这类值；画出来只会是
    屏幕角落的一条线，而且会把 `_rect` 占住，恢复时因"坐标没变"而漏刷新。
    注意副屏可能有合法负坐标，所以只判 -30000 以下的哨兵值。
    """
    l, t, r, b = rect
    return r - l <= 0 or b - t <= 0 or l <= -30000 or t <= -30000


class Overlay(QWidget):
    """全屏透明置顶窗：高亮一个控件。

    两种模式：
    - show_node(node)：常驻 + 后台线程 300ms 轮询跟随活体坐标（点查询结果时用）
    - blink_node(node)：闪烁确认后自动消失（拾取成功时用）
      3 次闪烁（500ms/周期，亮250灭250，只切 alpha 不动窗口显隐），
      最后停亮 2s 再消失；闪烁期间不轮询坐标。
    - clear()：立即隐藏（结果取消选中/刷新快照时调）
    """

    rect_polled = Signal(object, object)  # node, rect

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._node = None
        self._rect: tuple | None = None
        self._visible = True          # 闪烁时切 alpha 用；常驻时恒为 True
        self._blinking = False
        self._blink_gen = 0           # 闪烁轮次：区分过期收尾定时器
        self._blink_left = 0          # 剩余半周期数
        self._follow_stop: threading.Event | None = None
        self._blink_timer = QTimer(self, interval=250, timeout=self._blink_step)
        # 绘制用的 dpr：覆盖层一次只铺在目标所在的那块屏幕上，dpr 随目标切换
        self._dpr = QApplication.primaryScreen().devicePixelRatio() or 1.0
        self.rect_polled.connect(self._on_rect_polled)
        self.setGeometry(QApplication.primaryScreen().virtualGeometry())
        # WA_TransparentForMouseEvents 只对 Qt 内部事件有效；
        # 要让点击真正穿透到下层窗口，必须加 WS_EX_TRANSPARENT 扩展样式
        _make_click_through(self)

    def is_blinking(self) -> bool:
        return self._blinking

    def showEvent(self, event):
        super().showEvent(event)
        _make_click_through(self)  # 窗口重建后样式会丢，show 时补一次

    def show_node(self, node):
        """常驻高亮（跟随）。node 为 None 时隐藏。"""
        self._stop_blink()
        self._node = node
        if node is None:
            self.clear()
            return
        self._rect = node.rect
        self._visible = True
        if self._rect:
            self._fit_to_rect(self._rect)
            self.show()
            self.raise_()
            self.update()
        else:
            # 快照里没有可见矩形（窗口最小化/刚创建）：先别显示空框，
            # 交给跟随线程轮询到真实矩形后再显示
            self.hide()
        self._start_follow(node)

    def blink_node(self, node, cycles: int = 3, hold_ms: int = 2000,
                   rect=None) -> bool:
        """闪烁确认：cycles 次亮灭后停亮 hold_ms，再自动消失。

        rect 可传入刚读到的活体矩形：快照里的坐标可能已经过期（窗口挪动过），
        用旧坐标闪在错误位置比不闪更误导。
        返回是否真的开始闪烁：控件没有可见矩形时返回 False，交由调用方提示。
        """
        rect = rect or (node.rect if node is not None else None)
        if node is None or not rect:
            return False
        self._stop_follow()           # 闪烁期间不轮询
        self._node = node
        self._rect = rect
        self._fit_to_rect(rect)
        self._blinking = True
        self._blink_gen += 1
        gen = self._blink_gen
        self._blink_left = cycles * 2  # 半周期计数（亮→灭→亮…）
        self._visible = False          # 第一步翻转为亮
        self.show()
        self.raise_()
        self._blink_timer.start()
        QTimer.singleShot(
            int(250 * cycles * 2 + hold_ms), lambda: self._blink_finish(gen)
        )
        return True

    def _fit_to_rect(self, rect):
        """把覆盖层铺到 rect 所在的那块屏幕，并记下它的 dpr。

        多显示器混合缩放时各屏 dpr 不同：用主屏 dpr 统一换算会让副屏高亮整体偏移，
        所以这里按目标左上角定位屏幕，并把窗口几何设成该屏的逻辑几何。
        """
        l, t = rect[0], rect[1]
        screen, dpr = None, 0.0
        for s in QApplication.screens():
            d = s.devicePixelRatio() or 1.0
            g = s.geometry()
            # 屏幕逻辑几何 × 该屏 dpr = 该屏物理范围；UIA 的 rect 是物理坐标
            if g.left() * d <= l <= g.right() * d and g.top() * d <= t <= g.bottom() * d:
                screen, dpr = s, d
                break
        if screen is None:
            screen = QApplication.primaryScreen()
            dpr = screen.devicePixelRatio() or 1.0
        self._dpr = dpr
        geo = screen.geometry()
        if self.geometry() != geo:
            self.setGeometry(geo)

    def _blink_step(self):
        self._blink_left -= 1
        if self._blink_left <= 0:
            self._blink_timer.stop()
            self._visible = True       # 停亮，等 hold 定时器收尾
        else:
            self._visible = not self._visible
        self.update()

    def _blink_finish(self, gen: int):
        # 被 show_node/clear 打断时 _blinking 已置 False；
        # 若期间又发起了新一轮闪烁，gen 已过期，不能收掉新一轮的尾巴
        if not self._blinking or gen != self._blink_gen:
            return
        self._blinking = False
        self.clear()

    def _stop_blink(self):
        self._blink_gen += 1   # 让在途的收尾定时器失效
        self._blinking = False
        self._blink_timer.stop()

    def clear(self):
        self._node = None
        self._rect = None
        self._stop_blink()
        self._stop_follow()
        self.hide()

    # ---------------- 坐标跟随（后台线程） ----------------

    def _start_follow(self, node):
        self._stop_follow()
        stop = threading.Event()
        self._follow_stop = stop
        threading.Thread(
            target=self._follow_loop, args=(node, stop), daemon=True
        ).start()

    def _stop_follow(self):
        if self._follow_stop is not None:
            self._follow_stop.set()
            self._follow_stop = None

    def _follow_loop(self, node, stop):
        """后台轮询活体坐标：目标程序卡死时也只卡住这个线程，不冻 UI。

        这里跨线程复用快照线程创建的 UIA 控件读属性（UIA 代理通常可用）；
        控件销毁后会持续抛异常，连续失败即视为失效，让高亮消失而不是停在旧坐标。
        """
        fails = 0
        with auto.UIAutomationInitializerInThread(debug=False):
            while not stop.wait(0.3):
                try:
                    r = node.control.BoundingRectangle
                    rect = (r.left, r.top, r.right, r.bottom)
                except Exception:
                    fails += 1
                    if fails >= 10:   # ~3 秒都读不到：控件已失效
                        self.rect_polled.emit(node, None)
                        return
                    continue
                fails = 0
                self.rect_polled.emit(node, rect)

    def _on_rect_polled(self, node, rect):
        # 忽略已切换节点/闪烁期/已清除时到达的旧结果
        if node is not self._node or self._blinking:
            return
        if rect is None:      # 控件已失效：别让高亮挂在过期坐标上
            self._rect = None
            self.hide()
            return
        if _offscreen(rect):
            # 最小化/被折叠成零面积：没有可画的东西。把 _rect 置空，
            # 否则窗口恢复成同一矩形时会因"坐标没变"而漏掉重新显示
            self._rect = None
            self.hide()
            return
        if rect == self._rect:  # 坐标没变就不用重绘整块覆盖窗
            return
        self._rect = rect
        self._fit_to_rect(rect)  # 目标可能被拖到了另一块缩放比不同的屏幕
        if not self.isVisible():  # 之前因无矩形/离屏而隐藏，现在有了就补显示
            self.show()
            self.raise_()
        self.update()

    def paintEvent(self, event):
        if not self._rect or not self._visible:
            return
        origin = self.geometry().topLeft()
        # 快照里的 rect 是 UIA 物理屏幕坐标，Qt 窗口/画布用的是逻辑坐标，
        # 125% 缩放下 dpr=1.25，必须除一次才不会整体偏移放大；
        # dpr 取目标所在屏幕的值（_fit_to_rect 每次切换节点时更新）
        dpr = self._dpr or 1.0
        l, t, r, b = self._rect
        x, y = l / dpr - origin.x(), t / dpr - origin.y()
        w, h = (r - l) / dpr, (b - t) / dpr
        if w <= 0 or h <= 0:
            return
        p = QPainter(self)
        p.setBrush(QColor(255, 59, 48, 25))       # 半透明淡红填充
        p.setPen(QPen(QColor(255, 59, 48, 200), 1))  # 1px 边框
        p.drawRect(x, y, w, h)
        p.end()


# ---------------- 后台线程 ----------------


def enum_top_windows(exclude_hwnd: int = 0, include_owned: bool = True):
    """纯 Win32 枚举顶层窗口：(标题, PID, HWND)。

    不用 UIA 枚举：逐个读 UIA 属性要走跨进程 COM，实测几十个窗口要 6 秒，
    会把界面冻住；Win32 这里只要几毫秒。

    本进程自身的窗口一律跳过（主窗口、示范代码窗、错误框）：
    把它们当目标只会拍到自己、覆盖现有树。
    include_owned=True 时包含附属窗口（GetWindow GW_OWNER 非 0）：
    「另存为」「字体」这类对话框都靠它进列表；只要主窗口时传 False。
    """
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if hwnd == exclude_hwnd or not _user32.IsWindowVisible(hwnd):
            return True
        if not include_owned and _user32.GetWindow(hwnd, _GW_OWNER):
            return True  # 跳过弹出/工具窗口
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:  # 跳过无标题窗口
            return True
        cloaked = wintypes.DWORD(0)  # 跳过 UWP 幽灵窗口
        if _dwmapi.DwmGetWindowAttribute(
            hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        ) == 0 and cloaked.value:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == _SELF_PID:
            return True  # 本工具自己的窗口（主窗口、示范代码窗、错误框）
        found.append((buf.value, pid.value, hwnd))
        return True

    _user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


class SnapshotThread(QThread):
    done = Signal(object, float, int, int)  # root, elapsed, node_count, seq
    failed = Signal(str, int)               # 错误信息, seq

    def __init__(self, hwnd: int, seq: int, parent=None):
        super().__init__(parent)
        self._hwnd = hwnd
        self._seq = seq

    def run(self):
        try:
            # 子线程自己初始化 COM，否则 ControlFromHandle 会失败
            with auto.UIAutomationInitializerInThread(debug=False):
                control = auto.ControlFromHandle(self._hwnd)
                if control is None:
                    self.failed.emit("无法获取窗口控件（窗口可能已关闭）", self._seq)
                    return
                root, elapsed, count = build_snapshot(control)
                self.done.emit(root, elapsed, count, self._seq)
        except Exception as e:  # COM 错误等
            self.failed.emit(str(e), self._seq)


def build_snapshot(control):
    """建快照的纯函数，SnapshotThread 和 Picker 都复用。"""
    from uiaxpath.snapshot import count_nodes

    t0 = time.perf_counter()
    root = snapshot(control)
    elapsed = time.perf_counter() - t0
    return root, elapsed, count_nodes(root)


def _top_window(control):
    """沿父链向上找第一个 WindowControl。"""
    cur = control
    while cur is not None:
        try:
            if cur.ControlTypeName == "WindowControl":
                return cur
            cur = cur.GetParentControl()
        except Exception:
            break
    return None


class PickBridge(QObject):
    picked = Signal(object, object, str)  # root, node, path
    failed = Signal(str)
    status = Signal(str)                  # 状态栏提示（如"正在为 X 建快照…"）


_LOCK_KEY = 0xA2  # VK_LCONTROL：左 Ctrl，所有键盘都有，且只区分左键避免与组合键混淆


def _lock_state() -> tuple[bool, bool]:
    """(左 Ctrl 是否按住, 自上次查询以来是否按下过)。

    两个位一起用：0x8000 是按住状态，0x0001 能兜住两次 20ms 采样之间
    就已松开的极短点按（只靠按住位会整轮漏检）。
    """
    v = _user32.GetAsyncKeyState(_LOCK_KEY)
    return bool(v & 0x8000), bool(v & 0x0001)


class Picker:
    """手动触发的拾取器：工具栏按钮开启后，按左 Ctrl 锁当前鼠标位置。

    方案 C：
    - 若鼠标下窗口 == 当前快照根窗口（按 HWND 判断），直接复用现有快照定位（快）
    - 否则自动抓取该窗口的新快照（会发 status 信号提示"正在为 X 建快照…"）
    - 抓到新窗口后主窗口会用新 root 刷新树，并自动切到该窗口的下拉项

    锁定键用左 Ctrl 而非全局热键：只在拾取模式激活时轮询，
    不与输入法/系统快捷键冲突；Ctrl 必须区分左右是因为 GetAsyncKeyState(VK_CONTROL)
    在按住任意一侧 Ctrl 时都为真，而左 Ctrl 更少被组合键占用。
    """

    def __init__(self, get_root):
        self._get_root = get_root
        self.bridge = PickBridge()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self):
        if self._active:
            return
        self._active = True
        # 每轮用独立的停止标志：复用同一个 Event 再 clear() 会把上一轮
        # 尚未退出的线程"复活"，出现两条线程同时等左 Ctrl 的双拾取
        stop = threading.Event()
        self._stop = stop
        self._thread = threading.Thread(
            target=self._loop, args=(stop,), daemon=True
        )
        self._thread.start()

    def stop(self):
        if not self._active:
            return
        self._stop.set()
        self._active = False

    def _loop(self, stop):
        try:
            self._wait_and_pick(stop)
        except Exception as e:
            # 后台线程异常在 pythonw 下没有控制台可看：转成界面提示，
            # 否则按钮会一直停在"取消拾取"而用户不知道发生了什么
            if not stop.is_set():
                self.bridge.failed.emit(f"拾取线程异常: {e}")
        finally:
            # 只在当前轮仍是活动轮时复位，避免旧线程收尾时把新轮标成未激活
            if self._stop is stop:
                self._active = False

    def _wait_and_pick(self, stop):
        # 先排空"已经按住 / 已经按下"的旧状态（也顺带消费残留的按下沿位），
        # 再等一次新的左 Ctrl
        while not stop.is_set():
            down, hit = _lock_state()
            if not down and not hit:
                break
            time.sleep(0.02)
        while not stop.is_set():
            down, hit = _lock_state()
            if down or hit:
                break
            time.sleep(0.02)
        if stop.is_set():
            return
        x, y = _cursor_point()
        # 等左 Ctrl 松开（极短点按此时已松开，循环立即结束）
        while not stop.is_set():
            if not _lock_state()[0]:
                break
            time.sleep(0.02)
        if stop.is_set():  # 用户已取消：不产生迟到结果
            return
        # 子线程显式初始化 COM：ControlFromPoint/GetRuntimeId 才稳定
        with auto.UIAutomationInitializerInThread(debug=False):
            self._pick_at(x, y, stop)

    def _pick_at(self, x, y, stop):
        try:
            control = auto.ControlFromPoint(x, y)
            if control is None:
                self.bridge.failed.emit("鼠标位置下未找到控件")
                return

            top = _top_window(control)
            if top is None:
                self.bridge.failed.emit("未找到该控件所属的顶层窗口")
                return
            # 排除本工具自己的窗口：按**进程**比，而不是只比主窗口句柄——
            # 示范代码窗、错误框等也属于本进程，拿它们当目标只会覆盖现有树
            try:
                if top.ProcessId == _SELF_PID:
                    self.bridge.failed.emit("不能拾取本工具自身的窗口")
                    return
            except Exception:
                pass

            # 方案 C：HWND 相同则复用，否则新建快照
            root = self._get_root()
            root_hwnd = None
            if root is not None:
                try:      # 旧快照的窗口可能已关闭，属性访问会抛 COMError
                    root_hwnd = root.control.NativeWindowHandle
                except Exception:
                    root_hwnd = None
            same = root is not None and root_hwnd == top.NativeWindowHandle
            title = top.Name or "(无标题)"
            if not same:
                self.bridge.status.emit(f"正在为窗口「{title}」建立快照…")
                root, elapsed, count = build_snapshot(top)
                self.bridge.status.emit(
                    f"窗口「{title}」快照完成：{count} 节点，{elapsed:.2f}s"
                )
                if stop.is_set():  # 建快照期间用户已取消：不产生迟到结果
                    return

            node = locate_in_snapshot(root, control, point=(x, y))
            if node is None and same:
                # 复用的旧快照可能已过期（窗口内容变过）：重建一次再试，
                # 免得用户被迫先手动点「捕获快照」再来拾取
                self.bridge.status.emit(
                    f"旧快照里找不到该控件，正在重建「{title}」快照…"
                )
                root, elapsed, count = build_snapshot(top)
                if stop.is_set():
                    return
                self.bridge.status.emit(
                    f"窗口「{title}」快照重建完成：{count} 节点，{elapsed:.2f}s"
                )
                node = locate_in_snapshot(root, control, point=(x, y))
            if node is None:
                if not stop.is_set():
                    self.bridge.failed.emit("控件不在当前快照内")
                return
            if stop.is_set():
                return
            path = node_path(node, root)
            self.bridge.picked.emit(root, node, path)
        except Exception as e:
            if not stop.is_set():
                self.bridge.failed.emit(str(e))


def _cursor_point():
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


# ---------------- 示范 Python 代码 ----------------


def _build_demo_code(win_name: str, expr: str) -> str:
    """生成最小可用的演示脚本：快照 → find_all → 点击第一个匹配。

    窗口名和表达式都用 !r 转义嵌入，引号/换行不会破坏生成的代码。
    """
    return f'''# -*- coding: utf-8 -*-
# 由 UIA XPath 测试工具生成
# 依赖：pip install uiautomation，以及本项目中的 uiaxpath 包（与脚本同目录或已加入 sys.path）
import uiautomation as auto
from uiaxpath import snapshot, find_all

win = auto.WindowControl(searchDepth=1, SubName={win_name!r})
if not win.Exists(3):
    raise SystemExit("找不到目标窗口，请确认它已打开")

root = snapshot(win)
found = find_all(root, {expr!r})
print(f"匹配 {{len(found)}} 个")
if found:
    found[0].control.Click()  # 点击第一个匹配；换成 SetFocus() 可只聚焦不点击
'''


class _DemoCodeDialog(QDialog):
    """非模态小窗：等宽字体只读代码 + 复制按钮。单例复用，每次 set_code 更新。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("示范 Python 代码")
        self.resize(560, 400)
        self._code = ""
        lay = QVBoxLayout(self)
        self.edit = QPlainTextEdit()
        self.edit.setReadOnly(True)
        self.edit.setFont(QFont("Consolas", 10))
        lay.addWidget(self.edit, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.copy_btn = QPushButton("复制")
        close_btn = QPushButton("关闭")
        row.addWidget(self.copy_btn)
        row.addWidget(close_btn)
        lay.addLayout(row)
        close_btn.clicked.connect(self.close)
        self.copy_btn.clicked.connect(self._copy)

    def set_code(self, code: str):
        self._code = code
        self.edit.setPlainText(code)
        self.copy_btn.setText("复制")

    def _copy(self):
        QApplication.clipboard().setText(self._code)
        self.copy_btn.setText("已复制 ✓")
        QTimer.singleShot(1500, lambda: self.copy_btn.setText("复制"))


# ---------------- 主窗口 ----------------


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("UIA XPath 测试工具")
        self.setWindowIcon(QIcon(_APP_ICON))
        self.resize(765, 670)
        self._root: UiaNode | None = None
        self._snap_thread: SnapshotThread | None = None
        self._snap_seq = 0          # 每次"捕获/拾取"递增，用来丢弃迟到的旧快照
        self._demo_dlg: _DemoCodeDialog | None = None
        self._item_of: dict[int, QTreeWidgetItem] = {}
        self._filter_active = False
        self._current_node: UiaNode | None = None   # 树当前选中节点
        self._cand_node: UiaNode | None = None      # 候选区当前对应的节点
        # 候选结果缓存：(id(root), id(node)) → 候选列表。大树单次 generate 约 120ms，
        # 来回切换节点时不必重复算；换根快照时整体清空
        self._cand_cache: dict[tuple[int, int], list] = {}
        # 跨会话保存的 XPath 历史，下次启动直接可补全
        self._settings = QSettings("UIAXPathTool", "uia-xpath-gui")
        saved = self._settings.value("xpath_history", [])
        if isinstance(saved, str):
            saved = [saved]
        self._xpath_history: list[str] = [str(s) for s in saved if s][:10]
        self._hist_model = QStringListModel(self._xpath_history, self)
        # 历史只在退出时统一落盘：每条成功查询都写一次注册表没有必要
        self._settings_dirty = False

        self.overlay = Overlay()

        # 工具栏
        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(6, 6, 6, 0)
        self.win_combo = QComboBox()
        self.win_combo.setMinimumWidth(240)
        # 不设 maximumWidth：窗口拉宽时只让下拉框变长，按钮固定在右侧不动；
        # 有上限的话多余宽度会变成下拉框和按钮之间的空隙，很难看
        self.refresh_btn = QPushButton("刷新窗口列表")
        self.snap_btn = QPushButton("捕获快照")
        self.pick_btn = QPushButton("🎯 拾取 (左Ctrl)")
        self.pick_btn.setAccessibleName("拾取")
        self.pick_btn.setCheckable(True)
        self.pick_btn.setToolTip(
            "点此进入拾取模式 → 把鼠标移到目标控件上 → 按左 Ctrl 锁定\n"
            "拾取失败或想随时取消，再点一次按钮退出"
        )
        bar_lay.addWidget(QLabel("目标窗口:"))
        bar_lay.addWidget(self.win_combo, 1)
        # 顺序按操作流程排：先刷列表选窗口，再捕获，最后才是逐步试用的拾取
        bar_lay.addWidget(self.refresh_btn)
        bar_lay.addWidget(self.snap_btn)
        bar_lay.addSpacing(16)
        bar_lay.addWidget(self.pick_btn)

        # 控件树
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["控件树"])
        self.tree.itemExpanded.connect(self._on_expand)
        self.tree.currentItemChanged.connect(self._on_tree_select)

        # 控件树过滤：输入即筛（防抖 250ms），清空恢复懒加载全树
        self.tree_filter = QLineEdit()
        self.tree_filter.setPlaceholderText("过滤：输入控件名称或类型关键字，清空恢复全树")
        self.tree_filter.setClearButtonEnabled(True)
        self._filter_timer = QTimer(
            self, interval=250, singleShot=True, timeout=self._rebuild_tree
        )
        self.tree_filter.textChanged.connect(lambda _: self._filter_timer.start())

        tree_box = QWidget()
        tree_lay = QVBoxLayout(tree_box)
        tree_lay.setContentsMargins(0, 0, 0, 0)
        tree_lay.addWidget(self.tree_filter)
        tree_lay.addWidget(self.tree, 1)

        # 属性 / 候选（上移到右侧顶部，与树选择强绑定）
        self.tabs = QTabWidget()
        self.props = QTableWidget(0, 2)
        self.props.setHorizontalHeaderLabels(["属性", "值"])
        # 属性名短、值长：固定窄的第一列，把剩余宽度都留给"值"
        self.props.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        self.props.setColumnWidth(0, 140)
        self.props.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.props.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # 按行选中 + Ctrl+C 复制，方便把属性贴到脚本里
        self.props.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        QShortcut(QKeySequence.StandardKey.Copy, self.props).activated.connect(
            self._copy_props
        )
        self.cands = QListWidget()
        self.cands.itemClicked.connect(self._on_cand_click)
        self.tabs.addTab(self.props, "属性")
        self.tabs.addTab(self.cands, "候选 XPath")
        # 候选生成要跑最多 6 次全树查询（大树实测 ~120ms）：只有切到候选页才算，
        # 否则每次点控件树都在为一个看不见的页白付延迟
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # XPath 编辑区：最近执行的表达式进补全列表，免重复输入
        completer = QCompleter(self._hist_model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setMaxVisibleItems(10)
        self.xpath_edit = QLineEdit()
        self.xpath_edit.setPlaceholderText('如 //Button[@Name="确定"]，回车执行')
        self.xpath_edit.returnPressed.connect(self.run_xpath)
        self.xpath_edit.setCompleter(completer)

        # XPath 编辑区容器
        xpath_box = QWidget()
        xpath_lay = QVBoxLayout(xpath_box)
        xpath_lay.setContentsMargins(0, 0, 0, 0)
        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.addWidget(QLabel("XPath"))
        input_row.addWidget(self.xpath_edit, 1)
        # 放输入行右侧而非灰字行：按钮读的是输入框表达式，语义相邻，行高也协调
        self.demo_btn = QPushButton("示范Python")
        self.demo_btn.setToolTip("用当前输入框的 XPath 生成最小可用的 Python 代码")
        input_row.addWidget(self.demo_btn)
        self.xpath_info = QLabel(_RESULT_HINT)
        self.xpath_info.setStyleSheet(_INFO_STYLE)
        xpath_lay.addLayout(input_row)
        xpath_lay.addWidget(self.xpath_info)

        # 查询结果列表（每行：控件 ｜ 路径）
        self.results = QListWidget()
        # 加浅边框把结果区和上方编辑区区分开，空列表时也能看出是一块区域
        self.results.setStyleSheet(
            "QListWidget { border: 1px solid #cfd3d8; border-radius: 3px; }"
        )
        # 选中状态的唯一入口：点击/Ctrl+点击/方向键都会触发 selectionChanged。
        # 不能用 currentItemChanged——见 _on_result_selection_changed 注释。
        self.results.itemSelectionChanged.connect(self._on_result_selection_changed)
        results_hint = QLabel("Ctrl+点击已选中的结果可取消选中（高亮消失）")
        results_hint.setStyleSheet("color: gray; font-size: 11px;")

        # 右列两块（上=属性/候选，下=XPath+结果）
        bottom = QWidget()
        bottom_lay = QVBoxLayout(bottom)
        bottom_lay.setContentsMargins(0, 0, 0, 0)
        bottom_lay.addWidget(xpath_box)
        bottom_lay.addWidget(self.results, 1)
        bottom_lay.addWidget(results_hint)

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.tabs)
        right.addWidget(bottom)
        # 属性区给足高度方便看更多属性；结果多数只有一条，给少些、多条时滚动
        right.setSizes([400, 200])

        # 三块横向 splitter：控件树 | 属性/候选 | XPath+结果
        split = QSplitter()
        split.addWidget(tree_box)
        split.addWidget(right)
        split.setSizes([330, 420])

        central = QWidget()
        lay = QVBoxLayout(central)
        # 左右/底部留 6px：面板不要贴着窗口边框，密度也更均匀
        lay.setContentsMargins(6, 0, 6, 6)
        lay.setSpacing(6)
        lay.addWidget(bar)
        lay.addWidget(split, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        # 事件绑定
        self.refresh_btn.clicked.connect(self.reload_windows)
        self.snap_btn.clicked.connect(self.capture)
        self.win_combo.activated.connect(self.capture)
        self.pick_btn.clicked.connect(self._toggle_pick)
        self.demo_btn.clicked.connect(self._show_demo_code)

        # 拾取器（非常驻）
        self.picker = Picker(lambda: self._root)
        self.picker.bridge.picked.connect(self._on_picked)
        self.picker.bridge.failed.connect(self._on_pick_failed)
        # 进度类消息用默认色；只有真正的失败才标红
        self.picker.bridge.status.connect(self._status)

        self.reload_windows()
        self._rebuild_tree()   # 未捕获快照：树头 + 属性/候选给出占位提示

    # ---------------- 窗口列表与快照 ----------------

    def reload_windows(self):
        previous = self.win_combo.currentData()
        self.win_combo.clear()
        try:
            # 固定包含附属窗口（另存为/字体等对话框）：它们本来就是高频调试目标，
            # 做成开关只会让用户多一步「勾选再刷新」
            for title, pid, hwnd in enum_top_windows(int(self.winId()), True):
                self.win_combo.addItem(f"{title}  (PID {pid})", hwnd)
        except Exception as e:
            self._status(f"枚举窗口失败: {e}", red=True)
            return
        # 刷新后尽量保留原来选中的窗口
        if previous is not None:
            for i in range(self.win_combo.count()):
                if self.win_combo.itemData(i) == previous:
                    self.win_combo.setCurrentIndex(i)
                    break
        # 树里显示的仍是快照根：下拉框必须跟着它，否则用户会以为
        # 再点「捕获快照」拍的是同一个窗口
        if self._root is not None:
            self._sync_combo_to_root(self._root)
        # 走 _status 统一清掉上一次可能残留的红色样式
        self._status(f"共 {self.win_combo.count()} 个顶层窗口")

    def capture(self):
        hwnd = self.win_combo.currentData()
        if hwnd is None:
            return
        thread = self._snap_thread
        if thread is not None:
            try:
                busy = thread.isRunning()
            except RuntimeError:
                # 旧线程对象已被 deleteLater 销毁，视为空闲
                self._snap_thread = None
                busy = False
            if busy:
                self._status("快照进行中，请稍候…")
                return
        self._status("快照中…")
        self._snap_seq += 1
        thread = SnapshotThread(hwnd, self._snap_seq, self)
        self._snap_thread = thread
        thread.done.connect(self._on_snapshot)
        thread.failed.connect(self._on_snapshot_failed)
        # finished 后再 deleteLater，避免每次捕获都累积 QThread 对象
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_snapshot(self, root: UiaNode, elapsed: float, count: int, seq: int):
        # 本线程已经结束：先摘掉引用。不能只在 seq 有效时置 None——否则
        # 迟到线程随后被 deleteLater 销毁，self._snap_thread 会变成一个
        # 已死的包装对象，下次 capture()/closeEvent() 调 isRunning() 会抛
        # RuntimeError（Internal C++ object already deleted）
        if self.sender() is self._snap_thread:
            self._snap_thread = None
        if seq != self._snap_seq:
            return  # 期间又捕获/拾取了新快照：迟到的旧结果直接丢弃
        self._apply_root(root, f"快照完成: {count} 节点, {elapsed:.2f}s — {root.name}")

    def _on_snapshot_failed(self, msg: str, seq: int):
        if self.sender() is self._snap_thread:
            self._snap_thread = None
        if seq != self._snap_seq:
            return
        self._status(f"快照失败: {msg}", red=True)

    def _apply_root(self, root: UiaNode, status: str = ""):
        """用新 root 刷新树、属性、候选、下拉框；清除旧高亮。"""
        self._root = root
        self._current_node = None
        self._cand_node = None
        self._cand_cache.clear()   # 换根后旧节点的候选全部作废
        self.overlay.clear()
        self.results.clear()
        # 结果列表已清空，统计行必须一起清，否则会显示过期的"匹配 N 个"
        self._set_xpath_error(None)
        self.xpath_info.setText(_RESULT_HINT)
        self.props.setRowCount(0)
        self.props.clearSpans()
        self.cands.clear()
        self._rebuild_tree()
        self._sync_combo_to_root(root)
        if status:
            self._status(status)

    def _sync_combo_to_root(self, root: UiaNode):
        """让窗口下拉框跟着当前快照根走；目标窗口不在列表时补插一条。

        拾取可能命中"刷新列表之后才出现"的弹出层/新窗口。如果只做
        "查得到就选中"，下拉框会继续显示旧窗口，用户随后点「捕获快照」
        就会拍到错误窗口。
        """
        try:
            hwnd = root.control.NativeWindowHandle
        except Exception:
            return   # 窗口刚关闭：属性访问会抛 COMError
        for i in range(self.win_combo.count()):
            if self.win_combo.itemData(i) == hwnd:
                self.win_combo.setCurrentIndex(i)
                return
        label = f"{root.name or '(无标题)'}  (PID {root.process_id})"
        self.win_combo.insertItem(0, label, hwnd)
        self.win_combo.setCurrentIndex(0)

    def _status(self, text: str, red: bool = False):
        self.statusBar().setStyleSheet("color: #d93025;" if red else "")
        self.statusBar().showMessage(text)

    def closeEvent(self, event):
        """关窗收尾：停拾取线程，等建树线程退出。

        销毁还在 isRunning() 的 QThread 是 Qt 未定义行为（通常直接崩），
        而 finished→deleteLater 覆盖不到"建树途中关窗"这条路径。
        """
        self.picker.stop()
        self._flush_settings()
        thread = self._snap_thread
        if thread is not None:
            try:
                running = thread.isRunning()
            except RuntimeError:   # 线程对象已被 deleteLater 销毁
                running = False
            if running and not thread.wait(5000):
                # 目标程序卡死：宁可让线程对象留到进程退出，也不销毁运行中的 QThread
                thread.setParent(None)
        super().closeEvent(event)

    # ---------------- 树 ----------------

    @staticmethod
    def _label(node: UiaNode) -> str:
        name = node.name
        if len(name) > 40:
            name = name[:40] + "…"
        return f'{short_name(node.control_type)}  "{name}"' if name else short_name(node.control_type)

    def _make_item(self, node: UiaNode) -> QTreeWidgetItem:
        item = QTreeWidgetItem([self._label(node)])
        item.setIcon(0, _type_icon(node.control_type))
        item.setData(0, ROLE_NODE, node)
        self._item_of[id(node)] = item
        if node.children:
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
            )
        return item

    def _load_children(self, item: QTreeWidgetItem):
        if item.childCount() > 0:
            return
        node: UiaNode = item.data(0, ROLE_NODE)
        for child in node.children:
            item.addChild(self._make_item(child))

    @staticmethod
    def _matches(node: UiaNode, keyword: str) -> bool:
        """过滤匹配：名称或控件类型包含关键字（均小写比较）。"""
        return keyword in node.name.lower() or keyword in node.control_type.lower()

    def _rebuild_tree(self):
        """按过滤框内容重建控件树。

        无过滤：懒加载整树并选中根（属性/候选不空白）。
        有过滤：一次性建出"匹配节点 + 其父链"，展开全部，选中第一个匹配项。
        """
        keyword = self.tree_filter.text().strip().lower()
        self._filter_active = bool(keyword)
        self._item_of.clear()
        self.tree.clear()
        if self._root is None:
            self.tree.headerItem().setText(0, "控件树（尚未捕获快照）")
            self._show_empty_state()
            return
        if not self._filter_active:
            self.tree.headerItem().setText(0, "控件树")
            item = self._make_item(self._root)
            self.tree.addTopLevelItem(item)
            self._load_children(item)
            self.tree.expandItem(item)
            self.tree.setCurrentItem(item)  # 默认选中根
            return

        keep: set[int] = set()
        first_match = None
        n_match = 0
        for node in [self._root, *self._root.descendants()]:
            if not self._matches(node, keyword):
                continue
            n_match += 1
            first_match = first_match or node
            cur = node
            while cur is not None and id(cur) not in keep:
                keep.add(id(cur))
                cur = cur.parent

        def build(parent_item, node) -> QTreeWidgetItem:
            item = self._make_item(node)
            if parent_item is None:
                self.tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            for child in node.children:
                if id(child) in keep:
                    build(item, child)
            # 没有显示出来的子节点时去掉展开箭头，
            # 否则展开会走 _load_children 把被过滤掉的节点又加回来
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
                if item.childCount()
                else QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator
            )
            return item

        build(None, self._root)
        self.tree.expandAll()
        self.tree.headerItem().setText(0, f"控件树（过滤：{n_match} 项）")
        if first_match is None:
            # 0 命中时树里只有根项且不会触发 currentItemChanged，
            # 右侧会一直停在上一窗口/上一节点的属性上，必须显式清掉
            self._current_node = None
            self._cand_node = None
            self._show_placeholder("当前过滤条件下没有匹配的控件")
            return
        item = self._item_of.get(id(first_match))
        if item is not None:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)

    def _on_expand(self, item: QTreeWidgetItem):
        if self._filter_active:
            return  # 过滤模式的树已整棵建好，不能再懒加载被过滤掉的子节点
        self._load_children(item)
        for i in range(item.childCount()):
            self._load_children(item.child(i))  # 预载一层，展开指示器才正确

    def reveal(self, node: UiaNode):
        """沿父链展开，把 node 显示出来并选中。"""
        if self._reveal_impl(node):
            return
        # 过滤模式下目标可能被隐藏：清空过滤后重试一次
        if self.tree_filter.text():
            blocked = self.tree_filter.blockSignals(True)
            self.tree_filter.setText("")
            self.tree_filter.blockSignals(blocked)
            self._filter_timer.stop()
            self._rebuild_tree()
            self._reveal_impl(node)

    def _reveal_impl(self, node: UiaNode) -> bool:
        chain = []
        cur = node
        while cur is not None:
            chain.append(cur)
            cur = cur.parent
        chain.reverse()
        item = self._item_of.get(id(chain[0]))
        if item is None:
            return False
        for n in chain[1:]:
            self._load_children(item)
            item = next(
                (item.child(i) for i in range(item.childCount())
                 if item.child(i).data(0, ROLE_NODE) is n),
                None,
            )
            if item is None:
                return False
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)
        return True

    def _on_tree_select(self, item, _prev):
        if item is None:
            return
        node: UiaNode = item.data(0, ROLE_NODE)
        self._current_node = node
        self._show_props(node)
        # 候选生成较贵（大树 ~120ms），候选页不可见时先不算
        if self.tabs.currentIndex() == 1:
            self._show_candidates(node)

    def _on_tab_changed(self, index: int):
        """切到候选页时才为当前节点补算候选。"""
        if index != 1:
            return
        node = self._current_node
        if node is None or node is self._cand_node:
            return
        self._show_candidates(node)

    # ---------------- 属性与候选 ----------------

    def _show_empty_state(self):
        """未捕获快照时给属性/候选两栏一个明确占位，避免整块区域空白得让人迷惑。"""
        self._show_placeholder("尚未捕获快照：在上方选择目标窗口后点「捕获快照」")

    def _show_placeholder(self, hint: str):
        """把属性/候选两栏都置成不可点击的提示文案。"""
        self.props.clearSpans()
        self.props.setRowCount(1)
        self.props.setSpan(0, 0, 1, 2)
        self.props.setItem(0, 0, QTableWidgetItem(hint))
        self.cands.clear()
        item = QListWidgetItem(hint)
        item.setFlags(Qt.ItemFlag.NoItemFlags)  # 纯提示，不可点击
        self.cands.addItem(item)

    def _copy_props(self):
        """Ctrl+C 复制属性：优先复制选中行，没选就复制全部（Key<TAB>Value 每行）。"""
        rows = sorted({i.row() for i in self.props.selectedIndexes()})
        if not rows:
            rows = list(range(self.props.rowCount()))
        lines = []
        for r in rows:
            key, val = self.props.item(r, 0), self.props.item(r, 1)
            if key is None and val is None:
                continue
            lines.append(f"{key.text() if key else ''}\t{val.text() if val else ''}")
        if not lines:
            return
        QApplication.clipboard().setText("\n".join(lines))
        self._status(f"已复制 {len(lines)} 行属性")

    def _show_props(self, node: UiaNode):
        self.props.clearSpans()
        rows = [
            ("Name", node.name),
            ("AutomationId", node.automation_id),
            ("ClassName", node.class_name),
            ("ControlType", node.control_type),
            ("ProcessId", str(node.process_id)),
            ("Rect", str(node.rect)),
            ("RuntimeId", str(node.runtime_id)),
        ]
        if self._root is not None:
            rows.append(("路径", node_path(node, self._root)))
        self.props.setRowCount(len(rows))
        for i, (k, v) in enumerate(rows):
            self.props.setItem(i, 0, QTableWidgetItem(k))
            self.props.setItem(i, 1, QTableWidgetItem(v))

    def _show_candidates(self, node: UiaNode):
        self._cand_node = node
        self.cands.clear()
        if self._root is None:
            self._show_empty_state()
            return
        key = (id(self._root), id(node))
        found = self._cand_cache.get(key)
        if found is None:
            found = generate(self._root, node)
            self._cand_cache[key] = found
        if not found:
            hint = QListWidgetItem("该节点没有可用的定位表达式")
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            self.cands.addItem(hint)
            return
        for c in found:
            mark = "✓唯一" if c.unique else f"×{c.count}"
            item = QListWidgetItem(f"{c.expr}    [{mark}] {c.note}")
            # 绿点=唯一命中，橙点=多处命中，扫一眼就能分辨
            item.setIcon(_dot_icon("#2E7D32" if c.unique else "#E08A00"))
            item.setToolTip(c.expr)
            item.setData(ROLE_NODE, c.expr)
            self.cands.addItem(item)

    def _on_cand_click(self, item: QListWidgetItem):
        expr = item.data(ROLE_NODE)
        if not expr:   # 占位提示项（尚未捕获快照/无可用表达式）没有表达式可执行
            return
        self.xpath_edit.setText(expr)
        self.run_xpath()

    # ---------------- XPath 执行 ----------------

    def run_xpath(self):
        if self._root is None:
            self._set_xpath_error(None)
            self.xpath_info.setText("请先捕获快照")
            return
        expr = self.xpath_edit.text().strip()
        if not expr:
            # 只复位样式会把上一次的红色错误文案留成灰字，看起来像还在报错
            self._set_xpath_error(None)
            self.xpath_info.setText(_RESULT_HINT)
            return
        try:
            t0 = time.perf_counter()
            found = find_all(self._root, expr)
            ms = (time.perf_counter() - t0) * 1000
        except XPathSyntaxError as e:
            self._set_xpath_error(f"语法错误: {e}")
            self.results.clear()
            return
        except Exception as e:
            # 深嵌套谓词/超长 and 链会抛 RecursionError 等非语法异常：
            # 就地写进提示行，避免冒到全局 excepthook 弹模态错误框
            self._set_xpath_error(f"执行失败: {type(e).__name__}: {e}")
            self.results.clear()
            return
        self._set_xpath_error(None)
        limit_note = (
            f"（仅显示前 {_RESULT_LIMIT} 条）" if len(found) > _RESULT_LIMIT else ""
        )
        self.xpath_info.setText(
            f"匹配 {len(found)} 个{limit_note}，{ms:.1f}ms ｜ {_RESULT_HINT}"
        )
        self._remember_xpath(expr)
        # 重填结果时屏蔽信号：否则 Qt 会自动选中新插入的行，
        # 触发 _on_result_select 把控件树选中项改到最后一个匹配节点
        blocked = self.results.blockSignals(True)
        try:
            self.results.clear()
            # 行内只放控件标签；路径等选中该行时再算，几千条结果也不卡
            for node in found[:_RESULT_LIMIT]:
                item = QListWidgetItem(self._label(node))
                item.setData(ROLE_NODE, node)
                self.results.addItem(item)
        finally:
            self.results.blockSignals(blocked)
        if not self.overlay.is_blinking():
            self.overlay.clear()

    def _set_xpath_error(self, message: str | None):
        """语法/执行错误：输入框描红 + 提示行变红；恢复正常时一起复位。"""
        if message:
            self.xpath_edit.setStyleSheet(_EDIT_ERROR_STYLE)
            self.xpath_info.setStyleSheet(_INFO_ERROR_STYLE)
            self.xpath_info.setText(message)
        else:
            self.xpath_edit.setStyleSheet("")
            self.xpath_info.setStyleSheet(_INFO_STYLE)

    # ---------------- 示范 Python 代码 ----------------

    def _show_demo_code(self):
        """用当前输入框的表达式生成示范代码，弹非模态小窗（单例复用）。"""
        expr = self.xpath_edit.text().strip()
        if not expr:
            self._status("先在输入框里写好 XPath，再生成示范代码", red=True)
            return
        # 窗口名优先取快照根：下拉框只表示"下次捕获哪个窗口"，
        # 刷新列表/选择变化后可能与当前树不一致，取它会把代码指向错误窗口
        if self._root is not None:
            title = self._root.name
        else:
            # 没有快照时退回下拉框文本（形如 "影刀  (PID 14120)"，剥掉 PID 后缀）
            title = self.win_combo.currentText().rsplit("  (PID ", 1)[0]
        title = title or "目标窗口标题"
        code = _build_demo_code(title, expr)
        if self._demo_dlg is None:
            self._demo_dlg = _DemoCodeDialog(self)
        self._demo_dlg.set_code(code)
        self._demo_dlg.show()
        self._demo_dlg.raise_()
        self._demo_dlg.activateWindow()

    def _remember_xpath(self, expr: str):
        """把执行成功的表达式放进补全历史（最近 10 条，重复的提到最前），退出时落盘。"""
        hist = self._xpath_history
        if expr in hist:
            hist.remove(expr)
        hist.insert(0, expr)
        del hist[10:]
        self._hist_model.setStringList(hist)
        self._settings_dirty = True

    def _flush_settings(self):
        """把有改动的设置写回注册表（程序退出时调用一次）。"""
        if not self._settings_dirty:
            return
        self._settings.setValue("xpath_history", self._xpath_history)
        self._settings.sync()
        self._settings_dirty = False

    def _on_result_selection_changed(self):
        """结果列表唯一的选中入口：有选中→高亮跟随，无选中→高亮消失。

        不能用 currentItemChanged：Ctrl+点击取消选中时只清 selection、
        current 还留在原行；再点同一行 current 没变、该信号不发，
        "显示高亮"这一步就丢了。selectionChanged 两个方向都会触发。
        """
        selected = self.results.selectedItems()
        if not selected:
            # 拾取闪烁期间被 run_xpath 清空结果触发的取消选中，不打断闪烁
            if not self.overlay.is_blinking():
                self.overlay.clear()
            return
        node: UiaNode = selected[0].data(ROLE_NODE)
        # 路径按需计算：首次选中这一行时补上，之后不再重复算
        if not selected[0].data(ROLE_PATH_SHOWN):
            selected[0].setData(ROLE_PATH_SHOWN, True)
            if self._root is not None:
                selected[0].setText(
                    f"{self._label(node)} ｜ {node_path(node, self._root)}"
                )
        self.reveal(node)
        self.overlay.show_node(node)

    # ---------------- 拾取 ----------------

    def _toggle_pick(self):
        if self.picker.active:
            self.picker.stop()
            self._set_pick_ui(False)
            self._status("已退出拾取模式")
        else:
            self.picker.start()
            if self.picker.active:
                self._set_pick_ui(True)
                # 红色在状态栏专门表示失败，模式提示用默认色
                self._status(
                    "🎯 拾取中 → 把鼠标移到目标控件上，按左 Ctrl 锁定（再点按钮取消）"
                )

    def _set_pick_ui(self, active: bool):
        self.pick_btn.setChecked(active)
        self.pick_btn.setText("❌ 取消拾取" if active else "🎯 拾取 (左Ctrl)")

    def _on_picked(self, root: UiaNode, node: UiaNode, path: str):
        self._set_pick_ui(False)
        # 拾取是明确的最新意图：作废任何还在后台跑的旧快照，
        # 否则它完成后会把刚拾取的窗口/树覆盖掉
        self._snap_seq += 1
        # 同一窗口复用快照：不要重建树，否则展开状态/结果列表每次拾取都被清空
        if root is not self._root:
            self._apply_root(root)
        self.activateWindow()
        self.reveal(node)
        self.xpath_edit.setText(path)
        self.run_xpath()
        # 拾取确认优先用刚读到的活体矩形：窗口挪动后快照坐标已过期，
        # 照着旧位置闪比不闪更误导；只有读不到时才退回快照矩形
        live = None
        try:
            r = node.control.BoundingRectangle
            live = (r.left, r.top, r.right, r.bottom)
        except Exception:
            pass
        if live is None:
            flashed = self.overlay.blink_node(node)
        elif _offscreen(live):
            flashed = False   # 最小化/零面积：闪在屏幕边缘只会误导
        else:
            flashed = self.overlay.blink_node(node, rect=live)
        if flashed:   # 拾取确认：闪烁后自动消失
            self._status(f"拾取成功: {path}")
        else:
            self._status(f"拾取成功: {path}（控件当前无可见矩形，无法闪烁确认）")

    def _on_pick_failed(self, msg: str):
        self._set_pick_ui(False)
        self._status(f"拾取失败: {msg}", red=True)

    # ---------------- 冒烟 ----------------

    def smoke(self):
        """自动快照第一个窗口、跑一次查询、截图退出。"""
        if self.win_combo.count() == 0:
            print("SMOKE FAIL: 无顶层窗口")
            QTimer.singleShot(0, QApplication.instance().quit)
            return
        self.win_combo.setCurrentIndex(0)
        self.capture()

        def after_snap():
            if self._root is None:
                QTimer.singleShot(500, after_snap)
                return
            self.xpath_edit.setText("//*")
            self.run_xpath()
            self.grab().save("app_smoke.png")
            print(
                f"SMOKE OK: 窗口={self.win_combo.currentText()!r} "
                f"匹配={self.results.count()} 截图=app_smoke.png"
            )
            QTimer.singleShot(300, QApplication.instance().quit)

        QTimer.singleShot(500, after_snap)


def _install_excepthook():
    """未捕获异常写日志并弹窗。双击运行（pythonw 无控制台）时也能看到错误。"""
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_error.log")

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{text}")
        except OSError:
            pass
        if QApplication.instance() is not None:
            QMessageBox.critical(None, "UIA XPath 测试工具 - 错误", text[-2000:])

    sys.excepthook = hook


def _ensure_dpi():
    """固定 Qt 的缩放取整策略，并压掉启动时那条无法避免的 DPI 警告。

    uiautomation 在 import 时已把进程设为 per-monitor DPI 感知，而 Windows
    只允许设置一次，Qt 随后想升级到 PER_MONITOR_AWARE_V2 必然收到"拒绝访问"，
    所以这里不再尝试设置感知（那次调用注定无效）：进程已是 per-monitor 感知，
    Qt 仍按屏幕 DPI 做逻辑坐标缩放（125% 下 dpr=1.25），代价只是 Qt 逻辑坐标
    ≠ UIA 物理坐标，高亮绘制时需把 rect 除以 dpr。剩下的只有两件事：
    - 固定缩放取整策略，避免系统取整把 dpr 改成 1 或 2，破坏高亮换算；
    - 关掉 qt.qpa.window 的警告，否则控制台会出现吓人的"拒绝访问"。
    """
    # 两个环境变量都必须在 QApplication 之前设置：Qt 构造时读取日志/缩放配置
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window.warning=false")
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")


def main():
    _ensure_dpi()
    app = QApplication(sys.argv)
    _install_excepthook()
    win = MainWindow()
    win.show()
    win.raise_()
    win.activateWindow()
    if "--smoke" in sys.argv:
        win.smoke()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
