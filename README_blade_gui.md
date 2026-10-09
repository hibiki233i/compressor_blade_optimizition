# Blade Shape 主动学习控制台（GUI）

`blade_gui/` 是给现有主动学习流水线加的一层 PySide6 桌面控制台。它**不修改、不复制**任何优化算法：
数据通过 `blade_shape_active_learning.py` 自己的加载函数读取，所有动作都以子进程方式调用原命令行，
因此代理模型、采集函数、几何约束与 CFD 记录逻辑仍然只有一份。

```
code/blade_gui/
├── app.py            # QApplication 启动、命令行参数
├── context.py        # 全局状态（配置路径 / 数据目录）与跨页信号
├── project.py        # 纯 Python 数据层：配置、CSV、cases、统计（不含 Qt）
├── commands.py       # 纯函数：为每个动作拼出精确的 CLI argv
├── runner.py         # QProcess 封装：流式读取合并后的 stdout/stderr
├── validation_data.py # 纯 Python：只读解析攻角验证产物（不含 Qt，不重算攻角）
├── charts.py         # QPainter 自绘图表：散点 / 折线 / 阶梯 / parity / 条形
├── theme.py          # 调色板、字体选择与 QSS 样式表（下拉箭头/勾选等小图标运行时渲染为 PNG）
├── icons.py          # 内联 SVG 矢量图标与品牌标识（无需图片资源）
├── main_window.py    # 分组侧边导航 + 顶栏上下文标签 + 状态栏
├── widgets/          # Card / StatTile / Badge / MessageBar / ChartCard 等
└── pages/            # 六个功能页；incidence_view.py 为验证页的结果查看器
```

## 安装

只多一个依赖：PySide6（核心流水线的依赖仍是 NumPy/Pandas + 可选 SciPy/scikit-learn，见 `requirements-gui.txt`）。

```powershell
python -m pip install -r requirements-gui.txt
```

PySide6 6.11 提供 `cp310-abi3` 轮子，Python 3.10 及以上（含 3.14）均可安装。

## 启动

```powershell
cd "D:\blade optizamation"
python -m blade_gui
```

Windows 上也可以直接双击 `run_gui.bat`（会自动检测并安装 PySide6）。

可选参数：

```powershell
python -m blade_gui --config blade_shape_config.json
python -m blade_gui --data-dir "D:\blade optizamation\blade_al_runs"
python -m blade_gui --config D:\other\cfg.json --data-dir D:\copied\results
```

- `--config`：配置文件路径。省略时使用上次在界面中选择的配置；首次启动不加载任何配置，
  需在侧栏「配置…」中选择（之后会记住）。
- `--data-dir`：**只读覆盖** `paths.output_dir`。当你在非 CFD 主机上查看别人拷回来的
  `training_data.csv` / `pareto_front.csv` 时用它，不必改配置。

运行控制中的命令始终使用当前配置文件的 `paths.output_dir`；续跑提示、边界方案列表和「打开输出目录」
也按该实际运行目录显示。设置只读数据目录后，看板和算例浏览仍显示只读目录中的结果。

界面内 左下角「设置数据目录…」等价于 `--data-dir`，会记住选择。

**记住的内容**：界面不内置任何工程路径。所选配置、数据目录、运行控制与验证页各表单中填写的路径和数值、
当前动作、分栏宽度、上次打开的页面、窗口位置/大小，以及文件对话框最近使用的目录，都在每次修改时写入本机 INI 文件：
Windows 为 `%APPDATA%\BladeShape\ActiveLearningGUI.ini`，macOS/Linux 为 `~/.config/BladeShape/ActiveLearningGUI.ini`。
删除该文件即恢复为空白状态；环境变量 `BLADE_GUI_SETTINGS` 可指向另一个 INI 文件（测试与便携使用）。
CFX-Post 程序等字段只把配置推导的路径作为灰色提示，不会自动填入。

## 界面结构

- **左侧栏**：品牌区；按「监控 / 工作流 / 检查与验证」分组的六个页面入口（右侧显示快捷键）；
  尝试/成功/Pareto/失败四格统计；自动刷新开关（15 秒）、刷新、切换配置、设置数据目录。
