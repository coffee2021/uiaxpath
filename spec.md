# 全项目复查规格（2026-10-23）

## 1. 背景与范围

本轮改动集中在四处：候选生成（缓存 + 匿名 Edit 的「前一个标签 + following-sibling」）、结果列表（100 条截断 + 路径选中时才算）、编辑器（QSettings 历史 + 语法错误行内标红）、XPath 关键字/轴名/节点名大小写不敏感。

复查覆盖：`app.py`（全量）、`uiaxpath/engine.py`、`uiaxpath/snapshot.py`、`uiaxpath/picker.py`、`uiaxpath/candidates.py`、`uiaxpath/__init__.py`、三套测试、两个 README。

结论：XPath 引擎语义与前几轮修复均未回归（引擎 81 项、extras 12 项、picker 9 项通过）。发现 1 个高优先级崩溃路径、2 个提示文案/样式状态残留问题、1 个字段兜底缺失、2 处过期注释，另有 2 个值得做的优化。

## 2. BUG 与修法（decision-complete）

### B1（高）过期快照线程引用 → `RuntimeError: Internal C++ object already deleted`

位置：`app.py` `_on_snapshot`（L802）、`_on_snapshot_failed`（L808）、`capture`（L789）、`closeEvent`（L853）。

成因：`capture()` 里 `thread.finished.connect(thread.deleteLater)`；若期间 `_on_picked` 递增了 `_snap_seq`，线程结束时 `_on_snapshot` 因 seq 过期提前 `return`，**没有把 `self._snap_thread` 置 None**。线程对象随后被 `deleteLater` 销毁，而 `self._snap_thread` 仍指向已被销毁的 Python 包装对象；下次点「捕获快照」执行 `self._snap_thread.isRunning()`，或关窗时 `closeEvent` 执行 `thread.isRunning()`，都会抛 `RuntimeError`，经 excepthook 变成全局模态错误框。

修法：

```python
def _on_snapshot(self, root, elapsed, count, seq):
    # 本线程已结束：先摘掉引用，避免 deleteLater 后再调 isRunning()
    if self.sender() is self._snap_thread:
        self._snap_thread = None
    if seq != self._snap_seq:
        return
    self._apply_root(root, ...)
```

`_on_snapshot_failed` 同构（sender 判断 + seq 判断）。`capture()` 与 `closeEvent()` 再加一层防御：

```python
thread = self._snap_thread
if thread is not None:
    try:
        busy = thread.isRunning()
    except RuntimeError:      # 旧线程对象已被销毁
        self._snap_thread = None
        busy = False
```

`closeEvent` 中同理（`RuntimeError` 时直接跳过等待）。

### B2（中）空表达式只复位颜色、不复位文案

位置：`app.py` `run_xpath` L1112-1115 + `_set_xpath_error` L1153。

现象：先输入 `//Button[` 得到红色「语法错误: …」，再清空输入框回车 —— 红框和红字样式被复位，但提示行仍留着那句错误文案（变成灰字），看起来像还在报错。

修法：

```python
if not expr:
    self._set_xpath_error(None)
    self.xpath_info.setText(_RESULT_HINT)   # 清掉上一次的错误文案
    return
```

### B3（低）`_root is None` 分支不复位红色样式

位置：`app.py` L1109-1111。可达性很低（只有首次捕获前），但与 B2 同一处状态机，一并处理：

```python
if self._root is None:
    self._set_xpath_error(None)
    self.xpath_info.setText("请先捕获快照")
    return
```

### B4（中）`UiaNode` 直接信任底层属性，缺失时返回 None

位置：`uiaxpath/snapshot.py` `UiaNode.__init__` L28-31。

事实：`uiautomation` 的 `Name` 是 `CurrentName or ''`，但 `AutomationId` / `ClassName` / `ControlTypeName` 直接返回底层值（`uiautomation.py` L6605 / L6628），部分 Provider 可为 None。一旦为 None：

