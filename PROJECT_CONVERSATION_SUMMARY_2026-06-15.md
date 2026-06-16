# 叶轮叶型主动学习优化项目对话总结

更新时间：2026-06-15

## 项目目标

当前项目位于 `D:\blade optizamation`，目标是在原有几何参数优化代码基础上，建立一套用于离心压缩机叶轮叶型优化的真实 CFD 闭环主动学习流程。

整体链路为：

```text
DOE / 主动学习采样
-> 修改 CFturbo batch 中的叶型参数
-> CFturbo 导出 TurboGrid 曲线
-> TurboGrid 生成 Impeller_Mesh.gtm
-> CFX-Pre 生成 Impeller.def
-> CFX-Solver 求解
-> CFX-Post 提取性能指标
-> 写回 training_data.csv
-> 更新代理模型和 Pareto 前沿
```

## 当前代码文件

主要新增和维护的文件包括：

```text
blade_shape_active_learning.py
blade_shape_cfx_runner.py
Run-BladeShapeGeometryMeshing.ps1
blade_shape_config.json
README_blade_shape_active_learning.md
Templates/
```

其中：

- `blade_shape_active_learning.py`：负责 DOE、主动学习、代理模型、EGO/MOEGO acquisition、真实 CFD 调用和 Pareto 导出。
- `Run-BladeShapeGeometryMeshing.ps1`：负责把候选叶型写入 CFturbo batch，并调用 CFturbo 和 TurboGrid。
- `blade_shape_cfx_runner.py`：负责 CFX-Pre、CFX-Solver、CFX-Post。
- `blade_shape_config.json`：保存路径、变量上下界、运行参数、工程 Pareto 容差等配置。

## 模板迁移和链路检查

用户已将原 `Templates` 文件夹移动到当前项目目录内：

```text
D:\blade optizamation\Templates
```

配置中的模板路径已从旧的：

```text
F:\optimazition\Templates
```

切换为当前项目内路径。

`BaseMeshing.tst` 中用于每个 case 动态替换的占位符被保留，包括：

```text
{BLADE_COUNT}
{PROFILE_CURVE}
{TIP_CLEARANCE}
{HUB_CURVE}
{SHROUD_CURVE}
{PERIODIC_ANGLE}
```

生成到具体 case 的 `run_turbogrid.tst` 时，这些占位符会被替换为当前 case 的实际曲线路径和参数。模板中的旧 `State Filename` 路径已改为当前项目路径。

已通过 dry-run 检查：

```text
python blade_shape_active_learning.py write-candidate --index 0 --dry-run
```

检查内容包括：

- CFturbo batch 中 `Beta1/Beta2` 与候选端点一致。
- `TMeanLine Index=0/1` 的 `InnerProgPoints.y` 正确替换。
- `lePos` 正确替换。
- 生成的 `run_turbogrid.tst` 没有旧路径和未替换占位符。

## 当前叶型变量

当前第一版叶型优化使用 12 个变量，均为相对 `0908-2_modified.cft-batch` 当前叶型的 offset：

```text
hub_beta_0_deg_offset
hub_beta_1_deg_offset
hub_beta_2_deg_offset
hub_beta_3_deg_offset
hub_beta_4_deg_offset
shroud_beta_0_deg_offset
shroud_beta_1_deg_offset
shroud_beta_2_deg_offset
shroud_beta_3_deg_offset
shroud_beta_4_deg_offset
hub_theta_deg_offset
shroud_theta_deg_offset
```

`0908-2_modified.cft-batch` 后来被用户更新为只保留叶轮，不再包含 Stator/Volute。此前因包含蜗壳和多个部件导致 CFturbo 导出失败，后来已修正。

当前 bounds 已根据 `0908-2_modified.cft-batch` 的叶轮参数重新制定，`max_beta_offset_deg` 为 `6.0`，`max_theta_offset_deg` 为 `1.5`。

## DOE 设置和结果

当前 DOE 采样方法为 LHS：

```text
scipy.stats.qmc.LatinHypercube
```

如果环境没有 scipy，则退回普通均匀随机采样。

DOE 流程为：

```text
LHS 生成候选
-> 几何约束过滤
-> 距离去重
-> 不足时随机有效样本补齐
-> 真实 CFD
```

之前建议：

```text
12 维变量正式 DOE 建议 36-48 个样本
```

实际已完成：

```text
48 个 DOE 样本
48 个全部 CFD success
```

当前 `training_data.csv` 在启动主动学习后已有 56 行，说明 48 个 DOE 后已追加若干主动学习样本。

## 优化目标调整

最初目标为三目标最大化：

```text
Efficiency
PressureRatio
MassFlow
```

后来根据当前 CFX 边界条件重新判断：

- 当前入口/出口静压基本固定。
- 静压比 `PressureRatio` 只能在很小范围内波动。
- 直接优化总压比可能鼓励通过速度项刷高总压，导致局部 Mach 过大。

因此目标已调整为双目标最大化：

```text
Efficiency
MassFlow
```

以下指标仍保留记录，但不参与当前优化目标：

```text
PressureRatio
totalpressureratio
Power
```

当前代码中：

```python
OBJECTIVE_COLUMNS = ["Efficiency", "MassFlow"]
```

这会统一影响：

- Pareto 判断
- 代理模型训练标签
- NSGA-II 代理模型候选搜索
- EHVI / EGO acquisition
- 主动学习诊断输出

## 工程 Pareto 容差

