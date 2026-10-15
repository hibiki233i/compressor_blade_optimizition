# 离线基准算例

此算例验证固定种子下的优化程序：采样 → 几何约束 → 代理拟合 → NSGA-II / EHVI 选点 → 记录与诊断 → Pareto 导出。几何和 CFD 阶段使用 `tests/synthetic_pipeline.py` 的替身；平均线来自已有的 `tests/fixtures/synthetic_meanline.cft-batch`。不调用 PowerShell、CFturbo 或 ANSYS，不读取本机 INI。

## 运行

在仓库根目录执行。Python 3.11+，参考环境为 Python 3.14.3；只需要 NumPy、Pandas、SciPy：

```powershell
python -m pip install -r benchmarks/offline_baseline/requirements.txt
python benchmarks/run_offline_baseline.py
```

每次新建 `benchmarks/offline_baseline/runs/run-*`，重复执行不覆盖已有结果。末尾出现 `Offline baseline matches reference` 且退出码为 0，表示本次运行通过比对。也可指定一个新目录：

```powershell
python benchmarks/run_offline_baseline.py --output-dir benchmarks/offline_baseline/runs/my-run
python benchmarks/compare_offline_baseline.py benchmarks/offline_baseline/runs/my-run
```

已有非空目录会被拒绝；真实工程 CLI 的签名、锁和 `--resume` 行为保持原样。该独立基准每次从头运行，不提供续跑选项。

## 配置与目标函数

`config.json` 保留共享配置的 12 个变量、7 个活动变量及 5 个固定值、变量边界、几何约束、工况声明和 Pareto 容差。活动变量的选取依据见 [关键设计决策](../../docs/optimization.md#9-关键设计决策)，batch 字段映射见 [设计变量](../../docs/optimization.md#2-设计变量)。只把预算改为小规模基准：

| 设置 | 值 |
| --- | --- |
| seed | 42 |
| 初始点 / 主动学习轮数 / 每批点数 / 总预算 | 12 / 2 / 3 / 18 |
| 代理模型 / 后备模型 | `rbf_ridge_ensemble` / `none` |
| NSGA-II 种群 / 代数 / 随机池 | 12 / 4 / 32 |
| EHVI 初始 / 验证采样 | 16 / 32 |

SciPy 固定 LHS / Sobol 路径，RBF 固定代理路径；安装 scikit-learn 不影响此基准。版本见 `requirements.txt`。不使用工程模板，所有外部程序路径为空，输出路径仅由脚本设置。

合成函数从每个真实写出的 `candidate.json` 读取变量。令 `h0`、`h4` 为 hub 的 beta 0 / 4 偏移，`s0`、`s2`、`s3` 为 shroud 的 beta 0 / 2 / 3 偏移，`t` 为 hub theta 偏移（均为度）：

```text
Efficiency = 0.762 + 0.001*h0 - 0.0003*h0² - 0.0004*(h4-2)² - 0.0002*s2² + 0.0005*t
MassFlow   = 4.25 - 0.015*h0 + 0.012*s0 - 0.003*s3² + 0.006*h4
PressureRatio = 2.0, Power = 1.0, totalpressureratio = 2.1
```

这两个目标沿 `h0` 有不同的偏好，便于验证前沿和采集逻辑。这里的值均为合成评分，不赋予物理单位。工程数据的 `MassFlow` 相当于 g/s、`PressureRatio` 是静压比、`totalpressureratio` 是总压比，见 [数据单位](../../docs/optimization.md#数据单位)。

## 输出与参考

| 文件 | 参考比对内容 |
| --- | --- |
| `training_data.csv` | 18 行的编号、阶段、状态、来源、完整 12 维向量及 5 项评分 |
| `pareto_front.csv` | 3 行的编号、顺序、向量及评分 |
| `active_learning_diagnostics.csv` | 6 行的角色、选点来源、模型、训练样本数、预测、不确定度、EHVI 及超体积变化 |
| `benchmark.json` | 合成数据标记、种子及本次 Python / 依赖版本（不参与数值比较） |

优化程序还会输出严格前沿、候选文件、待评估队列和局部搜索状态。行内 `message` 均标记 `SYNTHETIC`，`status=success` 只表示替身评估成功。**整个目录仅供离线回归，不可合并到真实 CFD 训练集，也不用于工程性能结论。**

`reference.json` 冻结了上述三张表的关键列，记录环境及优化器来源提交。编号、顺序、状态、角色等精确比较；数值满足 `abs(actual-reference) <= 1e-8 + 1e-7*abs(reference)`。NaN、缺失列、行数变化和前沿成员变化都会导致非零退出。路径、时间戳和平台信息不参与比较。

参考工程容差前沿（完整精度和设计向量见 `reference.json`）：

| run_id | Efficiency | MassFlow |
| --- | --- | --- |
| case_000004 | 0.762846 | 4.250169 |
| case_000012 | 0.761250 | 4.281058 |
| case_000015 | 0.760937 | 4.298475 |

比对失败时先检查运行目录的 `benchmark.json` 和依赖版本，再检查脚本指出的文件、行与列。脚本不会自动更新参考值；有意改变算法或基准时，应单独审查参考差异及原因。
