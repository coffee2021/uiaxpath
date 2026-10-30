# -*- coding: utf-8 -*-
"""活体控件 → 快照节点映射 + 节点路径生成（纯函数，无副作用）。

- locate_in_snapshot()：把 ControlFromPoint 拿到的活体控件映射回快照树
  中的 UiaNode（RuntimeId → 类型/类名/名字 → 点击坐标，三级逐级放宽）。
- node_path()：生成从快照根到节点的路径（短名 + 同类兄弟索引），
  保证能被 engine.find_all() 精确回解（round-trip）。

阻塞式热键拾取在 uiaxpath.interactive（开发/调试辅助，勿用于生产脚本）。
"""
from __future__ import annotations

from typing import Optional

from uiaxpath.snapshot import UiaNode, short_name


# ---------------- 路径生成 ----------------


def _segment(node: UiaNode) -> str:
    """单个路径段：短名；同类兄弟多于 1 个时附加 [N]（1 基）。"""
    name = short_name(node.control_type)
    if node.parent is None:
        return name
    same = [s for s in node.parent.children if s.control_type == node.control_type]
    if len(same) == 1:
        return name
    return f"{name}[{same.index(node) + 1}]"


def node_path(node: UiaNode, root: UiaNode) -> str:
    """节点相对快照根的路径，如 /Pane[1]/Button[2]。根自身返回 '/'。"""
    if node is root:
        return "/"
    segs = []
    cur = node
    while cur is not None and cur is not root:
        segs.append(_segment(cur))
        cur = cur.parent
    return "/" + "/".join(reversed(segs))


# ---------------- 活体控件 → 快照节点 ----------------


def _iter_all(root: UiaNode):
    yield root
    yield from root.descendants()


def _identity(control) -> tuple | None:
    """控件的类型/类名/名字三元组，用于忽略坐标的身份匹配。

    与快照侧保持一致统一兜成空串：uiautomation 的 ClassName / ControlTypeName
    透传 COM 原始值，部分 Provider 返回 None，而快照里存的是 ""。
    不归一化会让"有歧义再按坐标挑"这一级整体失效。
    """
    try:
        return (
            control.ControlTypeName or "",
            control.ClassName or "",
            control.Name or "",
        )
    except Exception:
        return None


def _smallest_containing(nodes, point) -> Optional[UiaNode]:
    """在一组节点中挑"矩形包含该点且面积最小"的那个（即最内层）。"""
    px, py = point
    best, best_area = None, None
    for node in nodes:
        rect = node.rect
        if not rect:
            continue
        l, t, r, b = rect
        if not (l <= px <= r and t <= py <= b):
            continue
        area = max(0, r - l) * max(0, b - t)
        if best_area is None or area < best_area:
            best, best_area = node, area
    return best


def locate_in_snapshot(root: UiaNode, control,
                       point: Optional[tuple] = None) -> Optional[UiaNode]:
    """把 ControlFromPoint 拿到的活体控件映射回快照节点。

    三级匹配，逐级放宽：
    1. RuntimeId 精确匹配（最快最准）；
    2. 类型+类名+名字（**不含坐标**）：窗口被移动/控件位移后仍可命中，
       只匹配到一个就直接返回；
    3. 点击坐标兜底：同名节点多个时，取包含该点、面积最小的那个；
       名字变了（如标题栏）时，退化为同类型节点里按坐标找最内层。
    """
    rid = None
    try:
        rid = tuple(control.GetRuntimeId() or ()) or None
    except Exception:
        pass
    if rid is not None:
        for node in _iter_all(root):
            if node.runtime_id == rid:
                return node

    ident = _identity(control)
    if ident is None:  # 控件已失效
        return None
    same = [
        n for n in _iter_all(root)
        if (n.control_type, n.class_name, n.name) == ident
    ]
    if len(same) == 1:
        return same[0]
    if same and point is not None:
        hit = _smallest_containing(same, point)
        if hit is not None:
            return hit
    if same:
        return None  # 有歧义又没坐标：宁可让上层报错，也不猜错元素
    if point is not None:
        pool = [n for n in _iter_all(root) if n.control_type == ident[0]]
        return _smallest_containing(pool, point)
    return None
