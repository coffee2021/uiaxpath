# -*- coding: utf-8 -*-
"""uiaxpath 最小实现回归测试：
A. 合成树单元测试（不依赖真实 UI，确定性强）
B. 记事本活体回归（与 uiax 上轮实测结果对照 + 性能对比）
"""
import os
import sys
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from uiaxpath import snapshot, find_all, find, XPathSyntaxError
from uiaxpath.snapshot import UiaNode, count_nodes

PASS, FAIL = 0, 0

def check(label, actual, expected):
    global PASS, FAIL
    ok = actual == expected
    PASS, FAIL = PASS + ok, FAIL + (not ok)
    print(f"[{'OK' if ok else 'FAIL'}] {label}: 期望 {expected!r}, 实际 {actual!r}")

# ============ A. 合成树单元测试 ============
print("===== A. 合成树单元测试 =====")

class FakeControl:
    """模拟 uiautomation Control 的属性接口"""
    def __init__(self, ct, name="", aid="", cls=""):
        self.ControlTypeName = ct
        self.Name = name
        self.AutomationId = aid
        self.ClassName = cls
        self.ProcessId = 1234
        self.BoundingRectangle = None

def N(ct, name="", aid="", cls="", children=()):
    node = UiaNode.__new__(UiaNode)
    node.control = None
    node.name, node.automation_id, node.class_name = name, aid, cls
    node.control_type, node.process_id, node.rect = ct, 0, None
    node.children = list(children)
    node.parent = None
    for c in node.children:
        c.parent = node
    return node

#   Window(主窗口)
#   ├─ Pane(p1)
#   │   ├─ Button OK     (aid=btn1)
#   │   └─ Button Cancel (cls=BtnCls)
#   ├─ Pane(p2)
#   │   └─ Button OK     (aid=btn2)
#   └─ Edit(输入框)
tree = N("WindowControl", "主窗口", children=(
    N("PaneControl", "p1", children=(
        N("ButtonControl", "OK", aid="btn1"),
        N("ButtonControl", "Cancel", cls="BtnCls"),
    )),
    N("PaneControl", "p2", children=(
        N("ButtonControl", "OK", aid="btn2"),
    )),
    N("EditControl", "输入框"),
))

names = lambda nodes: [n.name for n in nodes]