- **顶栏**：当前页标题与说明；右侧标签显示配置文件（有校验错误时变红）、输出目录，
  以及「只读数据视图」（使用 `--data-dir` 时）和「任务运行中」提示。
- **状态栏**：运行状态指示点、最近一次操作结果与配置/记录概况。

快捷键：`Ctrl+R` 刷新数据，`Ctrl+1`～`Ctrl+6` 切换页面。

## 页面说明

### 1. 总览看板

- 六个 KPI 磁贴：总尝试、成功、失败、Pareto 解、最佳 Efficiency、最佳 MassFlow。
- **Pareto 前沿**：X = MassFlow，Y = Efficiency；灰点为被支配的成功样本，青色阶梯线为工程容差前沿，
  紫色圆环标出严格前沿。**单击数据点会跳转到对应算例**（见「算例浏览」）。
- **最优值收敛**：成功样本的累计最优，左轴 Efficiency、右轴 MassFlow（虚线）。
- **样本来源分布** / **失败阶段分布**：按 `sample_phase` 与 `failure_stage` 统计。
- **代理模型诊断门**：`local_diagnostic_gate.json`（或旧版 `diagnostic_gate.json`）的通过状态、
  各目标的 MAE 与容差对比、2σ 覆盖数。

图表交互：滚轮缩放（以鼠标位置为中心），双击复位，鼠标悬停显示 run_id 与数值。

### 2. 数据分析

顶部可切换目标函数（Efficiency / MassFlow），影响相关性、误差与标准差三张图。

- **变量分析**：变量-目标 Pearson 相关系数（正绿负红，按绝对值排序）+ 变量-目标散点
  （按样本来源着色）。成功样本少于 3 个时不给相关性，避免误读。
- **代理预测质量**：两个目标的预测 vs 真实 parity 图（虚线为理想对角线，坐标轴严格等范围以保证 45°）、
  预测误差随迭代（含零误差参考线）、预测标准差随迭代。
- **主动学习进程**：每轮超体积增益 `hv_gain`、候选来源分布、局部搜索半径、每轮评估前后 Pareto 行数。

> 这里展示的 `pred_*` 全部是 CFD **评估前**记录的代理预测，仅用于诊断；它们不会成为训练标签或 Pareto 成员。

### 3. 项目设置

按分区可视化编辑 `blade_shape_config.json`：

- 路径配置（带浏览按钮）、运行参数、代理模型、Pareto 容差、几何约束、搜索空间、局部细化。
- 标 ⓘ 的参数悬停可看含义、推荐值和确定方法；右侧提示随表单实时换算，例如把归一化距离换算成各活动变量的角度、给出“10 × 活动变量数”等推荐值。「参数说明」按钮列出全部说明，并标明修改会影响哪些签名或状态。说明文字集中在 `blade_gui/field_help.py`，修改参数语义时同步更新。
- **设计变量边界**表：下限 / 上限 / 是否参与搜索 / 固定值。取消勾选即把该变量写入 `fixed_variables`，
  勾选即写入 `active_variables`。12 个变量的顺序即几何写入顺序，不要调整行序。
- 顶部实时校验：错误（如下限 ≥ 上限、活动/固定划分不完整）会**阻止保存**；本机缺少外部程序路径只是警告。
- 保存为**原子写入**并保留时间戳备份到 `config_backups/`。「另存为…」可切换为新配置。
- 有未保存修改时会显示「未保存修改」徽标，并且自动刷新不会覆盖你的编辑。

### 4. 运行控制

覆盖 5 个动作，均以 `python -u blade_shape_active_learning.py <子命令> --config ...` 形式在子进程中执行：

