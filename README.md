# UIA XPath 测试工具

[![PyPI](https://img.shields.io/pypi/v/uiaxpath.svg)](https://pypi.org/project/uiaxpath/)
[![CI](https://github.com/coffee2021/uiaxpath/actions/workflows/ci.yml/badge.svg)](https://github.com/coffee2021/uiaxpath/actions/workflows/ci.yml)

Windows 桌面 UIA 控件的 XPath 测试工具（PySide6），定位是 FlaUInspect 的中文轻量替代：可视化浏览控件树、编写并测试 XPath、实时高亮定位结果，一键生成可落地的 Python 自动化代码。

## 功能

- **单窗口快照**：选定目标窗口后一次性建成内存快照，查询不卡 UI、不受目标程序卡顿影响
- **控件树浏览**：懒加载渲染 + 类型色块图标，支持按名称/类型过滤（自动保留父链）
- **XPath 查询**：自研子集引擎，支持 `//`、`..`、`|`、`and/or`、`!=`、`starts-with()`、位置索引等（语法清单见 [uiaxpath/README.md](uiaxpath/README.md)）
- **结果高亮**：点击查询结果，屏幕上透明红框常驻跟随目标控件（目标窗口挪动时实时跟随）
- **候选 XPath**：选中树节点自动反推最多 6 条定位表达式，绿点=唯一命中、橙点=多处命中，点击即回填执行
- **拾取**：工具栏进入拾取模式 → 鼠标移到目标控件上 → 按左 Ctrl 锁定，自动定位树节点并闪烁确认
- **属性面板**：查看选中控件全部快照属性，Ctrl+C 复制选中行（未选中则复制全部）
- **示范 Python**：按当前 XPath 一键生成最小可用的自动化脚本（快照→查询→点击第一个匹配），附复制按钮
- **历史补全**：最近执行成功的表达式进入输入框补全列表（最多 10 条），用 QSettings 跨会话保存
- **大结果集**：匹配超过 100 条只展示前 100 条（统计行注明总数）；结果行的完整路径在选中时才计算
- **错误提示**：语法错误时输入框描红、提示行变红，不再只是一句灰字

## 环境要求

- Windows 10/11
- Python 3.10+（实测 3.14）
- 依赖锁定版本见 `requirements.txt`：PySide6 6.12.0、uiautomation 2.0.29、comtypes 1.4.17

## 快速开始

```bash
pip install -r requirements.txt
python -X utf8 app.py
```

或双击 `启动.bat`（内部用 pythonw 无控制台启动，路径按本机 Python 位置修改）。

冒烟自检（自动快照首个窗口、跑一次查询、截图退出）：

```bash
python -X utf8 app.py --smoke
```

## 界面与工作流

```
┌──────────────────────────────────────────────────┐
│ 目标窗口: [下拉框────────────] [刷新] [捕获快照] [🎯拾取] │
├────────────────────────┬─────────────────────────┤
│ [过滤框]                │ 属性面板（Ctrl+C 复制）    │
│ 控件树                  │ ──────────────────────── │
│ （懒加载+类型色块）       │ 候选 XPath（点击即执行）   │
├────────────────────────┴─────────────────────────┤
│ XPath [输入框（历史补全）] [示范Python]              │
│ 匹配 N 个，X ms ｜ 每行：控件；选中后显示路径          │
│ 查询结果列表（点击高亮跟随，Ctrl+点击取消；最多 100 行） │
└──────────────────────────────────────────────────┘
```

典型流程：

1. 「刷新窗口列表」→ 下拉框选目标窗口 → 「捕获快照」
   （列表默认包含「另存为」「字体」这类附属对话框，无需额外设置）
2. 左侧树或过滤框找到目标控件；或用「🎯拾取」：进入拾取模式后把鼠标移到目标上按**左 Ctrl**
3. 右上候选面板直接选一条现成表达式，或在 XPath 框手写后回车执行
4. 点击结果列表条目 → 屏幕高亮框跟随定位；再次点击（或 Ctrl+点击）取消
5. 满意后点「示范Python」生成脚本，复制出去即可使用

## XPath 速查

```text
/Pane[1]/Button[2]                        绝对路径
//Button[@Name="确定"]                    后代+属性
//Edit[@AutomationId="kw"]                AutomationId（最稳）
//Button[@Name="x" or @Name="y"]          or（and 优先）
//Pane[@Name="工具栏"]//Button[2]          嵌套定位
//Button[1]/..                            父轴
//Edit[1] | //Button[1]                   并集（去重）
//*[starts-with(@Name,"文件")]             前缀匹配
//Pane[.//Button[@Name="确定"]]             含某控件的 Pane（容器消歧）
//Button[lower-case(@Name)="ok"]           忽略大小写
//Text[@Name="确定"]/following-sibling::Edit[1]   标签后的输入框
//Button[-1]                              最后一个（负索引，非标准扩展）
```

完整语法、属性清单与语义边界（根上下文、`[N]` 计数规则）见 [uiaxpath/README.md](uiaxpath/README.md)。

## 项目结构

```
app.py                 GUI 主程序
uiaxpath/              引擎包（快照/XPath/候选/映射）
├── snapshot.py        内存快照模型 + 扩展属性懒加载
├── engine.py          XPath 解析与求值
├── candidates.py      候选表达式生成
├── picker.py          活体控件→快照节点映射（纯函数，三级匹配）
└── interactive.py     阻塞式热键拾取（开发辅助，勿用于生产脚本）
requirements.txt       锁定依赖版本
启动.bat                无控制台启动
app.ico                软件图标
tests\test_uiaxpath.py  三套测试
```

## 测试

```bash
python -X utf8 tests\test_uiaxpath.py   # 引擎语法
python -X utf8 tests\test_extras.py     # 扩展属性 + 候选生成
python -X utf8 tests\test_picker.py     # 拾取映射
```

## 设计决策与已知限制

- **窗口定位在 XPath 外**：窗口由下拉框/示范代码的 `WindowControl(SubName=...)` 承载，表达式只描述窗口内部结构——与浏览器 XPath 先限定 document 的模式一致，也便于复用同一表达式操作多个同类窗口
- **单窗口快照**：目标窗口内容变化后需重新捕获快照；动态弹窗建议查询前再捕一次
- **高 DPI**：覆盖层按目标所在屏幕的缩放比换算（125% 下高亮框不偏移）；目标跨屏时只覆盖它所在的那块屏幕
- **扩展属性查询**：`IsEnabled` 等属性按需读活体控件，目标控件失效时该属性可能读到空值