- `app.py` `_label()` 的 `len(name)` → `TypeError`
- `_matches()` 的 `node.name.lower()` → `AttributeError`
- `_show_props()` 里 `QTableWidgetItem(None)` → `TypeError`
- `engine._node_test()` 的 `ct.lower()` → `AttributeError`

修法（一处兜底，四路问题同时消失）：

```python
self.name: str = control.Name or ""
self.automation_id: str = control.AutomationId or ""
self.class_name: str = control.ClassName or ""
self.control_type: str = control.ControlTypeName or ""
```

### B5（低）`test_uiaxpath.py` L113 注释过期

现写「节点名仍大小写敏感」，实际实现与紧随其后的两条用例（L120-121 `//button`、`//BUTTONCONTROL`）都已放开。改为：「关键字、轴名与节点名大小写不敏感；仅属性值大小写敏感」。

### B6（低）`app.py` L675 注释「最多 5 次全树查询」过期

候选策略已扩到 6 条（`generate` 默认 `max_candidates=6`），改为「最多 6 次」。

## 3. 优化点（建议做）

### O1 `_remember_xpath` 每次成功查询同步写注册表

位置：`app.py` L1181-1189。QSettings 写注册表虽然只需毫秒级，但每条成功查询都要落一次盘没有必要。改为「标脏 + 退出时统一落盘」：

- `__init__` 增 `self._settings_dirty = False`
- `_remember_xpath` 末尾改为 `self._settings_dirty = True`
- 新增 `_flush_settings()`：dirty 时 `setValue` + `sync()` 并复位
- `closeEvent` 开头调用 `_flush_settings()`

代价：进程被强杀时最多丢本次会话的历史（低价值数据，可接受）。

### O2 拾取到列表外的窗口时下拉框显示旧窗口

位置：`app.py` `_on_picked` L1241 + `_apply_root` L830-838。

拾取换根后，若目标窗口不在 `win_combo` 列表（窗口列表过期、弹出层窗口），下拉框保持旧窗口；用户随后点「捕获快照」会拍到错误窗口。

修法：抽出 `_sync_combo_to_root(root)`，`_apply_root` 与 `_on_picked`（换根分支）都调用；命中则 `setCurrentIndex`，未命中则 `insertItem(0, ...)` 补一条并选中。

## 4. 记录但本轮不做

- `snapshot()` 达到 `max_depth=60` 时静默截断：极深树会缺节点且无提示。改动需扩 `UiaNode` 槽位或改 `snapshot` 返回值，收益/成本不划算。
- `_cand_cache` 无上限：键为 `(id(root), id(node))`，单快照内条目数受节点数约束，换根即清空，实际不会膨胀。
- 结果行首次选中时把完整路径拼进同一行，超深路径会把行撑很宽：可用 tooltip 承载，纯观感问题。
- `setUpdatesEnabled(False)` 包裹结果列表重填：最多 100 行，收益很小。

## 5. 验证方案

1. `python -X utf8 test_uiaxpath.py` → 81 通过（B5 只改注释）
2. `python -X utf8 test_extras.py` → 12 通过
3. `python -X utf8 test_picker.py` → 9 通过（按钮项可能因遮挡跳过）
4. `python -X utf8 app.py --smoke` → `SMOKE OK`
5. B1 针对性验证：启动 GUI → 点「捕获快照」→ 在快照线程结束前手动执行 `w._snap_seq += 1`（或直接拾取一次）→ 等线程结束 → 再点「捕获快照」，断言无 `RuntimeError`、无模态错误框。
6. B2 针对性验证：输入 `//Button[` 回车（红框 + 红字）→ 清空输入框回车 → 提示行应为灰色「每行：控件；选中后显示完整路径」，输入框无红框。
7. B4 验证：用合成控件（`AutomationId=None`、`ControlTypeName=None`）建 `UiaNode`，断言字段为空串且 `_label`/`_show_props` 不抛异常。

## 6. 验收标准

- 上述 7 项验证全部通过；
- 无新增全局异常日志（`app_error.log` 不新增条目）；
- 不改变现有 UI 行为与文案（例外：B2 的错误文案在空输入时被正确清除）。
