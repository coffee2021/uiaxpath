# -*- coding: utf-8 -*-
"""高亮覆盖层 + XPath 候选生成 测试：
A. 合成树候选生成（排序、唯一性、round-trip）
B. 记事本活体候选生成 + 高亮冒烟（可视化验证窗口能弹出并自毁）
"""
import os
import sys
import time
import subprocess

# 从 tests/ 回到仓库根目录，保证未安装本包时也能直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uiaxpath import snapshot, find_all
from uiaxpath.snapshot import UiaNode
from uiaxpath.candidates import generate

PASS, FAIL = 0, 0

def check(label, actual, expected):
    global PASS, FAIL
    ok = actual == expected
    PASS, FAIL = PASS + ok, FAIL + (not ok)
    print(f"[{'OK' if ok else 'FAIL'}] {label}: 期望 {expected!r}, 实际 {actual!r}")

# ============ A. 合成树候选生成 ============
print("===== A. 合成树候选生成 =====")

def N(ct, name="", aid="", cls="", children=()):
    node = UiaNode.__new__(UiaNode)
    node.control = None
    node.name, node.automation_id, node.class_name = name, aid, cls
    node.control_type, node.process_id, node.rect, node.runtime_id = ct, 0, None, None
    node.children = list(children)
    node.parent = None
    for c in node.children:
        c.parent = node
    return node

# Window(@Name='主窗口')
# ├─ Pane(@AutomationId='left') ─ Button OK(@AutomationId='btn1')
# └─ Pane ─ Button OK / Button Cancel(@ClassName='BtnCls')
target = N("ButtonControl", "OK", aid="btn1")
dup = N("ButtonControl", "OK")
cancel = N("ButtonControl", "Cancel", cls="BtnCls")
tree = N("WindowControl", "主窗口", children=(
    N("PaneControl", aid="left", children=(target,)),
    N("PaneControl", children=(dup, cancel)),
))

cands = generate(tree, target)
for c in cands:
    print(f"  {c.expr}  (匹配 {c.count}, {c.note})")
check("AutomationId 候选居首", cands[0].expr, '//Button[@AutomationId="btn1"]')
check("首候选唯一", cands[0].unique, True)

cands_dup = generate(tree, dup)
for c in cands_dup:
    print(f"  {c.expr}  (匹配 {c.count}, {c.note})")
check("重名节点 Name 候选不唯一", next(c for c in cands_dup if "@Name" in c.expr and "ClassName" not in c.expr).count, 2)
check("重名节点绝对路径兜底唯一",
      next(c for c in cands_dup if c.note.startswith("绝对路径")).unique, True)
bad = [c.expr for c in cands_dup if dup not in find_all(tree, c.expr)]
check("所有候选都能命中目标", bad, [])

cands_cancel = generate(tree, cancel)
check("组合候选可生成且唯一",
      [c.unique for c in cands_cancel if "ClassName" in c.expr], [True])

# 根节点：//X 永远落空（根不是自己的后代），只应给出 "/"
cands_root = generate(tree, tree)
check("根节点只给 / 候选", [c.expr for c in cands_root], ["/"])

# 匿名输入框（登录框场景）：靠前一个 Label 定位
login_label = N("TextControl", "用户名")
login_edit = N("EditControl")
login_form = N("PaneControl", children=(login_label, login_edit))
login = N("WindowControl", "登录", children=(login_form,))
cands_edit = generate(login, login_edit)
for c in cands_edit:
    print(f"  {c.expr}  (匹配 {c.count}, {c.note})")
exprs_edit = [c.expr for c in cands_edit]
check("匿名 Edit 生成 Label 兄弟候选",
      '//Text[@Name="用户名"]/following-sibling::Edit' in exprs_edit, True)
check("Label 兄弟候选命中唯一",
      [c.unique for c in cands_edit if "following-sibling" in c.expr], [True])
check("匿名 Edit 候选均命中目标",
      [c.expr for c in cands_edit if login_edit not in find_all(login, c.expr)], [])

# ============ B. 记事本活体 ============
print("\n===== B. 记事本活体 =====")
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
    print("!! 未找到记事本窗口，跳过")
    sys.exit(1)

snap = snapshot(notepad)
docs = find_all(snap, "//Document")
if not docs:
    print("!! 快照中无 Document 控件，跳过")
    proc.terminate()
    sys.exit(1)
doc = docs[0]

live_cands = generate(snap, doc)
for c in live_cands:
    print(f"  {c.expr}  (匹配 {c.count}, {c.note})")
check("活体候选均命中 doc", [c.expr for c in live_cands if doc not in find_all(snap, c.expr)], [])
check("活体首候选唯一", live_cands[0].unique, True)

btns = find_all(snap, "//Button")
print(f"  Document 矩形: {doc.rect}, Button 数: {len(btns)}")
# tkinter 版高亮已随包分层清理删除；屏幕高亮由 GUI（app.py 的 Overlay）负责

proc.terminate()
print(f"\n===== 结果: {PASS} 通过, {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
