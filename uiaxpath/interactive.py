# -*- coding: utf-8 -*-
"""交互式拾取：阻塞等待热键，把鼠标下的控件映射回快照节点。

定位：开发/调试辅助。阻塞轮询热键 + 跨进程 COM，不适合嵌进
自动化脚本的主流程；生产代码请用 snapshot + find_all 组合。

GUI 工具（app.py）用自己的"工具栏按钮 + 左 Ctrl"方案，
只复用 picker.locate_in_snapshot，不依赖本模块。

is_pressed / get_point 可注入，便于自动化测试。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Callable, Optional

import uiautomation as auto

from uiaxpath.picker import locate_in_snapshot, node_path
from uiaxpath.snapshot import UiaNode

_user32 = ctypes.windll.user32

VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12}


def _hotkey_pressed(hotkey) -> bool:
    return all(_user32.GetAsyncKeyState(VK[k]) & 0x8000 for k in hotkey)


def _cursor_point():
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def wait_for_pick(hotkey=("ctrl", "shift"), poll=0.03,
                  is_pressed: Optional[Callable[[], bool]] = None,
                  get_point: Optional[Callable[[], tuple]] = None):
    """阻塞直到热键按下，返回此刻的鼠标坐标。等待松开后再返回，避免连发。"""
    pressed = is_pressed or (lambda: _hotkey_pressed(hotkey))
    point = get_point or _cursor_point
    while not pressed():
        time.sleep(poll)
    x, y = point()
    while pressed():
        time.sleep(poll)
    return x, y


def pick(root: UiaNode, hotkey=("ctrl", "shift"),
         is_pressed: Optional[Callable[[], bool]] = None,
         get_point: Optional[Callable[[], tuple]] = None,
         on_pick: Optional[Callable] = None):
    """拾取一个元素，返回 (node, path)；未匹配到快照节点时 node 为 None。"""
    x, y = wait_for_pick(hotkey, is_pressed=is_pressed, get_point=get_point)
    control = auto.ControlFromPoint(x, y)
    if control is None:
        return None, None
    node = locate_in_snapshot(root, control, point=(x, y))
    path = node_path(node, root) if node is not None else None
    if on_pick:
        on_pick(x, y, control, node, path)
    return node, path
