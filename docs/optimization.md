# 优化流程

本文介绍优化程序 `blade_shape_active_learning.py` 的工作方式和用法。目录：

1. [一个算例是怎么算出来的](#1-一个算例是怎么算出来的)
2. [设计变量](#2-设计变量)
3. [优化目标与 Pareto 前沿](#3-优化目标与-pareto-前沿)
4. [每一轮怎么挑选新点](#4-每一轮怎么挑选新点)
5. [运行、续跑与中断处理](#5-运行续跑与中断处理)
6. [CFD 结果什么时候算成功](#6-cfd-结果什么时候算成功)
7. [检查代理模型准不准](#7-检查代理模型准不准)
8. [边界实验](#8-边界实验)
9. [关键设计决策](#9-关键设计决策)

## 1. 一个算例是怎么算出来的

每个候选设计都有自己的目录 `cases/case_XXXXXX/`，按下面的顺序处理：

| 步骤 | 负责的程序 | 做什么 |
| --- | --- | --- |
| 几何 | `Run-BladeShapeGeometryMeshing.ps1` 调用 CFturbo | 把 12 个偏移量写进 CFturbo batch 文件，导出叶片曲线 |
| 网格 | 同上，调用 TurboGrid | 生成 `Impeller_Mesh.gtm` |
| 前处理 | `blade_shape_cfx_runner.py` 调用 CFX-Pre | 把网格装进 CFX 模板，生成 `.def` |
| 求解 | CFX-Solver | 求解，并检查残差是否达标（见第 6 节） |
| 后处理 | CFX-Post | 读出效率、流量、压比、功率，写入 `CFX_Results.txt` |
| 记录 | 优化程序 | 把结果追加到 `training_data.csv` |

任何一步失败，这个算例都会以 `status=failed` 记入 CSV，并在 `failure_stage` 列写明失败在哪一步。失败的算例不会被拿去训练模型，也不会进入 Pareto 前沿。

## 2. 设计变量

12 个变量都是相对基准模板（`cft_batch_template`）的**偏移量**，单位是度：

| 变量 | 含义 |
| --- | --- |
| `hub_beta_0..4_deg_offset` | hub 平均线上 5 个 beta 角控制点，0 号在前缘，4 号在后缘 |
| `shroud_beta_0..4_deg_offset` | shroud 平均线上 5 个 beta 角控制点 |
| `hub_theta_deg_offset`、`shroud_theta_deg_offset` | hub / shroud 的堆叠角 |

两端的 beta 控制点会同步写入 CFturbo 里的进口角 `Beta1` 和出口角 `Beta2`。

**当前搜索 7 个，固定 5 个。** 配置中的 `search.active_variables` 列出参与搜索的变量：hub_beta_0、hub_beta_3、hub_beta_4、shroud_beta_0、shroud_beta_2、shroud_beta_3、hub_theta。其余 5 个在 `search.fixed_variables` 里固定为某个具体值。写入 CFturbo 时仍然是完整的 12 个值。

**上下限和几何约束都在配置里改**，不要去改代码：
- 每个变量的上下限在 `variables` 里。hub_beta_3、hub_beta_4 的上下限不对称，因为基准模板的 hub 出口段已经很陡，这样设置是为了避免让它更陡。
- `constraints` 限制总偏移量、相邻控制点的角度跳变，以及判定两个设计"重复"的距离。

> `variables` 里必须正好是这 12 个名称，缺少或多出都会在加载配置时报错。名称和顺序同时对应代码中的向量下标、CFturbo 写入位置和 CSV 列，不要随意调整顺序。

**工况参数**：配置里 `runtime` 的 `rpm`、`mass_flow`、`alpha0` 只是对工况的**声明**，用于记录和续跑核对，并不会写进 CFturbo 或 CFX。真正的边界条件以 CFX 模板为准。`runtime.n_blades` 会作为叶片数传给几何脚本（决定 TurboGrid 的周期角），也用来把单通道的流量和功率换算成整轮。

## 3. 优化目标与 Pareto 前沿

两个目标都是越大越好：`Efficiency`（效率）和 `MassFlow`（质量流量）。当前 CFX 边界条件固定了进出口静压，所以 `MassFlow` 代表"给定压差下叶轮能通过多少流量"。

CFD 结果本身有数值误差。如果两个设计的效率只差 0.0001，很难说哪个真的更好。因此程序会输出两份前沿：

- `pareto_front.csv`：**工程容差前沿**。效率差在 0.0003 以内、流量差在 0.006 以内的设计视为打平，避免把数值噪声当成"更优"。容差在配置的 `pareto.tolerances` 里设置。
- `pareto_front_strict.csv`：严格数学定义的前沿，供对照。

前沿只从 `status=success` 的真实 CFD 记录中计算，模型预测值永远不会进入前沿。

## 4. 每一轮怎么挑选新点

### 初始采样（DOE）

在 7 个搜索变量的范围内用拉丁超立方采样均匀撒点，过滤掉违反几何约束或与已有设计太接近的点，不够时再随机补足。配置默认 48 个初始样本，已经全部算完。

### 代理模型

代理模型是一个"用已有 CFD 结果去猜新设计性能"的近似模型，猜一次只要几毫秒，用来筛选值得真算的候选。

- **主模型**：高斯过程（GP / Kriging），用全部历史成功样本训练，输入是全部 12 个变量。它除了给出预测值，还能给出"有多不确定"。
- **对照模型**：只用 5 个固定变量与当前取值一致的样本训练，输入是 7 个变量。这类样本达到 12 个以后才启用，用来和主模型比较谁预测得更准。

两个模型的预测值都会在 CFD 开始之前保存下来，事后再和真实结果对比。

模型种类由配置的 `surrogate.model` 指定：`gp` 或 `rbf_ridge_ensemble`。如果 GP 建不起来（没装 scikit-learn，或者样本少于 3 个），就改用 `surrogate.fallback_model` 指定的模型；把它设为 `none` 时，程序会直接停下，不换模型。

### 挑选候选

1. 在代理模型上用遗传算法（NSGA-II）搜索，并另外生成一个随机候选池。
2. 大约 70% 的候选来自"局部区域"，其余在整个范围内撒点。局部区域是围绕当前 Pareto 前沿上三个代表点（效率最高、流量最高、两者均衡）画的小方框。
3. 每批默认 3 个点，各自承担一个角色：
   - **ehvi**：预计能让 Pareto 前沿扩张最多的点（EHVI 即"期望超体积改进"，衡量前沿能被推出去多少）；
   - **uncertainty**：模型最没把握的点，用来改进模型；
   - **diversity**：离已有样本最远的点，避免扎堆。
4. 每个候选都要通过几何约束和重复检查。

**局部区域会自动缩放**：连续 3 个点都没有让前沿明显改善，方框边长减半；连续 3 个点都有改善，边长加倍。默认半径是变量范围的 15%，最小 5%，最大 30%，在 `refinement.local_search` 里设置。当前状态保存在 `local_search_state.json`。

如果 `--batch-size 1`，程序每算完一个点就重新训练模型，并且每次都用第一个角色（默认 ehvi）。

## 5. 运行、续跑与中断处理

```powershell
python blade_shape_active_learning.py run --config blade_shape_config.json --resume --iterations 2 --batch-size 3 --max-new-cfd 6
```

| 参数 | 作用 |
| --- | --- |
| `--resume` | 输出目录已有数据时必须加，表示接着之前的结果继续 |
| `--iterations` | 跑几轮主动学习；设为 0 时只处理之前没完成的点，不开新的一轮 |
| `--batch-size` | 每轮几个点 |
| `--max-new-cfd` | 这次运行最多新增几个 CFD 尝试，作为安全上限；设为 0 时只整理已有记录，不新算 |
| `--initial-samples` | 初始采样点数 |

**中断后怎么办**：程序在每个点开始计算前，先把它登记到 `pending_evaluations.json`，包括坐标、预测值和预留的算例编号。下次带 `--resume` 运行时，会先处理这些未完成的点，再挑新点。如果某个点的结果已经写进 CSV、只是收尾工作没做完，程序会直接补齐收尾，不会重算。

**几何脚本版本**：程序在开始任何算例前，会检查 `paths.geometry_script_path` 指向的 PowerShell 脚本是否声明了 `BladeCount` 参数。如果是旧版本的脚本副本，会直接报错停止，请换成仓库里的新版本。脚本的指纹也属于续跑核对的一部分：换了脚本之后，用旧脚本登记的待续跑点和边界方案会拒绝继续（提示 "Inputs changed"）。所以请先把它们跑完或检查完，再换脚本。

**不要删除 `.optimizer.lock`**。它防止两个进程同时写同一个输出目录或算例。进程退出后锁会自动释放，锁文件本身留在原地是正常的。中断后先确认没有残留的 CFX 求解进程（父进程停了，求解器可能还在跑），再续跑。

**旧的残留算例**：如果 `cases/` 里有目录没在任何记录里登记过，程序会在挑新点之前列出来。这些目录不会被当成成功结果导入，需要人工查看，必要时用新算例重算。

## 6. CFD 结果什么时候算成功

求解器正常退出、生成了 `.res` 文件，**还不算成功**。程序要求：

1. 读取 `.out` 文件中**最后一步**的 RMS 残差，动量（U/V/W-Mom）、质量（P-Mass）、能量（H-Energy）方程都要达到 `1e-5`，或配置的更严格的值。湍流方程（K-TurbKE、O-TurbFreq）只记录、不卡，这和 CFX 自己判断收敛停算的规则一致。实测中 CFX 曾在 O-TurbFreq 为 2.2e-5 时报告收敛并停下，如果也卡湍流，这类点既通不过，又因为不是步数用完而没有续算机会。
2. 如果没达标、但原因是步数用完了（不是崩溃或被中断），程序会从最后的流场出发**再追加最多 2000 步**（可设为 1500–2000），步数重新计数。
3. 追加之后仍不达标，记为 `failure_stage=solve` 的失败。日志和结果文件都会保留，供人工检查。

因此一个设计点最多可能调用两次求解器，但在 `--max-new-cfd` 里只算一个点。

相关设置在配置的 `cfx_convergence` 中：

```json
"cfx_convergence": {
  "rms_target": 0.00001,
  "restart_iterations": 2000,
  "flow_analysis": "Flow Analysis 1"
}
```

`flow_analysis` 必须和 CFX 模板里的 Flow 名称一致。

**续跑时的核对**：每个算例目录下有 `geometry_state.json` 和 `cfx_state.json`，记录了这个算例用的输入文件指纹和各阶段的完成情况。续跑时如果发现输入变了、或者某阶段没有完成记录，程序不会把残留文件当成成功，而是要求人工检查。

**跑之前先检查输入**：`check-pre` 只生成 CFX-Pre 的输入文件并检查路径，不启动求解。请用一个独立的空目录：

```powershell
python blade_shape_cfx_runner.py check-pre --config blade_shape_config.json --working-dir CHECK_DIRECTORY
```

再加上 `--def 已有的.def`，还会只读地检查 `.def` 里转子的转速是否和配置中声明的 `runtime.rpm` 一致，不一致时返回 1。

> 残差达标只说明数值上收敛了，不代表网格无关、守恒、工况都没问题。工程结论仍需人工审查。

## 7. 检查代理模型准不准

```powershell
python blade_shape_active_learning.py diagnose --config blade_shape_config.json
```

这条命令不跑 CFD，只统计已有记录中"事先预测值"和"事后真实值"的差距，输出：

| 文件 | 内容 |
| --- | --- |
| `role_diagnostics.csv` | 当前固定切片下，主模型和对照模型按角色统计的平均误差、偏差、±2σ 覆盖率 |
| `role_diagnostics_history.csv` | 历史上主模型的误差统计 |
| `training_slice_audit.csv` | 每个历史样本是否位于当前固定切片上 |
| `local_diagnostic_gate.json` | 综合判断：最近 18 个点中 ehvi 角色的点至少 6 个，平均误差不超过工程容差，并且真实值落在预测 ±2σ 内的比例达到 5/6（即 6 个点最多错 1 个，可在 `refinement.diagnostic_gate` 中调整），才判定通过 |

诊断门只是参考：它不会自动启动或阻止任何实验，也不能证明 CFD 本身收敛可信。

另外，模型给出的不确定度会根据历史误差自动放大校正（同一切片、同一模型至少有 6 个历史误差时启用），以免模型"过度自信"。

## 8. 边界实验

**目的**：在一个已有的好设计附近，有计划地把 4 个关键变量（hub_beta_4、shroud_beta_0、hub_beta_0、hub_theta）各推一小步，看性能怎么变。这是事先设计好的对照实验，不由代理模型选点。

### 生成方案

先从成功算例中选一个参考点，再生成冻结方案：

```powershell
python blade_shape_active_learning.py write-boundary-plan --config blade_shape_config.json --center-run-id CASE_ID --plan boundary_plan_v2.json
```

方案分三个阶段：

| 阶段 | 内容 | 点数 |
| --- | --- | --- |
| `singles` | 参考点重算一次，加上 4 个变量各自单独移动一步 | 5 |
| `pairs` | 4 个变量两两组合同时移动 | 6 |
| `extension` | 把 hub_beta_4 推到 5.0°，超出正常上限 4.5°，并配一个 4.5° 的对照点（已有合适对照点时复用） | 1 或 2 |

每个变量的步长在配置的 `refinement.boundary_levels` 里设置。默认 beta 走 0.5°（允许 0.1–1.0°），theta 走 0.25°（允许 0.05–0.5°）。这些是初始值，不是经过验证的最优步长。如果向上的空间不够，程序会改为向内走。

方案文件生成后不能手工修改，因为它带有内容签名。输入文件或配置一旦变化，执行时会被拒绝，此时需要重新生成方案。违反几何约束的点会在方案里标明原因，所在阶段不能执行。程序不会为了让方案通过而放宽限制。

### 执行

```powershell
python blade_shape_active_learning.py run-boundary --config blade_shape_config.json --plan boundary_plan_v2.json --stage singles --max-new-cfd 5
# 看完 singles 结果没问题，再跑下一阶段
python blade_shape_active_learning.py run-boundary --config blade_shape_config.json --plan boundary_plan_v2.json --stage pairs --max-new-cfd 6 --resume
python blade_shape_active_learning.py run-boundary --config blade_shape_config.json --plan boundary_plan_v2.json --stage extension --max-new-cfd 2 --resume
```

- 只要有一个点失败，就立即停止，且不会自动重算。需要重算时请人工检查后建立新实验。
- 预算用完时停下的状态是 `budget_exhausted`，加 `--resume` 可以接着跑。
- 进度保存在 `boundary_plan_v2.state.json`，预测与真实结果对比保存在 `boundary_diagnostics.csv`。
- 边界实验的成功结果会进入训练数据，也可以进入 Pareto 前沿。

这些实验给出的是局部对比，不能当成完整的响应面或稳定性证明。

## 9. 关键设计决策

以下决策来自项目早期的讨论和运行结果，改动前请先了解背后的原因。

**为什么只有效率和流量两个目标。** 最初是效率、静压比、流量三个目标。但当前 CFX 边界固定了进出口静压，静压比几乎不变。而直接优化总压比，可能会鼓励程序靠提高速度来刷高总压，导致局部马赫数过高。所以目标改为效率 + 流量，压比和功率只做记录。如果以后想把压比重新作为目标，应该先改工况：固定入口总压总温加出口流量，或者为每个几何搜索同一流量下的出口静压。如果发现马赫数过高，应该加一个 `MaxMach` 约束，而不是把总压比设成目标。

**工程容差怎么来的。** 48 组初始样本中，CFX 质量不平衡和能量不平衡的中位数都约为 0.08%。据此把效率容差定为 0.0003、流量容差定为 0.006。判断"谁更好"时，应同时考虑容差、重复算例和网格收敛，不要只看严格前沿。

**为什么从 12 维降到 7 维。** 根据 Sobol 敏感性分析和后续讨论，把 5 个变量固定在 `search.fixed_variables` 给出的值上，只搜索剩下的 7 个，以节省 CFD 预算。这些固定值不会随前沿变化而自动更新。写入 CFturbo 的仍是完整 12 个值。

**降维后模型变得不准。** 降维后首批 6 个点（case_000078–083）全部算成功，但预测平均误差是效率容差的 3.4 倍、流量容差的 4.9 倍。原因是旧样本大多不在新的固定切片上，模型把被冻结变量带来的差异当成了噪声。于是增加了只用同切片样本的对照模型，并决定先在切片内多做几轮局部校准，等误差降到容差附近再做边界实验。

### 数据单位

CFX 后处理脚本 `Extract_Results.cse` 直接输出 CFX 内部单位（mm、g、K）下的数值。以 case_000000 为例，CSV 中 `MassFlow=4.18154` 实际约为 0.00418 kg/s（10 个叶道合计），即这个数相当于 g/s。`Power` 也是内部单位下的数值。

历史数据全部是这个单位，代理模型和流量容差 0.006 也基于它。**不要在新算例里改成 SI 后追加到旧 CSV**，否则两种单位混在一起会破坏模型和前沿。如果要改，需要对历史数据做有版本、可回滚的迁移，并同步缩放容差。验证与诊断工具（见 [验证与诊断](validation.md)）始终输出 SI 单位，不写训练 CSV。
