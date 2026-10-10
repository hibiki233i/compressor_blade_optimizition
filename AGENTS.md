# 项目协作指南

本文件适用于仓库根目录及其子目录。先阅读 `README.md` 了解目标；涉及算法、恢复或边界实验时再查 `docs/optimization.md`，涉及桌面界面时查 `docs/gui.md`，涉及攻角验证与流动诊断时查 `docs/validation.md`。以当前代码和配置为准。

## 项目与代码入口

本项目以真实 CFD 反馈优化离心叶轮**主叶片平均线**。12 个设计变量是 hub/shroud 各 5 个 beta 偏移及各 1 个 theta 偏移；`blade_shape_config.json` 的 `search` 将其中 7 个设为当前活动变量，其余固定。目标是同时最大化 `Efficiency` 和 `MassFlow`。`PressureRatio`、`totalpressureratio`、`Power` 仅供审查。当前基准模板未启用 splitter blade，不要凭空增加分流叶片变量。

| 位置 | 责任 |
| --- | --- |
| `blade_shape_active_learning.py` | CLI、配置加载、基准几何、12 变量映射、约束、DOE、代理模型、候选选择、真实 CFD 记录与 Pareto 导出 |
| `blade_shape_acquisition.py` | 双目标期望超体积改进及采样 |
| `blade_shape_refinement.py` | 活动/固定变量划分、条件代理、局部搜索、诊断和分阶段边界实验 |
| `blade_shape_pending.py`、`blade_shape_runtime.py` | 待评估队列、断点恢复、算例预留、原子状态文件和进程锁 |
| `Run-BladeShapeGeometryMeshing.ps1` | 将候选写入 CFturbo batch，并调用 CFturbo、TurboGrid |
| `blade_shape_cfx_runner.py` | CFX-Pre、求解、后处理、结果解析及阶段恢复 |
| `blade_shape_incidence_validation.py`、`blade_shape_aca_extraction.py` | 独立展向攻角验证；`aca` 子命令用保存的 session 从 `.res` 导出 20 点 ACA CSV 供 `legacy` 使用 |
| `blade_shape_flow_diagnostics.py` | 对已有 `.res` 做只读熵增及近叶片角度诊断；评估见 `docs/validation.md` |
| `blade_gui/` | PySide6 界面；`project.py` 读取配置/CSV/算例，`validation_data.py` 只读解析攻角验证产物，`commands.py` 组装 CLI 参数，`runner.py` 通过子进程执行 CLI |
| `tests/` | 算法、恢复与界面测试；`fixtures/synthetic_meanline.cft-batch` 仅用于测试 |

GUI 是 CLI 的控制台和结果视图。新增运行能力应先放在 CLI/共享模块，再通过 `blade_gui/commands.py` 接入；不要在页面中重做优化或 CFD 逻辑。`blade_gui/project.py` 不依赖 Qt，负责数据读取与配置校验。`--data-dir` 只改变 GUI 的只读数据视图，运行仍写到配置的 `paths.output_dir`。GUI 不内置工程路径：首次启动不加载配置，表单输入与窗口布局经 `blade_gui/persist.py` 写入用户 INI 文件；新增表单字段用 `remember()` 绑定，测试通过 `BLADE_GUI_SETTINGS` 隔离。

## 必须维持的行为

