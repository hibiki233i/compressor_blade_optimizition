# 项目协作指南

本文件适用于仓库根目录及其子目录。

- 先阅读 `README.md`；按任务查 `docs/optimization.md`（算法、恢复、边界实验）、`docs/gui.md`（界面）或 `docs/validation.md`（攻角验证、流动诊断），行为以当前代码和配置为准。
  原因：文档提供工程背景，代码和配置确定当前实际执行的行为。

## 项目与代码入口

本项目以真实 CFD 反馈优化离心叶轮**主叶片平均线**。12 个设计变量是 hub/shroud 各 5 个 beta 偏移及各 1 个 theta 偏移；当前配置的 `search` 搜索其中 7 个，其余固定。目标是同时最大化 `Efficiency` 和 `MassFlow`；`PressureRatio`、`totalpressureratio`、`Power` 用于审查。

- 保持当前主叶片变量和双目标范围；当前映射没有 splitter blade 变量，清理代码时不要增加分流叶片变量或改变优化目标。
  原因：`geometry_indices()` 只接受现有 12 个名称，采集函数按两个目标计算改进，扩大范围会改变研究问题和计算链。

| 位置 | 责任 |
| --- | --- |
| `blade_shape_active_learning.py` | CLI、配置加载、基准几何、12 变量映射、约束、DOE、代理模型、候选选择、真实 CFD 记录与 Pareto 导出 |
| `blade_shape_acquisition.py` | 双目标期望超体积改进及采样 |
| `blade_shape_refinement.py` | 活动/固定变量划分、条件代理、局部搜索、诊断和分阶段边界实验 |
| `blade_shape_pending.py`、`blade_shape_runtime.py` | 待评估队列、断点恢复、算例预留、原子状态文件和进程锁 |
| `blade_shape_review.py` | 略未达到 RMS 门槛的送审结果（`status=review`）的列出与人工接受/否决，决定写入 `review_decisions.json` |
| `blade_shape_signature.py` | 物理签名（含声明工况、物理输入和 CFX 求解器身份）、训练记录的输入兼容判断、`--accept-input-change` 的确认记录 |
| `Run-BladeShapeGeometryMeshing.ps1` | 将候选写入 CFturbo batch，并调用 CFturbo、TurboGrid |
| `blade_shape_cfx_runner.py`、`blade_shape_convergence.py` | CFX-Pre、求解、后处理、结果解析、最终残差判定及阶段恢复 |
| `blade_shape_incidence_validation.py`、`blade_shape_aca_extraction.py` | 独立展向攻角验证；`aca` 子命令用保存的 session 从 `.res` 导出 20 点 ACA CSV 供 `legacy` 使用 |
| `blade_shape_flow_diagnostics.py` | 对已有 `.res` 做只读熵增及近叶片角度诊断；评估见 `docs/validation.md` |
| `blade_gui/` | PySide6 界面；`project.py` 读取配置/CSV/算例，`validation_data.py` 只读解析攻角验证产物，`commands.py` 组装 CLI 参数，`runner.py` 通过子进程执行 CLI |
| `tests/` | 算法、恢复与界面测试；`fixtures/synthetic_meanline.cft-batch` 仅用于测试 |

## GUI 分工

- 运行逻辑放在 CLI/共享模块，通过 `blade_gui/commands.py` 接入页面；数据读取和配置校验放在不依赖 Qt 的 `blade_gui/project.py`。
  原因：GUI 和 CLI 共用计算逻辑，数据层也能在没有 PySide6 的环境独立测试。
- `--data-dir` 只改变只读数据视图，运行命令仍写到所选配置的 `paths.output_dir`。
  原因：查看其他目录的结果不应把新算例写进该目录。
- 保持 GUI 从用户选择或已保存的配置路径启动；没有已保存选择时首次启动不加载配置，不内置工程路径。表单和分割布局由 `persist.py` 绑定，配置选择和窗口几何由 `context.py`、`main_window.py` 保存到用户 INI。
  原因：工程位置由用户确定，界面记忆只恢复操作状态，不替代工程配置。
- 新增需记忆的表单字段使用 `remember()`；界面设置测试用 `BLADE_GUI_SETTINGS` 指向临时 INI。
  原因：统一绑定可同时处理恢复和保存，测试不会读写用户的真实界面设置。

