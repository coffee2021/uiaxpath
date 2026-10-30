# tasks.md — 复查修复任务清单（对应 spec.md）

- [x] T1（高）修 B1：`app.py` `_on_snapshot` / `_on_snapshot_failed` 用 `self.sender()` 摘引用，`capture` / `closeEvent` 对 `isRunning()` 做 `RuntimeError` 兜底
- [x] T2（中）修 B2/B3：`app.py` `run_xpath` 空输入与 `_root is None` 分支同时复位样式与提示文案
- [x] T3（中）修 B4：`uiaxpath/snapshot.py` `UiaNode.__init__` 四个字段加 `or ""` 兜底
- [x] T4（低）修 B5/B6：`test_uiaxpath.py` L113 注释、`app.py` L675 注释
- [x] T5（优化）O1：`_remember_xpath` 改为标脏 + `_flush_settings()`，`closeEvent` 落盘
- [x] T6（优化）O2：抽出 `_sync_combo_to_root()`，`_apply_root` 复用，列表外的窗口补插一条
- [x] T7 回归：跑 `test_uiaxpath.py`(81)、`test_extras.py`(12)、`test_picker.py`(6)、`app.py --smoke`
- [x] T8 针对性验证：B1 过期线程复现、B2 错误文案清除、B4 合成 None 字段节点（12/12 通过）
- [x] T9 清理临时产物（`app_smoke.png`、`__pycache__`、探针脚本）

备注：spec.md 第 4 节「本轮不做」的项已确认排除，不进入实现。

## 执行补充说明

- `test_picker.py` 在本机运行时记事本被工具宿主窗口遮挡，`ControlFromPoint` 拾到的是别的进程。
  沿用按钮段落既有的处理方式，新增「拾到控件 ProcessId 与记事本不符则跳过 Document 断言」的遮挡保护；
  未遮挡时断言照常执行。