check("短名 //Button", names(find_all(tree, "//Button")), ["OK", "Cancel", "OK"])
check("全名 //ButtonControl", len(find_all(tree, "//ButtonControl")), 3)
check("子轴 /Pane", names(find_all(tree, "/Pane")), ["p1", "p2"])
check("通配 /*", len(find_all(tree, "/*")), 3)
check("等值 //Button[@Name='OK']", len(find_all(tree, "//Button[@Name='OK']")), 2)
check("contains //*[contains(@Name,'入')]", names(find_all(tree, "//*[contains(@Name,'入')]")), ["输入框"])
check("and 组合", names(find_all(tree, "//Button[@AutomationId='btn2' and @Name='OK']")), ["OK"])
check("多谓词叠加 [][]", names(find_all(tree, "//*[@ClassName='BtnCls'][contains(@Name,'Can')]")), ["Cancel"])
check("存在性 //Button[@AutomationId]", len(find_all(tree, "//Button[@AutomationId]")), 2)
check("per-parent 索引 //Pane/*[1]", names(find_all(tree, "//Pane/*[1]")), ["OK", "OK"])
check("索引 [2]", names(find_all(tree, "/Pane[2]/Button")), ["OK"])
check("索引越界", find_all(tree, "//Button[9]"), [])
check("find 唯一", find(tree, "//Edit").name, "输入框")
# 二期语法：!= / or / starts-with / .. / |
check("不等于 //Button[@Name!='OK']", names(find_all(tree, "//Button[@Name!='OK']")), ["Cancel"])
check("or 组合", len(find_all(tree, "//Button[@Name='OK' or @Name='Cancel']")), 3)
check("or + contains", names(find_all(tree, "//Button[contains(@Name,'Ca') or @ClassName='BtnCls']")), ["Cancel"])
check("starts-with", names(find_all(tree, "//Button[starts-with(@Name,'C')]")), ["Cancel"])
check("父轴 //Button/..", names(find_all(tree, "//Button/..")), ["p1", "p2"])
check("父轴 + 索引 //Button[1]/..", names(find_all(tree, "//Button[1]/..")), ["p1"])  # [1] 按上下文计数，只留全局第 1 个 Button
check("并集 //Button | //Edit", len(find_all(tree, "//Button | //Edit")), 4)
check("并集去重 //Edit | //*[@Name='输入框']", len(find_all(tree, "//Edit | //*[@Name='输入框']")), 1)
check("合成节点查扩展属性不报错", find_all(tree, '//Button[@IsEnabled="true"]'), [])
# 三期语法：ends-with / not / 完整轴写法
check("ends-with", names(find_all(tree, "//Button[ends-with(@Name,'el')]")), ["Cancel"])
check("not(存在性)", names(find_all(tree, "//Button[not(@AutomationId)]")), ["Cancel"])
check("not(等值)", names(find_all(tree, "//Button[not(@Name='OK')]")), ["Cancel"])
check("not 嵌套 or", find_all(tree, "//Button[not(@Name='OK' or @Name='Cancel')]"), [])
check("parent:: 等价 ..", names(find_all(tree, "//Button[1]/parent::*")), ["p1"])
check("ancestor:: 近→远", names(find_all(tree, "//Button[@AutomationId='btn2']/ancestor::*")), ["p2", "主窗口"])
check("ancestor::Pane", names(find_all(tree, "//Button[@AutomationId='btn1']/ancestor::Pane")), ["p1"])
check("following-sibling", names(find_all(tree, "//Button[@AutomationId='btn1']/following-sibling::Button")), ["Cancel"])
check("following-sibling::*[1] 紧邻", names(find_all(tree, "/Pane[1]/following-sibling::*[1]")), ["p2"])
check("preceding-sibling 近→远", names(find_all(tree, "//Edit/preceding-sibling::Pane")), ["p2", "p1"])
check("preceding-sibling::*[1] 紧邻", names(find_all(tree, "//Edit/preceding-sibling::*[1]")), ["p2"])
# 四期语法：相对路径谓词 / 字符串函数 / 关键字大小写 / 索引扩展
check("相对路径 //Pane[.//Button]", names(find_all(tree, "//Pane[.//Button]")), ["p1", "p2"])
check("相对路径 not(.//Button)", names(find_all(tree, "//Pane[not(.//Button)]")), [])
check("相对路径子轴 [./Button]", names(find_all(tree, "//Pane[./Button[@AutomationId='btn1']]")), ["p1"])
check("相对路径带属性 //Pane[.//Button[@Name='Cancel']]", names(find_all(tree, "//Pane[.//Button[@Name='Cancel']]")), ["p1"])
check("lower-case 比较", names(find_all(tree, "//Button[lower-case(@Name)='cancel']")), ["Cancel"])
check("upper-case+contains", names(find_all(tree, "//Button[contains(upper-case(@Name),'CAN')]")), ["Cancel"])
check("normalize-space", len(find_all(tree, "//Button[normalize-space(@Name)='OK']")), 2)
# 关键字、轴名与节点名大小写不敏感；仅属性值保持大小写敏感
check("大写 AND", names(find_all(tree, "//Button[@Name='OK' AND @AutomationId='btn2']")), ["OK"])
check("大写 OR", len(find_all(tree, "//Button[@Name='OK' OR @Name='Cancel']")), 3)
check("大写 NOT", names(find_all(tree, "//Button[NOT(@AutomationId)]")), ["Cancel"])
check("大写轴名 ANCESTOR::", names(find_all(tree, "//Button[@AutomationId='btn1']/ANCESTOR::Pane")), ["p1"])
check("大写函数 Contains", names(find_all(tree, "//Button[Contains(@Name,'Ca')]")), ["Cancel"])
check("大写 LAST()", names(find_all(tree, "//Pane[LAST()]")), ["p2"])
check("节点名大小写不敏感 //button", names(find_all(tree, "//button")), ["OK", "Cancel", "OK"])
check("节点名大小写不敏感 //BUTTONCONTROL", len(find_all(tree, "//BUTTONCONTROL")), 3)
check("last()", names(find_all(tree, "//Pane[last()]")), ["p2"])
check("position()>1", names(find_all(tree, "//Pane[position()>1]")), ["p2"])
check("负索引 [-1]", names(find_all(tree, "//Pane[-1]")), ["p2"])
check("负索引 [-2]", names(find_all(tree, "//Pane[-2]")), ["p1"])
check("负索引越界", find_all(tree, "//Pane[-3]"), [])
check("负索引 //Button[-2]", names(find_all(tree, "//Button[-2]")), ["Cancel"])
check("数值比较 >=", len(find_all(tree, "//Button[@ProcessId>=0]")), 3)
check("字符串比较回退 >", names(find_all(tree, "//Button[@Name>'M']")), ["OK", "OK"])
# 多个谓词按书写顺序依次作用（标准 XPath）：[1] 先取第 1 个，再接后续条件
check("谓词顺序 [1][@Name] 先取第1个", find_all(tree, "//Button[1][@Name='Cancel']"), [])
check("谓词顺序 [1][@Name] 命中第1个", names(find_all(tree, "//Button[1][@Name='OK']")), ["OK"])
check("谓词顺序 [@Name][1] 先过滤再取首", names(find_all(tree, "//Button[@Name='OK'][1]")), ["OK"])


