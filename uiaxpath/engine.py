# -*- coding: utf-8 -*-
"""XPath 引擎（快照版）。

支持语法（对标 FlaUI XPath 子集，并有三处扩展）：
  /  //  *  NodeTest(短名或全名, 如 Button / ButtonControl)
  ..（父轴简写）、完整轴写法 child:: / descendant:: / parent:: /
  ancestor:: / following-sibling:: / preceding-sibling::
  |（并集，仅顶层）
  [@Attr="v"]  [@Attr!="v"]  [@Attr]  [not(条件)]
  [@Attr>"v"]（> >= < <=，两侧都能转数字则按数值比，否则按字符串比）
  [contains(@Attr,"v")]  [starts-with(@Attr,"v")]  [ends-with(@Attr,"v")]  [N]  [-N]  [last()]  [position()>N]
  字符串函数可包裹属性：lower-case(@A) / upper-case(@A) / normalize-space(@A)，
  既可单独比较，也可作为 contains 等的第一个参数。
  相对路径谓词：//Pane[.//Button[@Name="x"]]（容器"内含某控件"消歧）
  同一 [] 内用 and / or 连接（and 优先）；多个 [] 可叠加，
  且**按书写顺序依次作用**：`//Button[1][@Name='x']` 是先取第 1 个、再看它叫不叫 x。
  关键字（and/or/not/contains/last/position 等）、轴名与节点测试**全部大小写不敏感**：
  `AND`、`ANCESTOR::`、`//button` 都能正常用，等价于全小写的规范写法。
索引语义：[N] 按"每个上下文节点"分别计数（贴近标准 XPath），
而非 uiax 的全局第 N 个。
负索引为**非标准扩展**：[-1] 表示最后一个，[-2] 倒数第二；标准 XPath 下
[-2] 恒为假，表达式移植到其他实现会静默返回空，请知悉。
反向轴语义：ancestor / preceding-sibling 的候选按"从近到远"排列，
[1] 取最近的父节点/紧邻的前一个兄弟（与标准 XPath 反向轴一致）。
属性：Name/AutomationId/ClassName/ControlType/ProcessId 来自快照；
LocalizedControlType/IsEnabled/IsOffscreen/IsKeyboardFocusable/
HelpText/AcceleratorKey/FrameworkId/NativeWindowHandle 按需走活体控件并缓存。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from uiaxpath.snapshot import UiaNode, short_name


class XPathSyntaxError(Exception):
    pass


# ---------------- 词法 ----------------

_TOKEN_RE = re.compile(
    r"""
      (?P<DSLASH>//)
    | (?P<SLASH>/)
    | (?P<DOTDOT>\.\.)
    | (?P<DCOLON>::)
    | (?P<DOT>\.)
    | (?P<NE>!=)
    | (?P<GE>>=) | (?P<LE><=)
    | (?P<GT>>) | (?P<LT><)
    | (?P<MINUS>-)
    | (?P<PIPE>\|)
    | (?P<ATTR>@[A-Za-z_][\w\-]*)
    | (?P<STRING>"[^"]*"|'[^']*')
    | (?P<NUMBER>\d+)
    | (?P<NAME>[A-Za-z_][\w\-]*|\*)
    | (?P<LBRACKET>\[) | (?P<RBRACKET>\])
    | (?P<EQUAL>=)
    | (?P<LPAREN>\() | (?P<RPAREN>\)) | (?P<COMMA>,)
    | (?P<WS>\s+)
    """,
    re.VERBOSE,
)


def _tokenize(expr: str):
    tokens = []
    pos = 0
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            raise XPathSyntaxError(f"无法识别的字符: {expr[pos]!r} (位置 {pos})")
        pos = m.end()
        kind = m.lastgroup
        if kind == "WS":
            continue
        tokens.append((kind, m.group()))
    return tokens


# ---------------- 语法 ----------------


@dataclass
class Cond:
    kind: str            # eq/ne/gt/ge/lt/le/contains/starts-with/ends-with/exists/not/path/and/or
    attr: str = ""
    value: str = ""
    fn: str = ""         # "" | lower-case | upper-case | normalize-space
    subs: list = field(default_factory=list)   # and/or/not 的子条件；path 时为 list[Step]


@dataclass
class Step:
    axis: str            # "child" | "descendant" | "parent" | "ancestor" | "following-sibling" | "preceding-sibling"
    name: str            # "*" | 节点名（短名或全名）
    # 谓词按**书写顺序**保存，逐条作用（标准 XPath 语义）：
    #   ("cond", Cond)                       —— 属性/相对路径条件
    #   ("idx", n) | ("last",) | ("pos", op, n) —— 位置索引
    # 两者必须共用一个有序列表，否则 [1][@Name='x'] 会被算成"先过滤再取第 1 个"。
    preds: list = field(default_factory=list)


# 完整轴写法（axisname::NodeTest）的轴名映射
_AXES = {
    "child": "child",
    "descendant": "descendant",
    "parent": "parent",
    "ancestor": "ancestor",
    "following-sibling": "following-sibling",
    "preceding-sibling": "preceding-sibling",
}

# 允许包裹属性的字符串函数
_STR_FNS = ("lower-case", "upper-case", "normalize-space")

# 比较运算符 token → 条件类型
_OPS = {"EQUAL": "eq", "NE": "ne", "GT": "gt", "GE": "ge", "LT": "lt", "LE": "le"}

# 合法属性名（快照核心属性 + 扩展属性），解析期据此校验拼写
_ALL_ATTRS = frozenset(UiaNode._ATTR_MAP) | frozenset(UiaNode._EXTRA_ATTRS)


def _map_axis(name: str) -> str:
    # 轴名大小写不敏感：ANCESTOR:: 与 ancestor:: 等价
    axis = _AXES.get(name.lower())
    if axis is None:
        raise XPathSyntaxError(
            f"不支持的轴: {name!r}（可用 {'/'.join(_AXES)}）"
        )
    return axis


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def peek2(self):
        return self.tokens[self.pos + 1] if self.pos + 1 < len(self.tokens) else (None, None)

    def next(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def expect(self, kind):
        tok = self.next()
        if tok[0] != kind:
            raise XPathSyntaxError(f"期望 {kind}，实际 {tok[1]!r}")
        return tok[1]

    def parse(self) -> list[list[Step]]:
        """返回若干条备选路径（| 分隔），任一路径命中即算命中。"""
        alts = [self.parse_path()]
        while self.peek()[0] == "PIPE":
            self.next()
            alts.append(self.parse_path())
        return alts

    def parse_path(self) -> list[Step]:
        steps = []
        axis = None
        start = self.pos
        while self.pos < len(self.tokens) and self.peek()[0] != "PIPE":
            kind, val = self.peek()
            if kind in ("SLASH", "DSLASH"):
                axis = "descendant" if kind == "DSLASH" else "child"
                self.next()
                continue
            if kind == "DOTDOT":  # .. 父轴，本身就是一个 step
                self.next()
                steps.append(Step(axis="parent", name="*"))
                axis = None
                continue
            if kind == "NAME" and self.peek2()[0] == "DCOLON":
                # 完整轴写法：ancestor::Pane、following-sibling::Edit 等；
                # 覆盖 / 或 // 设置的默认轴（轴写法优先级更高）
                self.next()   # 轴名
                self.next()   # ::
                axis = _map_axis(val)
                continue
            if kind != "NAME":
                raise XPathSyntaxError(f"此处应为节点名，实际 {val!r}")
            steps.append(self.parse_step(axis or "child"))
            axis = None
        if axis is not None:
            # 末尾悬空的 "/"、"//" 或 "axis::"（如 //Button/、//、ancestor::）
            if not steps and self.pos - start == 1 and self.tokens[start][0] == "SLASH":
                return []   # 单独的 "/" 表示快照根自身
            raise XPathSyntaxError("表达式不能以 /、// 或轴名结尾")
        if not steps:
            raise XPathSyntaxError("表达式为空")
        return steps

    def parse_step(self, axis) -> Step:
        _, name = self.next()  # NAME 或 *
        step = Step(axis=axis, name=name)
        while self.peek()[0] == "LBRACKET":
            self.next()
            kind, val = self.peek()
            if kind == "NUMBER":
                step.preds.append(("idx", int(self.next()[1])))
                self.expect("RBRACKET")
                continue
            if kind == "MINUS" and self.peek2()[0] == "NUMBER":
                self.next()
                step.preds.append(("idx", -int(self.next()[1])))
                self.expect("RBRACKET")
                continue
            if kind == "NAME" and val.lower() == "last" and self.peek2()[0] == "LPAREN":
                self.next(); self.expect("LPAREN"); self.expect("RPAREN")
                step.preds.append(("last",))
                self.expect("RBRACKET")
                continue
            if kind == "NAME" and val.lower() == "position" and self.peek2()[0] == "LPAREN":
                self.next(); self.expect("LPAREN"); self.expect("RPAREN")
                op = self.peek()[0]
                if op not in ("GT", "GE", "LT", "LE"):
                    raise XPathSyntaxError("position() 只支持与 > >= < <= 数字比较")
                self.next()
                step.preds.append(("pos", op, int(self.expect("NUMBER"))))
                self.expect("RBRACKET")
                continue
            step.preds.append(("cond", self.parse_cond_or()))
            self.expect("RBRACKET")
        return step

    def parse_value_expr(self) -> tuple[str, str]:
        """谓词里的取值：@Attr 或 fn(@Attr)，返回 (属性名, 函数名)。

        属性名在**解析期**就校验：否则 `//Edit[@Foo]` 在没有 Edit 的树上会
        静默返回 0，拼写错误看起来像"没匹配到"。
        """
        kind, val = self.peek()
        if kind == "ATTR":
            return self._checked_attr(self.next()[1][1:]), ""
        if kind == "NAME" and val.lower() in _STR_FNS and self.peek2()[0] == "LPAREN":
            fn = self.next()[1].lower()
            self.expect("LPAREN")
            attr = self._checked_attr(self.expect("ATTR")[1:])
            self.expect("RPAREN")
            return attr, fn
        raise XPathSyntaxError(f"此处应为 @属性 或字符串函数，实际 {val!r}")

    @staticmethod
    def _checked_attr(attr: str) -> str:
        if attr not in _ALL_ATTRS:
            raise XPathSyntaxError(f"未知属性 @{attr}")
        return attr

    def parse_literal(self) -> str:
        """比较右侧的字面量：字符串、整数，或带负号的整数（XPath 允许裸数字）。"""
        kind, val = self.peek()
        if kind == "STRING":
            return self.next()[1][1:-1]
        if kind == "NUMBER":
            return self.next()[1]
        if kind == "MINUS" and self.peek2()[0] == "NUMBER":
            self.next()
            return "-" + self.next()[1]
        raise XPathSyntaxError(f"此处应为字符串或数字，实际 {val!r}")

    def parse_rel_path(self) -> list[Step]:
        """谓词里的相对路径（必须以 . 开头）：.//X / ./X，返回 Step 列表。"""
        self.expect("DOT")
        steps = []
        axis = None
        while True:
            kind, val = self.peek()
            if kind in ("SLASH", "DSLASH"):
                axis = "descendant" if kind == "DSLASH" else "child"
                self.next()
                continue
            if kind == "DOTDOT":
                self.next()
                steps.append(Step(axis="parent", name="*"))
                axis = None
                continue
            if kind == "NAME" and self.peek2()[0] == "DCOLON":
                self.next(); self.next()
                axis = _map_axis(val)
                continue
            if kind == "NAME":
                steps.append(self.parse_step(axis or "child"))
                axis = None
                continue
            break
        if axis is not None:
            raise XPathSyntaxError("相对路径不能以 /、// 或轴名结尾")
        if not steps:
            raise XPathSyntaxError("相对路径谓词不能只有 .，应为 .//X 或 ./X")
        return steps

    def parse_cond_or(self) -> Cond:
        node = self.parse_cond_and()
        while self.peek()[0] == "NAME" and self.peek()[1].lower() == "or":
            self.next()
            node = Cond("or", subs=[node, self.parse_cond_and()])
        return node

    def parse_cond_and(self) -> Cond:
        node = self.parse_cond_atom()
        while self.peek()[0] == "NAME" and self.peek()[1].lower() == "and":
            self.next()
            node = Cond("and", subs=[node, self.parse_cond_atom()])
        return node

    def parse_cond_atom(self) -> Cond:
        kind, val = self.peek()
        if kind == "DOT":  # 相对路径谓词：容器"内含某控件"
            return Cond("path", subs=self.parse_rel_path())
        if kind == "ATTR" or (kind == "NAME" and val.lower() in _STR_FNS
                              and self.peek2()[0] == "LPAREN"):
            attr, fn = self.parse_value_expr()
            op = self.peek()[0]
            if op in _OPS:
                self.next()
                return Cond(_OPS[op], attr, self.parse_literal(), fn)
            return Cond("exists", attr, fn=fn)
        if kind == "NAME" and val.lower() in ("contains", "starts-with", "ends-with"):
            name = self.next()[1].lower()
            self.expect("LPAREN")
            attr, fn = self.parse_value_expr()
            self.expect("COMMA")
            value = self.expect("STRING")[1:-1]
            self.expect("RPAREN")
            return Cond(name, attr, value, fn)
        if kind == "NAME" and val.lower() == "not":
            # not(条件)：内部支持完整的 and/or 组合与相对路径
            self.next()
            self.expect("LPAREN")
            inner = self.parse_cond_or()
            self.expect("RPAREN")
            return Cond("not", subs=[inner])
        if val is None:
            raise XPathSyntaxError("表达式在 [ 之后意外结束")
        raise XPathSyntaxError(f"无法解析的条件: {val!r}")


def parse(expr: str) -> list[list[Step]]:
    return _Parser(_tokenize(expr.strip())).parse()


# ---------------- 执行 ----------------


def _node_test(node: UiaNode, name: str) -> bool:
    if name == "*":
        return True
    # 节点名大小写不敏感：//button 与 //Button 等价（短名、全名都按小写比较）
    low = name.lower()
    ct = node.control_type
    return ct.lower() == low or short_name(ct).lower() == low


def _as_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


_CMP = ("gt", "ge", "lt", "le")


def _apply_fn(text: str, fn: str) -> str:
    if fn == "lower-case":
        return text.lower()
    if fn == "upper-case":
        return text.upper()
    if fn == "normalize-space":
        return " ".join(text.split())
    return text


def _compare(a_text: str, b_text: str, kind: str) -> bool:
    """两侧都能转整数时按数值比，否则退化为字符串比较。"""
    try:
        a, b = int(a_text), int(b_text)
    except ValueError:
        a, b = a_text, b_text
    if kind == "gt":
        return a > b
    if kind == "ge":
        return a >= b
    if kind == "lt":
        return a < b
    return a <= b


def _cond_ok(node: UiaNode, cond: Cond) -> bool:
    if cond.kind == "and":
        return all(_cond_ok(node, c) for c in cond.subs)
    if cond.kind == "or":
        return any(_cond_ok(node, c) for c in cond.subs)
    if cond.kind == "not":  # 先于此后的 attr 取值：not 自身不带 attr
        return not _cond_ok(node, cond.subs[0])
    if cond.kind == "path":  # 相对路径谓词：以本节点为根的子树里是否有命中
        return bool(_eval_path(node, cond.subs))
    try:
        value = node.attr(cond.attr)
    except KeyError:
        raise XPathSyntaxError(f"未知属性 @{cond.attr}")
    if cond.kind == "exists":
        return bool(_apply_fn(_as_text(value), cond.fn)) if cond.fn else bool(value)
    text = _as_text(value)
    if cond.fn:  # 有字符串函数时统一走文本比较（布尔属性按 true/false 文本）
        text = _apply_fn(text, cond.fn)
    elif isinstance(value, bool):   # 布尔属性允许写 "True"/"true"/"1"
        want = cond.value.strip().lower() in ("true", "1")
        if cond.kind in ("eq", "ne"):
            return (value == want) if cond.kind == "eq" else (value != want)
    if cond.kind == "eq":
        return text == cond.value
    if cond.kind == "ne":
        return text != cond.value
    if cond.kind == "contains":
        return cond.value in text
    if cond.kind == "starts-with":
        return text.startswith(cond.value)
    if cond.kind == "ends-with":
        return text.endswith(cond.value)
    if cond.kind in _CMP:
        return _compare(text, cond.value, cond.kind)
    raise XPathSyntaxError(f"未知条件类型 {cond.kind}")


def find_all(root: UiaNode, expr: str) -> list[UiaNode]:
    out, seen = [], set()
    for steps in parse(expr):   # | 并集：逐条路径求值后去重
        for node in _eval_path(root, steps):
            if id(node) not in seen:
                seen.add(id(node))
                out.append(node)
    return out


def _apply_index(matched: list[UiaNode], item: tuple) -> list[UiaNode]:
    """对单个上下文节点已匹配的列表施加一个索引项。

    正索引 1 基（XPath 惯例）；负索引是新添的非标准扩展（-1=最后一个），
    标准 XPath 下负数比较恒假，移植出去会得到空结果。
    """
    tag = item[0]
    if tag == "idx":
        n = item[1]
        if n > 0:
            return [matched[n - 1]] if n <= len(matched) else []
        if n < 0:
            return [matched[n]] if -n <= len(matched) else []
        return []
    if tag == "last":
        return [matched[-1]] if matched else []
    _, op, n = item
    keep = {"GT": lambda i: i > n, "GE": lambda i: i >= n,
            "LT": lambda i: i < n, "LE": lambda i: i <= n}[op]
    return [m for i, m in enumerate(matched, 1) if keep(i)]


def _sibling_positions(parent, cache):
    """id(子节点) → 下标；同一父节点在一轮求值里只建一次。

    兄弟轴若对每个上下文节点都调 list.index，最坏是 O(n²)；
    用缓存后 `//*` 上的 following-sibling 查询退化为 O(n)。
    """
    m = cache.get(id(parent))
    if m is None:
        m = {id(c): i for i, c in enumerate(parent.children)}
        cache[id(parent)] = m
    return m


def _eval_path(root: UiaNode, steps: list[Step]) -> list[UiaNode]:
    if not steps:  # "/"：快照根自身
        return [root]
    current = [root]
    pos_cache: dict[int, dict[int, int]] = {}
    for step in steps:
        matched_all, seen = [], set()
        for ctx in current:
            if step.axis == "parent":
                cands = [ctx.parent] if ctx.parent is not None else []
            elif step.axis == "ancestor":
                # 反向轴：从近到远，[1] = 父节点
                cands = []
                cur = ctx.parent
                while cur is not None:
                    cands.append(cur)
                    cur = cur.parent
            elif step.axis == "following-sibling":
                if ctx.parent is not None:
                    sibs = ctx.parent.children
                    pos = _sibling_positions(ctx.parent, pos_cache)[id(ctx)]
                    cands = sibs[pos + 1:]
                else:
                    cands = []
            elif step.axis == "preceding-sibling":
                # 反向轴：从近到远，[1] = 紧邻的前一个兄弟
                if ctx.parent is not None:
                    sibs = ctx.parent.children
                    pos = _sibling_positions(ctx.parent, pos_cache)[id(ctx)]
                    cands = sibs[:pos][::-1]
                else:
                    cands = []
            elif step.axis == "descendant":
                cands = ctx.descendants()
            else:
                cands = ctx.children
            matched = [c for c in cands if _node_test(c, step.name)]
            # 谓词按书写顺序逐条作用：[1][@Name='x'] 是"先取第 1 个再按 Name 过滤"
            for pred in step.preds:
                if pred[0] == "cond":
                    cond = pred[1]
                    matched = [m for m in matched if _cond_ok(m, cond)]
                else:
                    matched = _apply_index(matched, pred)
            for m in matched:
                if id(m) not in seen:
                    seen.add(id(m))
                    matched_all.append(m)
        current = matched_all
    return current


def find(root: UiaNode, expr: str) -> UiaNode:
    results = find_all(root, expr)
    if not results:
        raise LookupError(f"未找到元素: {expr}")
    if len(results) > 1:
        raise LookupError(f"找到 {len(results)} 个元素: {expr}")
    return results[0]
