# 离心叶轮主叶片形状优化

`compressor_blade_optimizition` · CFD 反馈驱动的主动学习优化流程

本项目使用真实 CFD 反馈优化离心叶轮**主叶片平均线**。流程串联 CFturbo、ANSYS TurboGrid 和 ANSYS CFX，以叶轮效率 `Efficiency` 与质量流量 `MassFlow` 为双最大化目标。主动学习代理模型用于选择下一批计算点；最终 Pareto 前沿只从成功完成的真实 CFD 记录中生成。

仓库提供命令行流程和 PySide6 桌面界面。GUI 读取同一份配置与结果文件，并通过子进程调用命令行入口，不另行实现优化或 CFD 逻辑。

## 工作流程

1. 从 CFturbo batch 模板读取 hub/shroud 基准平均线和堆叠角。
2. 在配置边界内生成主叶片的 12 个角度偏移量，并检查几何约束与重复设计。
3. 为候选建立 `cases/case_XXXXXX/`，调用 CFturbo、TurboGrid 和 CFX 获得真实结果。
4. 将每次尝试及其成功或失败状态写入 `training_data.csv`；只使用成功样本训练代理模型。
5. 在当前搜索空间内筛选下一批候选，完成 CFD 后更新工程容差前沿及严格前沿。

当前模板没有启用 splitter blade，因此本项目不包含分流叶片设计变量。`PressureRatio`、`totalpressureratio` 和 `Power` 会保存以供工程审查，不参与当前双目标优化。

## 仓库内容

| 路径 | 用途 |
| --- | --- |
| `blade_shape_active_learning.py` | DOE、几何检查、主动学习、续跑、诊断和 Pareto 导出入口 |
| `Run-BladeShapeGeometryMeshing.ps1` | CFturbo 几何更新与 TurboGrid 网格生成 |
| `blade_shape_cfx_runner.py` | CFX-Pre、求解、后处理与结果解析 |
| `blade_shape_config.json` | 路径、运行预算、变量边界、搜索空间及约束配置 |
| `blade_shape_acquisition.py`、`blade_shape_refinement.py` | 候选筛选、局部搜索和边界实验辅助逻辑 |
| `blade_shape_pending.py`、`blade_shape_runtime.py` | 待处理队列、恢复与运行时保护 |
| `blade_shape_flow_diagnostics.py` | 对已有 `.res` 做只读熵增和近叶片流向角诊断；不改变优化目标 |
| `blade_shape_incidence_validation.py` | 可变目标路径的展向攻角验证、旧指标复现、工况比较及预算化进口角敏感性试验 |
| `blade_shape_aca_extraction.py` | 用保存的 CFX-Post session 从已有 `.res` 导出 20 点 Velocity Beta ACA，并整理为 `legacy` 所需的两列 CSV |
| `blade_gui/` | 六页桌面控制台：总览、分析、设置、运行、算例浏览、验证 |
| `tests/` | Python 与离屏 GUI 测试；合成模板仅供测试 |

算法、边界实验和产物字段见 [命令行详细说明](README_blade_shape_active_learning.md)；界面操作见 [GUI 详细说明](README_blade_gui.md)。

进出口熵增与叶片角匹配的初步实测、独立提取命令和单位问题见 [流动诊断评估](README_flow_diagnostics.md)。目前该诊断不会修改现有训练数据、Pareto 前沿或 CFD 续跑状态。

目标叶轮的验证入口和可变路径示例见 [展向攻角验证](README_incidence_validation.md)。支持 `init`、`extract`、`sweep`、`aca`、`legacy`、`compare`、`plan`、`run`，真实敏感性计算复用原 CFD 链并写独立目录；需明确 `--max-new-cfd`。

## 运行条件与配置

- Python 3.10+；核心 Python 依赖为 NumPy、Pandas。SciPy、scikit-learn 用于相应的采样与 GP/Kriging 实现；缺少 scikit-learn 时，代码会回退到 RBF-ridge 集成代理。
- 桌面界面额外需要 PySide6，可通过 `requirements-gui.txt` 安装。
- **真实几何、网格和 CFD 流程**需要 Windows、PowerShell 7、CFturbo、ANSYS TurboGrid 和 ANSYS CFX。本机路径不入库：`blade_shape_config.json` 的 `paths.*` 留空，由同目录下未跟踪的 `blade_shape_local.ini` 的 `[paths]` 段补齐（JSON 中写了非空值的以 JSON 为准；环境变量 `BLADE_SHAPE_LOCAL_INI` 可指向别处）。首次使用先复制模板并按本机修改：

  ```powershell
  Copy-Item blade_shape_local.ini.example blade_shape_local.ini
  ```

  模板里是原先写在 JSON 中的 CFturbo 2025.2.2 / ANSYS 2025 R1 路径。保持字符串完全一致时，已有运行记录的输入签名不变，可照常续跑。界面「项目设置」保存时只把改动过的路径写回该 INI，而不是 JSON。`*.ini` 已加入 `.gitignore`。

  `cfx_bin_dir` 和 `turbogrid_exe` 在 JSON 与 INI 中都留空（或删掉这两行）时，由 ANSYS 安装程序设置的环境变量 `AWP_ROOT<版本>` 推导为 `<根目录>\CFX\bin` 与 `<根目录>\TurboGrid\bin\cfxtg.exe`。版本用 INI 的 `[ansys] version = 251` 指定；不写时只在本机恰好装了一个版本时自动采用，装了多个版本则不推导并提示指定，以免新装一个版本就悄悄换掉求解器（`cfx_bin_dir` 属于 CFX 续跑签名）。

  INI 没有生效时，用下面的只读命令查看每个路径的实际取值与来源（`ini`、`AWP_ROOT251` 或 `json`），以及 INI 是否被找到、是否被 JSON 非空值覆盖、是否被 Windows 存成了 `blade_shape_local.ini.txt`；界面「项目设置 → 路径配置」也显示同样的说明：

  ```powershell
  python blade_shape_local_config.py blade_shape_config.json
  ```