- 最终 Pareto 只取 `training_data.csv` 中 `status=success` 且目标值有效的真实 CFD 记录。预测值、dry-run 和失败记录不得充当真实标签。工程容差前沿写入 `pareto_front.csv`，严格前沿写入 `pareto_front_strict.csv`。
- `variables` 的名称和顺序与 Python 向量切片、CFturbo 写入映射、CSV 列对应。改变量定义时同步检查这三处、`search.active_variables` / `search.fixed_variables`、几何约束和测试。普通边界或运行预算调整优先改配置，避免把数值硬编码到算法或脚本。
- 续跑是有状态操作：`pending_evaluations.json`、`cases/case_XXXXXX/`、训练 CSV、`geometry_state.json`、`cfx_state.json` 与输入文件身份共同决定能否恢复。保持已有 `run_id`、阶段元数据和历史 CSV 的额外列；不要静默覆盖候选、重编号、伪造完成凭据或把残留 `.res` / `CFX_Results.txt` 当成成功。
- `.optimizer.lock` 保护输出目录与 CFX 算例的单写者流程。不要删除锁文件来绕过正在运行的进程。中断后先检查状态、日志与可能仍在运行的外部求解器，再决定是否续跑。
- 边界实验由 `write-boundary-plan` 生成冻结方案，`run-boundary` 按 `singles`、`pairs`、`extension` 阶段执行。保持方案签名、前置阶段检查和失败即停的语义；不要自动重试已失败的真实计算。
- 代码记录的执行完成与文件身份只证明流程可追溯；工程结论仍需人工检查残差、守恒、网格和工况一致性。

## 修改与验证方式

在仓库根目录执行命令。项目使用 Python 3.10+、NumPy、Pandas；SciPy 和 scikit-learn 是相应采样/GP 功能的可选依赖；GUI 另需 `requirements-gui.txt` 中的 PySide6。真实几何、网格和 CFD 需要 Windows、PowerShell 7、CFturbo、TurboGrid、CFX 及未入库的 `Templates/`。本机路径不写进 JSON：`blade_shape_config.json` 的 `paths.*` 留空，由 `blade_shape_local_config.py` 从配置旁未入库的 `blade_shape_local.ini` 补齐（非空 JSON 值优先），模板为 `blade_shape_local.ini.example`；运行前先检查该 INI，不要假设本机可用，也不要把机器路径提交回 JSON 或 PowerShell 默认值。

```powershell
python -m py_compile blade_shape_active_learning.py blade_shape_acquisition.py blade_shape_refinement.py blade_shape_pending.py blade_shape_runtime.py blade_shape_cfx_runner.py blade_shape_incidence_validation.py blade_shape_aca_extraction.py blade_shape_local_config.py
python -m unittest discover -s tests -v
```

未安装 PySide6 时，依赖 Qt 的界面测试会跳过；这不代表 GUI 已验证。改 GUI 时，在具备 PySide6 的环境运行界面测试。改命令构造、配置或数据层时，运行 `tests/test_gui.py`；改搜索和边界逻辑时，运行 `tests/test_refinement.py`；改恢复、锁或 CFX 时，运行 `tests/test_reliability.py`，随后运行全套测试。

`write-candidate --offline` 仍需可读的 CFturbo batch 基准模板，并会在输出目录写候选文件；`--dry-run` 且不带 `--offline` 会调用 PowerShell 生成中间文件，但不启动工程程序。`check-pre` 会写检查目录。不要把这些命令当成完全无副作用的语法检查，也不要把测试夹具用于工程计算。真实 `run` / `run-boundary` 会消耗求解资源并改写运行产物，只有在工程环境、配置、输出目录和预算明确时才执行。已有训练或待处理数据的普通运行需 `--resume`；用 `--max-new-cfd` 限制新增尝试数。CLI 显式指定配置时，把 `--config` 放在子命令后，例如：

```powershell
python blade_shape_active_learning.py run --config blade_shape_config.json --resume --iterations 0 --max-new-cfd 0
```

该示例仍会读取并可能整理现有运行状态，不应作为纯读取命令。输出目录通常包含 `training_data.csv`、两种 Pareto CSV、`active_learning_diagnostics.csv`、`iteration_summary.csv` 和 `cases/`；它们以及 `ansys_work/`、`Templates/` 均为本地工程数据，不要提交生成产物。需要新增测试时使用临时目录、合成夹具和外部调用替身，覆盖行为和恢复边界；离线测试通过不能替代真实 Windows 工程链的单算例验证。