## 必须维持的行为

- 最终 Pareto 只取 `training_data.csv` 中 `status=success`、目标值有效且物理签名与当前输入兼容的真实 CFD 记录。预测值、dry-run、失败记录和待审核记录（`status=review`）不得充当真实标签；送审结果只能经用户用 `review --accept` 写明原因后变为 `success`，不要自动接受，GUI 也不要记住审核理由。工程容差前沿写入 `pareto_front.csv`，严格前沿写入 `pareto_front_strict.csv`。
  原因：代理预测和未通过接受判定的产物不能成为性能证据，送审结果需要独立的人工判断，两种前沿分别服务工程比较与严格数学对照。
- `variables` 必须包含现有 12 个名称且各出现一次；几何和约束通过 `geometry_indices()` 按名称查找，向量按配置列表顺序编码，CSV 按列名读取。改名称或顺序时同步检查向量转换、几何映射、活动/固定变量、约束、签名和测试。
  原因：重排后的变量可以正确映射到几何，但顺序仍属于签名，已有队列和边界方案里的向量需要保持可解释。
- 普通边界或运行预算调整优先改配置，避免把数值硬编码到算法或脚本。
  原因：边界和预算已有配置入口，单一来源能让 CLI、GUI 与续跑使用同一组设置。
- 续跑是有状态操作：`pending_evaluations.json`、`cases/case_XXXXXX/`、训练 CSV、`geometry_state.json`、`cfx_state.json` 与输入文件身份共同决定能否恢复。保持已有 `run_id`、阶段元数据和历史 CSV 的额外列（含 `physical_signature`）；不要静默覆盖候选、重编号、伪造完成凭据或把残留 `.res` / `CFX_Results.txt` 当成成功。
  原因：这些记录共同绑定设计点、算例和阶段结果，丢失其中的身份信息会造成重复计算或误用旧产物。
- 签名字段以 `blade_shape_signature.py` 的 `physical_inputs()` 为准：变量名称/顺序、声明工况、收敛设置、模板和几何脚本内容、CFX 求解器身份。新增影响同一设计点计算结果的输入应纳入签名；搜索设置、边界、核数和安装位置按现有排除规则处理。`rpm`、`mass_flow`、`alpha0` 当前是参与签名的声明值，实际边界条件以模板及 CFX 输入生成逻辑为准。
  原因：兼容判断既要区分物理输入和声明工况，也要允许只改变搜索策略或同版本安装位置时复用结果。
- 签名不兼容时列出差异并停止；只有用户通过 `--accept-input-change` 写明原因后才继续。保留旧行签名和确认记录，GUI 不记住接受原因。
  原因：旧结果是否适用于新输入需要明确判断，自动接受、改签名或复用上次原因会抹去这次判断的责任和证据。
- `.optimizer.lock` 保护输出目录与 CFX 算例的单写者流程。不要删除锁文件来绕过正在运行的进程。中断后先检查状态、日志与可能仍在运行的外部求解器，再决定是否续跑。
  原因：并发写入会破坏队列和阶段凭据，父进程退出也不保证外部求解器已经停止。
- 明确由操作员停止的未完成尝试按 `run_attempt.json` 和 token 停止凭据恢复：同一 run_id 的旧产物先可恢复地归档，再在预算内重算；无标记故障和数值失败不自动重试。旧版本中断须经 `retry-interrupted` 显式登记；历史数值失败须经 `reassess` 计划/应用核验后才能送审，不能手改凭据或 CSV。
  原因：中断重算是操作员明确要求的行为，不能把它与自动重试真正失败混为一谈；旧证据和人工决定仍需保留。
- 边界实验由 `write-boundary-plan` 生成冻结方案，`run-boundary` 按 `singles`、`pairs`、`extension` 阶段执行。保持方案签名、前置阶段检查、失败即停和送审时暂停的语义；不要自动重试已失败的真实计算。
  原因：冻结点位和阶段顺序保证实验可比较，失败或送审后绕过人工检查会改变后续阶段的前提。
