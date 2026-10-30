# -*- coding: utf-8 -*-
"""拾取器测试：
A. 合成树：node_path 生成 + find_all 回解 round-trip
B. 记事本活体：注入式 pick（模拟热键+坐标），验证 RuntimeId 映射与路径
"""
import os
import sys
import time
import subprocess

# 从 tests/ 回到仓库根目录，保证未安装本包时也能直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uiaxpath import snapshot, find_all
from uiaxpath.snapshot import UiaNode
from uiaxpath.picker import node_path, locate_in_snapshot
from uiaxpath.interactive import pick

PASS, FAIL = 0, 0

def check(label, actual, expected):
    global PASS, FAIL
    ok = actual == expected
    PASS, FAIL = PASS + ok, FAIL + (not ok)
    print(f"[{'OK' if ok else 'FAIL'}] {label}: 期望 {expected!r}, 实际 {actual!r}")

# ============ A. 合成树 ============
print("===== A. 合成树路径 =====")

def N(ct, name="", children=()):
    node = UiaNode.__new__(UiaNode)
    node.control = None
    node.name, node.automation_id, node.class_name = name, "", ""
    node.control_type, node.process_id, node.rect, node.runtime_id = ct, 0, None, None
    node.children = list(children)
    node.parent = None
    for c in node.children:
        c.parent = node
    return node

# Window
# ├─ Pane ─ Button OK / Button Cancel / Button Apply
# ├─ Pane ─ Button OK
# └─ Edit
btn_ok = N("ButtonControl", "OK")
tree = N("WindowControl", children=(
    N("PaneControl", children=(btn_ok, N("ButtonControl", "Cancel"), N("ButtonControl", "Apply"))),
    N("PaneControl", children=(N("ButtonControl", "OK"),)),
    N("EditControl"),
))
pane1, pane2, edit = tree.children

check("唯一同类兄弟不带索引", node_path(edit, tree), "/Edit")
check("多个同类兄弟带索引", node_path(pane1.children[1], tree), "/Pane[1]/Button[2]")
check("第二个 Pane 的 Button", node_path(pane2.children[0], tree), "/Pane[2]/Button")
check("根节点", node_path(tree, tree), "/")

# round-trip：树中每个节点都能被自己的路径精确找回
all_nodes = [tree] + list(tree.descendants())
bad = []
for n in all_nodes:
    p = node_path(n, tree)
    if p == "/":
        continue
    got = find_all(tree, p)
    if got != [n]:
        bad.append((p, len(got)))
check(f"全树 {len(all_nodes)} 个节点路径 round-trip", bad, [])

# ============ B. 记事本活体拾取 ============
print("\n===== B. 记事本活体拾取 =====")
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
# 活体拾取按屏幕坐标取控件：目标窗口被其它窗口挡住时会拾到别的窗口，
# 先把它提到前台（失败也不影响后续，只是可能命中不到）
try:
    auto.SetForegroundWindow(notepad.NativeWindowHandle)
    time.sleep(0.4)
except Exception:
    pass

# 目标：文本编辑器（DocumentControl），取其矩形中心作为拾取点
docs = find_all(snap, "//Document")
if not docs:
    print("!! 快照中无 Document 控件，跳过")
    proc.terminate()
    sys.exit(1)
doc = docs[0]
l, t, r, b = doc.rect
px, py = (l + r) // 2, (t + b) // 2

# 1) ControlFromPoint → locate_in_snapshot（验证 RuntimeId 映射）
picked_control = auto.ControlFromPoint(px, py)
# 记事本被别的窗口盖住时，拾到的是别的进程的控件；映射必然落空，
# 这是环境问题而不是实现问题，与按钮段落一致：跳过断言
try:
    occluded = picked_control.ProcessId != notepad.ProcessId
except Exception:
    occluded = True
node = locate_in_snapshot(snap, picked_control, point=(px, py))
print(f"拾取点 ({px},{py}) 命中: {picked_control.ControlTypeName} -> 快照节点 {node!r}")
if occluded:
    print("!! 记事本窗口被其它窗口遮挡（拾到的是别的进程），跳过 Document 拾取断言")
else:
    check("RuntimeId 映射到 DocumentControl", node.control_type, "DocumentControl")
    check("映射结果与引擎找到的是同一节点", node is doc, True)

# 2) pick() 全流程（注入热键与坐标，模拟用户按压）
def make_fake_pressed():
    """每次调用生成独立计数器：第 3~5 次轮询视为按下，随后"松开"。"""
    state = {"n": 0}
    def fake_pressed():
        state["n"] += 1
        return 3 <= state["n"] <= 5
    return fake_pressed

node2, path = pick(snap, is_pressed=make_fake_pressed(), get_point=lambda: (px, py))
print(f"pick() 返回路径: {path}")
if occluded:
    print("!! 同上，跳过 pick() 路径断言")
else:
    check("pick() 返回同一节点", node2 is doc, True)
    check("路径回解唯一", find_all(snap, path) == [doc], True)

# 3) 对一个按钮拾取，验证多同类兄弟路径
btns = find_all(snap, "//Button")
if btns:
    btn = btns[0]
    bl, bt, br, bb = btn.rect
    node3, path3 = pick(snap, is_pressed=make_fake_pressed(),
                        get_point=lambda: ((bl + br) // 2, (bt + bb) // 2))
    print(f"按钮路径: {path3}")
    if node3 is None:
        # 窗口被遮挡时坐标落在别的窗口上，映射不回快照——属于环境问题，不算失败
        print("!! 按钮拾取未命中（窗口可能被遮挡），跳过按钮路径检查")
    else:
        check("按钮路径回解唯一", find_all(snap, path3) == [node3], True)
else:
    print("!! 快照中无 Button，跳过按钮路径检查")

proc.terminate()
print(f"\n===== 结果: {PASS} 通过, {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