class FakeExtra:
    """模拟带扩展属性的 uiautomation 控件"""
    Name, AutomationId, ClassName = "OK", "btn1", "Button"
    ControlTypeName, ProcessId, BoundingRectangle = "ButtonControl", 1, None
    IsEnabled, IsOffscreen = True, False
    LocalizedControlType, FrameworkId, NativeWindowHandle = "按钮", "Win32", 4321

    def GetRuntimeId(self):
        return [1, 2]


fake = UiaNode(FakeExtra())
check("扩展属性 IsEnabled", fake.attr("IsEnabled"), True)
check("扩展属性 LocalizedControlType", fake.attr("LocalizedControlType"), "按钮")
check("扩展属性按需读取并缓存", (fake.attr("FrameworkId"), fake._extra.get("FrameworkId")), ("Win32", "Win32"))
try:
    find(tree, "//Button[@Name='OK']")
    check("find 多结果报错", "无异常", "LookupError")
except LookupError:
    check("find 多结果报错", "LookupError", "LookupError")
for bad in ["//Button[@Name=", "//Button[", "//*[or @Name='x']", "//*[foo(@Name,'O')]", "//Button/unknown::X", "//Button[@Nope='x']",
            "//Slider[@Foo='x']",   # 树里没有 Slider：属性名拼错也必须在解析期报错，不能静默返回 0
            "//Button/", "//Button//", "//", "//*[.//]", "//*[./]", "//*[.]"]:
    try:
        find_all(tree, bad)
        check(f"非法语法报错 {bad}", "无异常", "XPathSyntaxError")
    except XPathSyntaxError:
        check(f"非法语法报错 {bad}", "XPathSyntaxError", "XPathSyntaxError")
check("单独的 / 仍是根自身", find_all(tree, "/"), [tree])

# ============ B. 记事本活体回归 ============
print("\n===== B. 记事本活体回归 =====")
import uiautomation as auto


def launch_notepad(timeout=10.0):
    """新开记事本并按 PID / 新增 HWND 精确定位，避免误用其它已开的记事本窗口。"""
    before = {
        w.NativeWindowHandle
        for w in auto.GetRootControl().GetChildren() if w.ClassName == "Notepad"
    }
    proc = subprocess.Popen(["notepad.exe"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.25)
        cands = [
            w for w in auto.GetRootControl().GetChildren()
            if w.ClassName == "Notepad" and w.NativeWindowHandle not in before
        ]
        if not cands:
            continue
        mine = [w for w in cands if w.ProcessId == proc.pid]
        pool = mine or cands
        return proc, next((w for w in pool if w.Name), pool[0])
    return proc, None


proc, notepad = launch_notepad()
if notepad is None:
    print("!! 未找到记事本窗口，跳过活体测试")
    sys.exit(1)

t0 = time.perf_counter()
snap = snapshot(notepad)
build_ms = (time.perf_counter() - t0) * 1000
n_nodes = count_nodes(snap)
print(f"快照构建: {n_nodes} 个节点, {build_ms:.0f}ms（一次性成本）")

def timed(expr):
    t0 = time.perf_counter()
    res = find_all(snap, expr)
    return res, (time.perf_counter() - t0) * 1000

docs = find_all(snap, "//Document")
doc_name = docs[0].name if docs else "文本编辑器"
doc_cls = docs[0].class_name if docs else "RichEditD2DPT"

r1, dt1 = timed("//ButtonControl")
check("活体 //ButtonControl 非空", len(r1) > 0, True)
r2, dt2 = timed("//Button")
check("活体短名 //Button 与全名一致", len(r2), len(r1))
r3, dt3 = timed(f'//Document[@Name="{doc_name}"]')
check("活体等值谓词", len(r3), len(docs))
r4, dt4 = timed(f'//Document[@Name="{doc_name}" and @ClassName="{doc_cls}"]')
check("活体 and 组合（uiax 直接报错）", len(r4), len(docs))
r5, dt5 = timed("//*")
check("活体 //* 覆盖全树（节点数-1）", len(r5), n_nodes - 1)
r6, dt6 = timed("//*[1]")
check("单上下文 //*[1]", len(r6), 1)
r7, dt7 = timed('//Document[@IsEnabled="true"]')
check("活体扩展属性 IsEnabled", len(r7), len(docs))
r8, dt8 = timed("//Document/.. | //Button[1]")
check("活体父轴 + 并集非空", len(r8) > 0, True)

print(f"\n查询耗时（快照上，纯内存）: {max(dt1,dt2,dt3,dt4,dt5,dt6):.2f}ms（6 条中最慢）")
print(f"扩展属性查询（需回读活体控件）: {max(dt7, dt8):.0f}ms")

proc.terminate()
print(f"\n===== 结果: {PASS} 通过, {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