- `Templates/` 中的基准工程与求解器生成文件不随仓库提供。运行候选生成前，至少要准备配置所指向的 `cft_batch_template`；完整流程还需要其余模板与软件路径。

在仓库根目录安装 Python 依赖：

```powershell
python -m pip install numpy pandas
python -m pip install -r requirements-gui.txt
```

如需使用 scikit-learn 的 GP 模型及 SciPy 功能，再安装对应可选依赖。设计变量边界和几何约束以 `blade_shape_config.json` 为配置入口；不要直接改算法或 PowerShell 中的数值来替代配置。

## 快速开始

以下命令均在**仓库根目录**执行。`--config` 放在子命令后面。

```powershell
# Python 语法检查
python -m py_compile blade_shape_active_learning.py blade_shape_cfx_runner.py

# 生成一个候选并检查 CFturbo XML 生成，不启动真实 CFD
python blade_shape_active_learning.py write-candidate --config blade_shape_config.json --index 0 --dry-run

# 仅检查 Python 候选与几何规则；仍需可读取的 CFturbo batch 基准模板
python blade_shape_active_learning.py write-candidate --config blade_shape_config.json --index 0 --dry-run --offline
```

在配置好 Windows 工程环境后，可以先以最多一次新增 CFD 尝试做小规模检查：

```powershell
python blade_shape_active_learning.py run --config blade_shape_config.json --initial-samples 1 --iterations 0 --max-new-cfd 1
```

已有训练数据或待处理算例时，`run` 要求显式使用 `--resume`；每次调用可用 `--max-new-cfd` 限制新增评估尝试数。需要查看模型与搜索诊断时运行：

```powershell
python blade_shape_active_learning.py diagnose --config blade_shape_config.json
```

边界实验使用 `write-boundary-plan` 生成冻结方案，再按 `singles`、`pairs`、`extension` 阶段分别执行；命令和前置条件见[命令行详细说明](README_blade_shape_active_learning.md#version-2-boundary-plans-local-levels-and-independent-stages)。

### 桌面界面

```powershell
python -m blade_gui
```

Windows 上也可双击 `run_gui.bat`。只查看从 CFD 主机拷回的结果时，可指定包含 `training_data.csv` 的目录：

```powershell
python -m blade_gui --data-dir "D:\copied\blade_al_runs"
```

`--data-dir` 仅覆盖界面的**只读数据视图**。运行控制仍使用当前配置中的 `paths.output_dir`，界面会分别提示实际运行目录与查看目录。

## 主要输出与结果边界

输出位置由 `paths.output_dir` 决定（通常在 `blade_shape_local.ini` 中设置）：

| 文件或目录 | 内容 |
| --- | --- |
| `training_data.csv` | 全部真实 CFD 尝试、设计变量、指标、阶段、状态与失败信息 |
| `pareto_front.csv` | 按配置工程容差计算的非支配**成功**样本 |
| `pareto_front_strict.csv` | 不使用工程容差的严格 Pareto 前沿 |
| `iteration_summary.csv` | 本次调用中的逐次尝试摘要 |
| `active_learning_diagnostics.csv` | 候选来源、评估前预测、不确定度、真实值及误差等诊断 |
| `cases/` | 每个算例的输入、生成文件与阶段日志 |

代理预测、dry-run 结果和失败记录都不能作为真实成功标签进入最终 Pareto 前沿。求解器退出、存在 `.res` 文件或出现后处理文本本身也不足以证明 CFD 数值质量；工程使用前仍需检查收敛、守恒和工况一致性。

## 测试

```powershell
python -m unittest discover -s tests -v
```

未安装 PySide6 时，依赖 Qt 的界面测试会跳过。测试中的 `synthetic_meanline.cft-batch` 是合成夹具，不能用于工程计算。在 macOS/Linux 上可以检查 Python 逻辑和离屏界面，但不能据此认定 Windows 外部工程链已通过验证。