| 动作 | 说明 |
| --- | --- |
| 运行主动学习 `run` | 真实 CFD 循环；预填 `initial_samples` / `iterations` / `batch_size` / `max_new_cfd` / `seed`，可勾选 `--resume` |
| 生成候选 `write-candidate` | 单个候选 + 几何规则校验，支持 `--dry-run` / `--offline` |
| 离线诊断 `diagnose` | 不启动 CFD，生成切片审计、角色诊断与诊断门 |
| 生成边界方案 `write-boundary-plan` | 中心算例 run_id 从**成功记录下拉列表**中选择，避免手填错误 |
| 运行边界阶段 `run-boundary` | 选择方案文件与 `singles` / `pairs` / `extension` 阶段 |

- **命令预览**区显示将要执行的完整命令行，可一键复制。
- **本机环境检查**列出 PowerShell、CFturbo、TurboGrid、CFX bin 是否存在于本机。
- 运行前有二次确认（真实 CFD 会调用外部工程软件）；已存在训练数据但未勾选 `--resume` 时会明确警告。
- 实时日志按级别着色（错误红、警告黄、成功绿），支持自动滚动、清空、保存日志、打开输出目录。
- 运行中可「停止」：先 `terminate`，2.5 秒后仍未退出再 `kill`（不阻塞界面）。注意被中断的是父进程，
  求解器子进程可能仍需人工确认。
- 进程结束后会自动刷新全部页面数据。

> `run` 与 `run-boundary` 只能在装有 CFturbo 2025.2.2 与 ANSYS CFX/TurboGrid 2025 R1 的 Windows 机器上真正跑通；
> 在 macOS/Linux 上界面可正常使用，但这些动作会因外部程序缺失而失败并在日志中给出原因。

### 5. 算例浏览

- 左侧表格：`run_id`、来源、状态、失败阶段、Efficiency、MassFlow、文件数、更新时间；
  支持按 run_id / 失败信息搜索，以及「仅成功 / 仅失败 / 未登记」筛选，表头可排序。
- 右侧详情四个标签页：
  - **概览**：状态、失败阶段与消息、来源、四个指标、文件数、目录。
  - **文件**：目录内所有文件（大小、时间），双击用系统程序打开；日志类文件高亮。
  - **日志**：下拉选择日志文件，显示末尾 800 行并给出文件大小。
  - **candidate.json**：格式化 JSON。

> 如果 CSV 里记录的是 Windows 路径（例如在 Mac 上查看拷贝回来的 CSV），
> 详情区会明确提示「该算例目录在本机不存在」，而不是显示空白面板。

### 6. 验证（展向几何攻角诊断）

“验证”页（`Ctrl+6`）左侧是八项入口的表单：待核对配置准备、展向攻角提取、截面/分带敏感性、ACA 20 点 CSV 导出、20点旧指标复现、基准/目标工况对比、进口角敏感性方案生成与真实CFD执行。每个动作都标注影响范围（只读后处理 / 写新文件 / 真实 CFD），路径可手填或浏览；选择「新输出目录」的父目录后会自动追加带时间戳、尚不存在的子目录名，已存在的输出会提前提示（CLI 拒绝覆盖）。准备配置时可「从优化算例填入」：按 `cfx_state.json` 的完成凭据找到已接受的 `.res`，并填入 `candidate.json`；没有凭据时只提示，不替你确认。

右侧「攻角诊断」标签页只读显示 CLI 写出的结果，命令结束（含退出码 2 的质量未通过）后自动载入对应输出，也可手动打开任意结果目录：

| 结果 | 显示内容 |
| --- | --- |
| `extract` 目录 | RMS / 平均 / 最大 \|i\|、整轮净流量、逆流占比与质量闭合；叶片角 β_b 与来流角 β_f 的展向分布、几何攻角 i(span)（含 ±RMS 参考线）、正向流量份额与逐带逆流占比（含阈值）、相对速度分量 W_s / W_θ；分带明细表（攻角列按正负着色） |
| 叠加基准 | 仅当方法与测量定义（截面、分带、方向约定、逆流阈值）完全一致才允许，规则与 `compare` 一致；叠加后显示基准曲线与逐带 Δi，否则拒绝并说明原因 |
| `sweep` 目录 | RMS vs 截面位置（按分带数分线）、各测量定义的 i(span) 叠加、RMS 范围与极差、`sensitivity.csv` |
| `aca` 导出 | 不进入查看器；成功后把两列 CSV 自动填入「复现报告20点指标」的「原始 ACA CSV」，象限不满足时在日志中警告 |
| `legacy` 目录 | 20 点旧指标的 β_b / β_f / i 与原始 β_cfx；标明与质量加权指标不可混比 |
| `compare` 结果 | CLI 写出的匹配结论、流量相对差与 ΔRMS；可一键叠加两侧的展向分布 |
| `plan` 目录 | 方案点状态、Efficiency / MassFlow 随前缘角偏移的变化、已完成点之间的有限差分斜率与曲率 |
| 验证配置 JSON | 仍待确认的字段、预填来源与警告 |
| 失败的提取目录 | `state.json` 中的失败阶段与信息、保留的日志文件 |

