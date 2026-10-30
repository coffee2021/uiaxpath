# -*- coding: utf-8 -*-
"""XPath 候选生成：从一个节点反推多条定位表达式，按稳定性排序。

生成策略（优先级从高到低）：
  1. AutomationId 定位   //Button[@AutomationId='ok']      —— 最稳定
  2. Name 定位           //Button[@Name='确定']            —— 可读性好
  3. ClassName + Name    //Button[@ClassName='c' and @Name='n']
  4. 锚点 + 相对路径     //Pane[@AutomationId='bar']/Button[2] —— 父锚点稳定
  5. 前一个标签 + 兄弟   //Text[@Name='用户名']/following-sibling::Edit —— 匿名输入框
  6. 绝对结构路径        /Pane[1]/Button[2]                —— 兜底，最脆
每条候选都用引擎实测匹配数，标注是否唯一；排序：唯一优先，再按策略优先级。
实测 0 命中的候选直接丢弃——典型场景是选中根窗口时 `//Window[...]` 必然落空
（`//X` 从根的后代里找，根不是自己的后代），这类项只会让人误以为表达式写错。
"""
from __future__ import annotations

from dataclasses import dataclass

from uiaxpath.engine import find_all, XPathSyntaxError
from uiaxpath.picker import node_path
from uiaxpath.snapshot import UiaNode, short_name


@dataclass
class Candidate:
    expr: str
    count: int          # 实测匹配数
    priority: int       # 策略优先级，小者优
    note: str

    @property
    def unique(self) -> bool:
        return self.count == 1


def _quote(value: str) -> str | None:
    """选一种引号包住 value；两种引号都含有时返回 None（放弃该候选）。"""
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    return None


def _eval(root: UiaNode, expr: str) -> int:
    try:
        return len(find_all(root, expr))
    except (XPathSyntaxError, LookupError):
        return 0


def _anchor_expr(node: UiaNode, root: UiaNode):
    """找最近的带 AutomationId 或 Name 的祖先（不含根自身），返回 (锚点表达式, 祖先节点)。

    根不能作锚点：//X 在根的"后代"中查找，根不是自己的后代，表达式会落空。
    """
    cur = node.parent
    while cur is not None and cur is not root:
        anc_short = short_name(cur.control_type)
        if cur.automation_id:
            q = _quote(cur.automation_id)
            if q:
                return f"//{anc_short}[@AutomationId={q}]", cur
        if cur.name:
            q = _quote(cur.name)
            if q:
                return f"//{anc_short}[@Name={q}]", cur
        cur = cur.parent
    return None, None


def _prev_label_expr(node: UiaNode) -> str | None:
    """匿名输入框的兜底：前一个带名字的兄弟节点 + following-sibling。

    很多登录框、搜索框既没有 Name 也没有 AutomationId，只能靠紧挨着的
    文字标签定位（如 //Text[@Name='用户名']/following-sibling::Edit）。
    """
    parent = node.parent
    if parent is None:
        return None
    try:
        idx = parent.children.index(node)
    except ValueError:
        return None
    short = short_name(node.control_type)
    for prev in reversed(parent.children[:idx]):
        if not prev.name:
            continue
        q = _quote(prev.name)
        if not q:
            continue
        return f"//{short_name(prev.control_type)}[@Name={q}]/following-sibling::{short}"
    return None


def generate(root: UiaNode, node: UiaNode, max_candidates: int = 6) -> list[Candidate]:
    short = short_name(node.control_type)
    cands: list[Candidate] = []
    seen = set()

    def add(expr, priority, note):
        if not expr or expr in seen:
            return
        seen.add(expr)
        count = _eval(root, expr)
        if count == 0:
            return  # 实测落空（如根节点上的 //X：根不是自己的后代），列出来只会误导
        cands.append(Candidate(expr, count, priority, note))

    if node.automation_id:
        q = _quote(node.automation_id)
        if q:
            add(f"//{short}[@AutomationId={q}]", 0, "AutomationId，最稳定")
    if node.name:
        q = _quote(node.name)
        if q:
            add(f"//{short}[@Name={q}]", 1, "Name 定位，可读性好")
    if node.class_name and node.name:
        qc, qn = _quote(node.class_name), _quote(node.name)
        if qc and qn:
            add(f"//{short}[@ClassName={qc} and @Name={qn}]", 2, "ClassName+Name 组合")
    anc_expr, anc = _anchor_expr(node, root)
    if anc_expr and anc is not None:
        rel = node_path(node, anc)  # 形如 /Button[2]
        add(anc_expr + rel, 3, "锚点+相对路径，抗兄弟变动")
    if (not node.automation_id and not node.name
            and node.control_type in ("EditControl", "DocumentControl")):
        # 匿名输入框：只能靠旁边的标签文字定位
        label_expr = _prev_label_expr(node)
        if label_expr:
            add(label_expr, 3, "前一个标签 + following-sibling")
    add(node_path(node, root), 4, "绝对路径，兜底最脆")

    cands.sort(key=lambda c: (not c.unique, c.priority))
    return cands[:max_candidates]
