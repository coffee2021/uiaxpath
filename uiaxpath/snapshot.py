# -*- coding: utf-8 -*-
"""快照模型：把 UIA 控件树一次性捕获为纯 Python 节点树。

设计要点：
- XPath 查询只跑在快照上（纯内存，微秒级），不触碰 COM；
- 快照与控件树视图、高亮、XPath 引擎共享同一份数据；
- 每个节点保留 control 引用，供后续高亮/点击时回到活体控件。
"""
from __future__ import annotations

from typing import Optional


def short_name(control_type: str) -> str:
    """ButtonControl -> Button"""
    return control_type[:-7] if control_type.endswith("Control") else control_type


class UiaNode:
    __slots__ = (
        "name", "automation_id", "class_name", "control_type",
        "rect", "process_id", "runtime_id", "children", "parent", "control",
        "_extra",
    )

    def __init__(self, control):
        self.control = control  # 活体控件引用（可能失效，使用时需容错）
        # AutomationId / ClassName / ControlTypeName 由 uiautomation 直接透传底层值，
        # 部分 Provider 会返回 None：统一兜成空串，避免下游 len()/lower() 崩掉
        self.name: str = control.Name or ""
        self.automation_id: str = control.AutomationId or ""
        self.class_name: str = control.ClassName or ""
        self.control_type: str = control.ControlTypeName or ""
        self.process_id: int = control.ProcessId
        try:
            # RuntimeId 是 UIA 元素的唯一标识，拾取器靠它映射回快照
            self.runtime_id = tuple(control.GetRuntimeId() or ()) or None
        except Exception:
            self.runtime_id = None
        try:
            r = control.BoundingRectangle
            self.rect = (r.left, r.top, r.right, r.bottom)
        except Exception:
            self.rect = None
        self.children: list[UiaNode] = []
        self.parent: Optional[UiaNode] = None
        self._extra: dict[str, object] = {}   # 扩展属性缓存（首次访问才读活体控件）

    # XPath 属性访问入口：@Name / @AutomationId / @ClassName / @ControlType ...
    _ATTR_MAP = {
        "Name": "name",
        "AutomationId": "automation_id",
        "ClassName": "class_name",
        "ControlType": "control_type",
        "ProcessId": "process_id",
    }

    # 扩展属性：建快照时不读（每条都是一次跨进程 COM，会成倍拖慢建树），
    # 只有 XPath 真正引用时才按需读取并缓存到 _extra。
    _EXTRA_ATTRS = (
        "LocalizedControlType",
        "IsEnabled",
        "IsOffscreen",
        "IsKeyboardFocusable",
        "HelpText",
        "AcceleratorKey",
        "FrameworkId",
        "NativeWindowHandle",
    )

    def attr(self, attr_name: str):
        field = self._ATTR_MAP.get(attr_name)
        if field is not None:
            return getattr(self, field)
        if attr_name in self._EXTRA_ATTRS:
            return self._extra_attr(attr_name)
        raise KeyError(attr_name)

    def _extra_attr(self, attr_name: str):
        cache = getattr(self, "_extra", None)
        if cache is None:  # 测试里用 __new__ 造的合成节点没有 _extra
            cache = self._extra = {}
        if attr_name not in cache:
            try:
                value = getattr(self.control, attr_name)
            except Exception:
                value = ""  # 控件已失效：按空值处理，便于查询时跳过
            cache[attr_name] = value
        return cache[attr_name]

    def descendants(self):
        """先序遍历的后代节点（不含自身），迭代实现避免深树爆栈。"""
        stack = list(reversed(self.children))
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    def __repr__(self):
        return f"<UiaNode {self.control_type} Name={self.name!r}>"


def snapshot(control, max_depth: int = 60) -> UiaNode:
    """以 control 为根构建快照树。max_depth 防御病态深树。"""
    root = UiaNode(control)
    # (uia_control, uia_node, depth) 迭代构建，避免递归
    stack = [(control, root, 0)]
    while stack:
        src, dst, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            children = src.GetChildren()
        except Exception:
            continue  # 控件已失效等情况，按无子节点处理
        for child_control in children:
            try:
                child_node = UiaNode(child_control)
            except Exception:
                # 单个子控件在遍历途中失效，只跳过该分支；
                # 不让它把整棵树的建树结果一起带走
                continue
            child_node.parent = dst
            dst.children.append(child_node)
            stack.append((child_control, child_node, depth + 1))
    return root


def count_nodes(root: UiaNode) -> int:
    return 1 + sum(1 for _ in root.descendants())