根据 48 组 DOE 结果和 CFX 求解不平衡，加入了工程容差 Pareto。

当时观察到：

```text
P-Mass imbalance median 约 0.085%
H-Energy imbalance median 约 0.0807%
```

当前双目标工程容差为：

```json
"pareto": {
  "use_engineering_tolerance": true,
  "tolerances": {
    "Efficiency": 0.0003,
    "MassFlow": 0.006
  }
}
```

输出文件：

```text
pareto_front.csv
pareto_front_strict.csv
```

其中：

- `pareto_front.csv`：使用工程容差的 Pareto。
- `pareto_front_strict.csv`：严格数学 Pareto，用于对照。

后续相似 CFD 优化、网格无关性或 Pareto 判断时，需要提醒用户考虑工程容差、重复算例、网格收敛或 GCI，而不是只相信严格数学 Pareto。

## 主动学习和 EGO 部分

当前主动学习部分仍在代码中。

主要函数为：

```text
fit_surrogate()
nsga2_candidates()
select_acquisition()
approximate_expected_hvi()
```

对应逻辑：

```text
真实 CFD 样本训练代理模型
-> 代理模型上 NSGA-II 搜索候选
-> 通过近似 EHVI 计算 EGO/MOEGO acquisition
-> 加入距离多样性
-> 选择下一批真实 CFD 点
```

当前代理模型优先使用：

```text
Gaussian Process / Kriging
```

如果 sklearn 不可用，则退回：

```text
RBF-ridge ensemble
```

当前 acquisition 得分：

```text
score = normalized(EHVI) + 0.15 * normalized(distance_to_existing)
```

## 主动学习诊断记录

已新增主动学习诊断文件：

```text
active_learning_diagnostics.csv
```

每个主动学习选中的真实 CFD case 会记录：

```text
iteration
run_id
status
selection_rank
selection_source
acquisition_score
ehvi
distance_to_existing
pareto_rows_before
pareto_rows_after
pred_Efficiency
std_Efficiency
true_Efficiency
prediction_error_Efficiency
pred_MassFlow
std_MassFlow
true_MassFlow
prediction_error_MassFlow
case_dir
failure_stage
message
```

`selection_source` 可为：

```text
nsga
random_pool
fallback_random
```

这样后续可以判断：

- 主动学习点为什么被选中。
- EHVI 是否在下降。
- 代理模型不确定性是否合理。
- 预测值和真实 CFD 的偏差是否收敛。
- 每轮 Pareto 是否有改善。

## 已发生的运行和归档

### 第一次 DOE 失败

第一次启动 48 组 DOE 时，因 `0908-2_modified.cft-batch` 仍包含 Stator/Volute，CFturbo 导出失败：

```text
Exactly one vaned component has to be selected for export.
Cutwater creation failed.
```

这批失败样本已归档：

```text
blade_al_runs\archive_failed_geometry_20260614_160237
```

### 成功 DOE

用户删除蜗壳等部件后，重新运行 DOE，48 个样本全部成功。

日志前缀：

```text
doe48_20260614_160247
```

### 错误批次的主动学习

曾误将“10 组主动学习”理解为 10 个新增点，因此启动了：

```text
10 轮 × 每轮 1 点
```

得到 `case_000048..case_000057`，后来根据用户说明，这不是期望批次。

这批旧主动学习结果已归档：

```text
blade_al_runs\archive_al_batch1_20260615_122220
```

当前训练集已恢复为 48 个 DOE 样本后重新启动主动学习。

## 当前正在运行的主动学习

用户澄清：

```text
10 组 = 10 个循环，每循环 3 点
```

因此当前启动命令为：

```text
python blade_shape_active_learning.py run --initial-samples 48 --iterations 10 --batch-size 3 --max-new-cfd 30 --resume
```

启动时的后台主进程：

```text
PID 67924
```

日志文件：

```text
blade_al_runs\logs\al10x3_20260615_122332.out.log
blade_al_runs\logs\al10x3_20260615_122332.err.log
blade_al_runs\logs\al10x3_20260615_122332.meta.json
```

截至本总结写入时：

```text
python 主进程 67924 仍在运行
cfx5solve.exe 进程 75380 正在运行
training_data.csv 当前 56 行
active_learning_diagnostics.csv 已生成
```

这说明当前 10 轮 × 3 点的主动学习任务正在进行中，尚未全部完成。

## 当前注意事项

1. 当前目标是 `Efficiency + MassFlow`，不要再把 `PressureRatio` 或 `totalpressureratio` 当成主目标，除非后续修改边界条件为统一质量流量或统一工况点。

2. 若未来要重新引入压比目标，建议先改变 CFD 工况：

```text
入口总压/总温固定
出口质量流量固定
```

或：

```text
每个几何通过出口静压搜索相同质量流量点
```

3. 当前固定进出口静压下，`MassFlow` 是有效目标，代表给定压差下的通过能力。

4. 如果后续发现总压比高但局部 Mach 过高，应加入 `MaxMach` 约束，而不是把总压比直接作为目标。

5. 主动学习完成后建议检查：

```text
active_learning_diagnostics.csv
pareto_front.csv
pareto_front_strict.csv
training_data.csv
```

重点看：

- Pareto 是否真正改善。
- EHVI 是否下降。
- 预测误差是否收敛。
- 选点是否都来自 `nsga`，是否缺少探索性。
- 是否出现异常高流量但效率或收敛质量变差的点。

