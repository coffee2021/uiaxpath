# uiaxpath

[![PyPI](https://img.shields.io/pypi/v/uiaxpath.svg)](https://pypi.org/project/uiaxpath/)
[![Python](https://img.shields.io/pypi/pyversions/uiaxpath.svg)](https://pypi.org/project/uiaxpath/)
[![License](https://img.shields.io/pypi/l/uiaxpath.svg)](https://github.com/coffee2021/uiaxpath/blob/main/LICENSE)
[![CI](https://github.com/coffee2021/uiaxpath/actions/workflows/ci.yml/badge.svg)](https://github.com/coffee2021/uiaxpath/actions/workflows/ci.yml)

Windows UI 自动化的 XPath 子集引擎：把 UIA 控件树一次性建成**内存快照**，再用自研 XPath 在快照上查询。快照建好后查询阶段不再跨进程 COM（扩展属性除外），适合批量查询和反复调试表达式。

配套的 GUI 工具见 [仓库主页](https://github.com/coffee2021/uiaxpath)。

## 特性

- **内存快照模型**：`snapshot()` 一次遍历建成节点树，查询全程纯内存，不触碰活动控件
- **XPath 子集**：六轴（child / descendant / parent / ancestor / following-sibling / preceding-sibling）、相对路径谓词、字符串函数、比较运算、位置索引（含负索引）、and/or、`|` 并集
- **候选表达式生成**：从任意节点反推多条定位表达式，按稳定性排序并实测匹配数
- **拾取定位**：按热键拾取屏幕上的控件，映射回快照节点
- **扩展属性懒加载**：`IsEnabled` 等低频属性只在表达式真正引用时才读活体控件并缓存

## 环境要求

- Windows（依赖 Win32 / UIA COM）
- Python 3.10+
- [uiautomation](https://pypi.org/project/uiautomation/) 2.0.x、comtypes 1.4.x（随本包自动安装）

```bash
pip install uiaxpath
```

## 快速上手

```python
import uiautomation as auto
from uiaxpath import snapshot, find_all

win = auto.WindowControl(searchDepth=1, SubName="影刀")
if not win.Exists(3):
    raise SystemExit("找不到目标窗口")

root = snapshot(win)                       # 一次性建成内存快照
found = find_all(root, '//Button[@Name="确定"]')
for node in found:
    print(node.name, node.rect)

found[0].control.Click()                   # UiaNode.control 是原始 UIA 控件
```

节点对象 `UiaNode` 常用字段：`control_type`、`name`、`automation_id`、`class_name`、`rect`、`parent`、`children`；`node.control` 回到 uiautomation 的活体控件做点击等操作。

## XPath 语法

### 路径与轴

| 写法 | 含义 |
|---|---|
| `/Pane[1]/Button[2]` | 绝对路径：从根的**子节点**开始逐层匹配 |
| `//Button` | 后代轴：在根的所有后代中找（根不是自己的后代） |
| `//Button/..` | 父轴 `..`：Button 的父节点 |
| `//Toolbar/Button` | 相对步：默认 child 轴 |
| `//Edit[1] \| //Button[1]` | 顶层并集：逐条求值后**去重**合并 |

### 节点测试

- 控件类型全名（`ButtonControl`）或短名（`Button`）均可
- `*` 匹配任意类型

### 谓词

| 写法 | 含义 |
|---|---|
| `[@Name='确定']` | 属性等于（单双引号皆可） |
| `[@Name!='确定']` | 不等于 |
| `[@AutomationId]` | 属性存在 |
| `[starts-with(@Name,'文件')]` | 前缀匹配 |
| `[ends-with(@Name,'.txt')]` | 后缀匹配 |
| `[contains(@Name,'保存')]` | 包含 |
| `[not(@AutomationId)]` | 条件取反，内部可嵌 and/or |
| `[1]` / `[-1]` | 位置索引；负数为扩展（见下） |
| `[last()]` / `[position()>1]` | 最后一个 / 除第一个外 |
| `[@ProcessId>=1000]` | 数值比较（`> >= < <=`，两侧能转数字则按数值） |
| `[lower-case(@Name)='ok']` | 字符串函数包裹属性 |
| `[.//Button[@Name='确定']]` | 相对路径：**容器内含某控件**（消歧神器） |
| `[@Name='x' and @Enabled]` | 同一 `[]` 内用 and / or 连接（and 优先） |
| `[1][@Name='x']` | 多个 `[]` **按书写顺序依次作用**：先取第 1 个，再看它是不是 x |

字符串函数：`lower-case(@A)` / `upper-case(@A)` / `normalize-space(@A)`（去首尾空白并压缩内部空格），既可单独比较，也可作为 `contains` 等的第一个参数，如 `contains(upper-case(@Name),'OK')`。

相对路径谓词必须以 `.` 开头：`./Child` 是子节点，`.//Descendant` 是后代。典型用法：

```text
//Pane[.//Button[@Name="确定"]]        含"确定"按钮的 Pane
//Pane[not(.//Button)]                  不含任何按钮的 Pane
```

路径必须完整：`/` 单独写表示快照根自身，但 `//`、`//Button/`、`//Button//`、`[.//]`、`[.]`
这类悬空分隔符或空相对路径会抛 `XPathSyntaxError`，不会被静默当成"匹配全部"。

关键字（`and` / `or` / `not` / `contains` / `last` / `position` 等）、轴名与节点名**大小写不敏感**：`AND`、`ANCESTOR::`、`//button` 都能正常解析。属性**值**仍按原样比较，要忽略大小写请显式用 `lower-case(@Name)='ok'`。

布尔属性（`IsEnabled` 等）写成 `'true' / 'True' / '1'` 均可命中。

> **负索引是非标准扩展**：`[-1]` 表示最后一个，`[-2]` 倒数第二。标准 XPath 里负数位置比较恒假，表达式移植到其他实现会**静默返回空**，请知悉。

### 暂不支持

谓词内支持任意层级的相对路径嵌套（如 `[.//A[.//B]]`）；暂不支持 `matches()` 正则、`count()` 等返回值函数与命名空间相关语法。用到会抛 `XPathSyntaxError`。

## 可用属性

**快照核心属性**（建树时读取，查询零开销）：

`Name`、`AutomationId`、`ClassName`、`ControlType`、`ProcessId`

**扩展属性**（懒加载：表达式首次引用时读活体控件，结果缓存到节点）：

`LocalizedControlType`、`IsEnabled`、`IsOffscreen`、`IsKeyboardFocusable`、`HelpText`、`AcceleratorKey`、`FrameworkId`、`NativeWindowHandle`

引用未知属性会抛 `XPathSyntaxError`。

> 注意：扩展属性走活体控件，若目标控件在快照之后已失效，该次读取可能抛异常或返回空值。对稳定性要求高的查询优先用核心属性。

## API

```python
from uiaxpath import (
    UiaNode, snapshot, find_all, find, XPathSyntaxError,
    pick, node_path, locate_in_snapshot,
    generate, Candidate,
)
```

### snapshot(control, max_depth=60) -> UiaNode

从任意 UIA 控件（通常是窗口）递归建立内存快照。`max_depth` 限制深度防失控。

### find_all(root, expr) -> list[UiaNode]

在快照 `root` 上求值表达式，返回全部匹配节点（保持文档序）。`|` 并集结果已去重。

### find(root, expr) -> UiaNode

要求**恰好一个**匹配：0 个或多个都抛 `LookupError`。

### node_path(node, root) -> str

生成从 root 到 node 的绝对路径，如 `/Pane[1]/Button[2]`。根节点返回 `/`。

### generate(root, node, max_candidates=6) -> list[Candidate]

反推定位表达式候选，每条都实测匹配数。`Candidate` 字段：

- `expr`：表达式
- `count`：实测匹配数；`unique` 属性 = `count == 1`
- `note`：策略说明（"AutomationId，最稳定"、"绝对路径，兜底最脆"等）

排序规则：唯一命中优先，再按策略稳定性（AutomationId > Name > ClassName+Name > 锚点+相对 > 前一个标签+following-sibling > 绝对路径）。没有 Name/AutomationId 的 `Edit`/`Document` 会额外尝试"前一个带名字的兄弟 + `following-sibling`"（登录框、搜索框常见）。实测 0 命中的候选会被丢弃（如根节点上的 `//X` 必然落空）。

### locate_in_snapshot(root, control, ...) -> UiaNode | None

把一个活体 UIA 控件映射回快照节点：RuntimeId → 类型/类名/名字 → 包围盒（取包含鼠标点的最小节点）三级匹配，逐级放宽。拾取场景的入口。

### pick(root, hotkey=("ctrl","shift"), ...) / wait_for_pick(...)

阻塞等待用户按热键，拾取鼠标下的控件并经 `locate_in_snapshot` 映射回快照。GUI 里用的是"工具栏按钮 + 左 Ctrl"的变体（见 app.py）。

## 设计语义与边界

- **根上下文**：`/` 第一步匹配 root 的子节点，`//` 匹配 root 的后代——root 自身都不是候选。选中根节点时候选生成只会给出 `/`。
- **窗口不在 XPath 里**：`find_all(root, expr)` 的 root 就是窗口快照，窗口身份由调用方（`WindowControl(SubName=...)` 或 GUI 下拉框）承载，表达式只描述窗口内部结构。
- **线程模型**：建快照须在初始化过 COM 的线程里进行（uiautomation 的 `UIAutomationInitializerInThread`）；建好后 `find_all` 纯内存，任意线程可用。
- **`[N]` 语义**：索引按每个上下文节点的匹配列表计数，与标准 XPath 的"兄弟计数"在多上下文时有差异。

## 测试

仓库 `tests/` 下三套测试（需要本机有记事本用于活体回归）：

```bash
python -X utf8 tests\test_uiaxpath.py   # 引擎语法 85 项
python -X utf8 tests\test_extras.py     # 扩展属性 + 候选生成
python -X utf8 tests\test_picker.py     # 拾取映射
```