查看器不会重算攻角：所有数值来自 `profile.csv` / `summary.json` 等 CLI 产物。有限差分只描述已经计算的 CFD 点，不是最小损失攻角标定；叠加只用于观察形状差异，是否同工况仍以「工况对比」结果为准。

“打开验证配置”使用系统关联程序打开JSON；准备阶段会从可识别的 `candidate.json`、`.cft-batch` 或匹配的 CFX 运行记录预填有来源的值，并在日志中列出仍需确认的字段。可选候选文件无效时会跳过角度预填并保留警告；对同一算例重复准备会先备份原配置，再只补齐空字段。预填不能证明几何对应关系；提取前应核对实际角度、工况和方向约定，详见 [验证操作说明](README_incidence_validation.md)。提取、敏感性等分析输出必须使用独立的新文件/目录。退出码2表示质量或工况检查未通过，不显示为验证成功。真实CFD表单要求明确点数预算，可勾选续跑；运行控制页与验证页互斥启动任务。

项目设置页增加 `cfx_convergence.rms_target`（默认1e-5，只能设得更严格）、`restart_iterations`（1500–2000，默认2000）及CFX的Flow名称。求解结束后必须通过末次RMS检查；正常耗尽步数且不收敛时，允许一次有预算的续算，仍不收敛则记录solve失败。一个CFD点的预算包含初算和这一次追加求解，不按两个设计点计数。

## 安全与边界

- GUI 从不把预测值、dry-run 或失败记录写成真实成功标签，也不参与 Pareto 计算。
- GUI 不导入优化器去「自己跑」，所有执行都走原 CLI，失败阶段与日志语义完全一致。
- 配置保存前用与 CLI 相同的规则校验（含 `blade_shape_refinement.active_indices` 的划分检查），
  出错则拒绝写入；写入为临时文件 + 原子替换，并保留备份。
- 输出目录的 `.optimizer.lock` 单写者保护仍由 CLI 负责；GUI 不会绕过它。

## 测试

```powershell
# 纯逻辑测试（不需要 Qt）
python -m unittest tests.test_gui -v

# 全部测试
python -m unittest discover -s tests -v
```

`tests/test_gui.py` 覆盖：命令拼装、配置校验与备份、数据层统计/缓存/空目录/损坏配置、配置切换与运行目录交互，
以及离屏的窗口与图表构造。`tests/test_incidence_viewer.py` 用验证 CLI 自身的 `reduce_profile` 生成合成结果，
覆盖各类产物的读取、叠加可比性、有限差分与查看器交互；`tests/test_regressions.py` 覆盖本轮修复的缺陷。
未安装 PySide6 时，widget 测试会自动 skip，命令行测试仍照常通过。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `ModuleNotFoundError: No module named 'PySide6'` | `python -m pip install -r requirements-gui.txt` |
| 顶栏提示「存在校验错误」 | 到「项目设置」查看红色提示，修正后保存 |
| 运行页提示外部程序缺失 | 该机不是 CFD 主机；用 `--data-dir` 看数据即可 |
| 算例详情提示目录不存在 | CSV 中记录的是别的机器上的路径；用「设置数据目录…」指向本机输出目录 |
| 图表空白 | 对应产物尚未生成（如没有诊断记录就看不到 parity 图） |