- 保留严格残差门槛、送审条件和人工决定记录；形成工程结论时仍需人工检查残差、守恒、网格和工况一致性。
  原因：数值收敛与人工接受是两种不同的接受依据，完成凭据还不能代替网格和物理工况的工程审查。

## 修改与验证方式

- 在仓库根目录执行命令。Python 部分使用 Python 3.10+、NumPy、Pandas；相应采样/GP 功能可选 SciPy 和 scikit-learn，GUI 依赖 `requirements-gui.txt` 中的 PySide6 和用于停止求解进程的 psutil。
  原因：相对路径和测试夹具按仓库根目录定位，不同功能需要的依赖也不同。
- 本机路径放在未入库的 `blade_shape_local.ini`，模板为 `blade_shape_local.ini.example`；共享 JSON 的 `paths.*` 留空，显式非空 JSON 值仍优先。尚未配置的 `cfx_bin_dir`、`turbogrid_exe` 可分别从 `AWP_ROOT<版本>` 推导，多个安装版本须在 `[ansys] version` 指定。运行前核对 INI 和路径来源，不把机器路径提交回 JSON 或 PowerShell 默认值。
  原因：共享配置能在不同机器使用，明确的路径优先级和版本选择可避免加载错误工程或悄悄换用求解器。
- 真实几何、网格和 CFD 在具备 Windows、PowerShell 7、CFturbo、TurboGrid、CFX 及工程模板的环境运行；执行真实 `run` / `run-boundary` 前明确配置、输出目录和预算。
  原因：真实工程链依赖外部程序，并会消耗求解资源和改写运行产物。
- `write-candidate --offline` 需要可读的 CFturbo batch 基准模板并写候选文件；`--dry-run` 不带 `--offline` 时还会调用 PowerShell 生成中间文件，`check-pre` 会写检查目录。语法检查使用编译工具，工程计算使用真实模板。
  原因：这些 CLI 命令有文件副作用，合成测试夹具也不能代表真实工程几何。
- 已有训练或待处理数据的普通运行使用 `--resume`，以 `--max-new-cfd` 限制新增尝试数；即使预算为零，`run` 仍可能整理现有状态和导出结果。
  原因：显式续跑和预算限制保护历史记录与求解资源，零预算不等于纯读取。

普通续跑示例（会读取并可能整理现有状态）：

```powershell
python blade_shape_active_learning.py run --config blade_shape_config.json --resume --iterations 0 --max-new-cfd 0
```

- 不提交 `training_data.csv`、Pareto/诊断/汇总 CSV、`cases/`、`ansys_work/` 和 `Templates/` 等本地工程产物。
  原因：这些文件依赖具体工程和运行状态，不属于可共享的源码或配置。
- 修改 Python 源码时，对涉及的文件执行 `python -m py_compile`，再按下列改动范围运行测试；恢复、锁或 CFX 改动完成针对性测试后运行全套测试。
  原因：编译检查发现语法错误，行为测试检查源码变化是否保持现有计算和恢复契约。
- 改命令构造、配置或 GUI 数据层时运行 `tests/test_gui.py`；涉及变量映射、配置入口、声明工况或命令文件版本时结合 `tests/test_config_seams.py`。
  原因：这些测试覆盖 GUI 到 CLI 的参数传递和配置与实际执行之间的接口。
- 改搜索和边界逻辑时运行 `tests/test_refinement.py`。
  原因：该测试覆盖活动/固定切片、局部搜索和分阶段实验的行为。
- 改恢复、锁或 CFX 时运行 `tests/test_reliability.py`，并按改动涉及的签名、收敛或人工审核行为选择对应测试（含 `tests/test_review.py`）。
  原因：恢复与接受判定直接决定能否复用已有产物，需要验证中断、失败和审核边界。
- 改 GUI 界面时在具备 PySide6 的环境运行界面测试；报告测试结果时区分通过与依赖缺失导致的跳过。
  原因：没有创建 Qt 控件的测试不能验证界面布局、信号和状态恢复。

全套测试命令：

```powershell
python -m unittest discover -s tests -v
```

- 新增测试使用临时目录、合成夹具和外部调用替身，覆盖行为和恢复边界；真实 Windows 工程链的验证使用工程模板跑单算例。
  原因：离线测试应独立于本机工程数据，真实程序与模板的兼容性需要工程环境验证。
