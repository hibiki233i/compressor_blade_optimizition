# 验证与诊断

这里有两个独立的分析工具，都只读取已有的 CFD 结果：

| 工具 | 用途 | 入口 |
| --- | --- | --- |
| **展向攻角验证** | 按叶高分带计算前缘的来流角、叶片角和攻角，比较不同叶轮，并可以跑进口角敏感性 CFD | `blade_shape_incidence_validation.py` |
| **熵增与流向角诊断** | 快速看进出口熵增，以及 hub/shroud 附近的流向角与叶片角差多少 | `blade_shape_flow_diagnostics.py` |

两者都不会往优化的 `training_data.csv` 里写数据，也不会改变 Pareto 前沿或优化目标。它们输出的流量都是 SI 单位（kg/s），与训练 CSV 的单位不同，见 [数据单位](optimization.md#数据单位)。

---

## 一、展向攻角验证

### 适合回答什么问题

- 目标叶轮在前缘各叶高处的攻角分布是怎样的？
- 两个叶轮的攻角差异，是几何不同造成的，还是工况不同造成的？
- 能否复现报告里用 20 点方法算出的攻角？
- 把 hub 或 shroud 进口角改一点点，效率和流量怎么变？

### 推荐流程

```text
准备配置 (init) → 人工核对配置
        ↓
提取攻角 (extract)
        ├── 截面/分带敏感性 (sweep)：先确认测量定义稳定
        └── 工况对比 (compare)：基准与目标是否同工况、攻角差多少
        ↓
生成敏感性方案 (plan) → 运行敏感性 CFD (run)
        ↓
对每个结果 .res 再提取攻角 → 工况对比（扰动点和中心点是否同流量）
```

另有一条独立支线，用来复现报告里的 20 点旧指标：导出 ACA 曲线 (aca) → 复现 (legacy)。

以下命令示例都用 PowerShell 变量代表路径，请换成本机的实际路径。每次分析都必须输出到**新的**文件或目录，程序拒绝覆盖已有结果。

```powershell
$PostExe = 'D:\ANSYS Inc\v251\CFX\bin\cfx5post.exe'
$Root = 'D:\my_project\incidence_validation'
```

### 1. 准备配置（init）

```powershell
python blade_shape_incidence_validation.py init --res 'D:\my_project\target\Impeller_001.res' --geometry-source 'D:\my_project\target\target.cft' --output "$Root\target.json"
```

这一步生成一个**待核对**的 JSON 配置。程序会尽量预填能从文件里找到依据的字段：

- 几何来源是本项目的 `candidate.json` 或 `.cft-batch` 时，预填 hub/shroud 前缘角。几何来源是 `.cft`、而候选文件在别处时，可以另加 `--candidate 候选.json`。
- `.res` 同目录下有已完成求解记录时，预填叶片数。

**打开 JSON 后，需要人工核对或填写：**

| 字段 | 说明 |
| --- | --- |
| `hub_beta_deg`、`shroud_beta_deg` | 目标叶轮实际的前缘金属角（度）。预填的值也要核对 |
| `geometry_verified` | 确认几何来源与 `.res` 对应、前缘角为线性分布（`BetaModeLE=Linear`）、CFturbo 与 CFX 的叶高坐标一致后，再改为 `true` |
| `measurement.normal_sign` | 取 +1 或 −1，使截面法向指向下游。请先在 CFD-Post 里确认 |
| `measurement.theta_reference_sign` | 角度从哪个周向方向量起：+1 为正周向，−1 为负周向。不要照抄别的工程的值 |
| `conditions` | 转速、入口总压（Pa）、入口总温（K）、介质名称、叶片数。程序不会从 `.res` 读这些，填什么就按什么比较 |
| `conditions.simulated_passages` | 模拟了几个通道，默认 1；整圈模型填叶片数 |
| `measurement` 其他项 | 分带数（默认 20）、截面位置（默认 0.22）、Turbo 域名（默认 R1）、逆流比例上限（默认 1%） |

截面位置 0.22 是 CFX "Blade Aligned" 坐标（前缘在 0.25），不是弦长百分比。目前只支持前缘角沿叶高线性分布的叶片。

对同一个算例重复运行 `init` 时，程序会先备份原文件，再只补齐空字段，不会覆盖你手工填的值。

### 2. 提取攻角（extract）

```powershell
python blade_shape_incidence_validation.py extract --spec "$Root\target.json" --post-exe "$PostExe" --output-dir "$Root\target_extract"
```

程序调用 CFX-Post，在前缘上游的截面上，把叶高等分成若干带，每带计算：

- **来流角 β_f**：由相对速度的流向分量和周向分量求出，只统计正向流动的质量通量；
- **叶片角 β_b**：hub 与 shroud 前缘角按叶高线性插值得到；
- **攻角 i = β_b − β_f**。

最后汇总出质量加权的 RMS 攻角、平均绝对攻角、最大绝对攻角和整轮净流量。

**以下情况视为质量不合格**：某带没有正向流动、平均流向速度不为正、逆流比例超过上限、各带流量之和与 CFX 算出的总流量相差超过 1%。这时 `quality_ok=false`，不给出整体攻角，命令返回码为 2。

主要输出：

| 文件 | 内容 |
| --- | --- |
| `profile.csv` | 每个叶高带的流量、速度、叶片角、来流角、攻角 |
| `summary.json` | 汇总指标和质量结论 |
| `inputs.json` | 所用文件的指纹。提取过程中文件被改动，会直接报失败 |
| 其余文件 | CFX-Post 的 session、日志、返回码等，供排查问题 |

本项目 case_000000 已在真实 CFX-Post 25.1 上跑通过一次（20 带、0.22 截面，质量检查通过）。这只证明表达式能正确执行，形成工程结论前还应在 CFD-Post 中手工积分、对照速度三角形核对一遍。

### 3. 截面 / 分带敏感性（sweep）

截面位置和分带数不同，算出的攻角也会不同。正式比较之前，应该先确认结果对测量定义不敏感：

```powershell
python blade_shape_incidence_validation.py sweep --spec "$Root\target.json" --post-exe "$PostExe" --stations 0.20 0.22 0.24 --bands 20 40 --output-dir "$Root\sweep"
```

这个例子会依次提取 3 × 2 = 6 种设置，结果汇总在 `sensitivity.csv`。任何一次失败就停止。看过曲线、选定一种固定的测量定义后，后续所有比较都用这种定义。程序不会替你自动挑选"最好"的截面。

### 4. 复现报告的 20 点旧指标（aca + legacy）

报告中的旧方法是：取 20 个叶高点上的 Velocity Beta ACA 角，换算成来流角，与线性插值的叶片角相减，再对攻角绝对值做算术平均。这和第 2 步的质量加权方法**不是同一个指标，不能混在一起比较**。

**先从 `.res` 导出 20 点曲线**。这一步需要一个保存好的 CFX-Post session 文件（原流程的 `extract_aca.cse`，不在仓库里，换机器时要一起拷贝）。测量定义完全以这个 session 为准：

```powershell
python blade_shape_incidence_validation.py aca --res 'D:\Kn\sliptip_las_temre_007.res' --post-exe "$PostExe" --session 'D:\path\to\extract_aca.cse' --output-dir "$Root\aca_007"
```

程序会检查：导出的正好是 20 个点、叶高按 j/19 从 0 到 1 均匀分布、列名和单位正确。检查通过后，整理出一个两列的 CSV（`span,beta_cfx_deg`）。

**再计算旧指标**：

```powershell
python blade_shape_incidence_validation.py legacy --csv "$Root\aca_007\sliptip_las_temre_007_beta_aca_20.csv" --hub-beta-deg 70.356 --shroud-beta-deg 20.209 --output-dir "$Root\legacy_007"
```

上面的角度只是示例，请填入与该 `.res` 对应的实际几何角度。旧方法只适用于 ACA 角在 −90° 到 0° 之间的情况，超出这个范围时 `legacy` 会拒绝计算。

在界面上，`aca` 导出成功后会自动把 CSV 填进 `legacy` 的表单。

### 5. 工况对比（compare）

```powershell
python blade_shape_incidence_validation.py compare --baseline "$Root\baseline\summary.json" --target "$Root\target\summary.json" --flow-tolerance 0.01 --output "$Root\comparison.json"
```

比较两次提取结果之前，先检查它们**是否可比**：

1. 两次必须用同一种方法、同一套测量定义，否则直接报错。
2. 声明的转速、入口总压、入口总温、介质、叶片数必须完全一致。
3. 两边的实际净流量相差不超过 `--flow-tolerance`（例子中是 1%）。
4. 两边的提取质量都合格。

全部满足时，`usable_for_matched_point_diagnostic=true`，并给出攻角 RMS 的差值 `delta_rms_deg`；否则返回码为 2。即使判定为同工况，也只说明工况一致，不能据此断定"损失是攻角造成的"。

### 6. 进口角敏感性 CFD（plan + run）

**目的**：在目标设计上，把 hub 或 shroud 的进口角各改 ±一小步，用真实 CFD 观察性能变化。

**生成方案**：先复制一份 `blade_shape_config.json` 作为这次实验专用的配置，把其中的路径指向目标工程的模板和软件，然后：

```powershell
python blade_shape_incidence_validation.py plan --config 'D:\my_project\target_validation_config.json' --step-deg 0.25 --output-dir "$Root\endpoint_study_01"
```

- 每个背压生成 5 个点：中心、hub 进口角 −step、hub +step、shroud −step、shroud +step。
- 不加 `--candidate` 时，中心就是目标模板本身（12 个偏移量都为 0）。加了 `--candidate` 时，以该候选为中心，程序会核对候选与模板生成的几何是否一致。
- 需要扫描多个背压时加 `--pressures-pa`。数值含义与 CFX 模板里的 `MyBackPressure` 一致，请按实际模板选择，不要照搬别的工程的值。
- 任何一个点违反变量范围或几何约束，整个方案都会被拒绝，程序不会自动放宽范围。
- 输出目录必须和优化的输出目录分开。

**执行**：

```powershell
# 先只跑 1 个点（中心点），确认没问题
python blade_shape_incidence_validation.py run --plan "$Root\endpoint_study_01\plan.json" --max-new-cfd 1
# 再续跑剩下的点
python blade_shape_incidence_validation.py run --plan "$Root\endpoint_study_01\plan.json" --max-new-cfd 4 --resume
```

`--max-new-cfd`（界面上的"本次最多新增 CFD 点数"）是**这一次运行**最多新开始几个点，而不是方案的总点数：

- 已完成的点会跳过，不占名额；
- 名额用完就停，状态为 `budget_exhausted`，下次加 `--resume` 接着跑；
- 一旦有点失败或曾被中断，程序就停下并要求人工检查，**不会自动重算**；
- 每个点要完整跑一遍"几何 → 网格 → CFX"。如果残差不达标，还可能追加一次续算（见 [CFD 成功判定](optimization.md#6-cfd-结果什么时候算成功)），但仍只算一个名额；
- 填 0 时不跑 CFD，只检查方案和输入文件。

方案生成时会记下所有输入文件的指纹和签名。之后源文件有任何改动，`run` 都会拒绝执行。

**跑完之后**：结果在方案目录的 `cfd/` 下，CSV 沿用优化程序的单位。要比较攻角，需要对每个算例的 `.res` 再执行第 2 步提取，然后用第 5 步检查扰动点和中心点是否仍在同一流量。改了进口角以后，同一背压下流量会变，程序**不会**自动保证等流量。流量对不上时，需要另建一个背压方案，不能用插值或模型预测代替。

目前尚未实现：自动寻找等流量背压、非线性的展向前缘角、稳定裕度判断、最小损失攻角标定。

### 在界面中使用

界面「验证」页（`Ctrl+6`）的八个动作与上面各步一一对应。右侧「攻角诊断」会画出：叶片角与来流角的展向分布、各叶高的攻角、逆流比例、不同测量定义的对比、两次结果叠加后的差值，以及敏感性 CFD 各点之间的变化斜率。

这些图只是展示命令行的结果。叠加图只用来观察曲线形状的差异，两次结果是否同工况仍以 `compare` 的结论为准。斜率只描述已经算过的几个点，不是最小损失攻角的标定。

---

## 二、熵增与流向角诊断

### 做什么

`blade_shape_flow_diagnostics.py` 对已有的 `.res` 调用 CFX-Post，读取：

- 进口、出口，以及前缘附近（0.22）、后缘附近（0.78）截面的质量加权静熵，单位 J/(kg·K)；
- 这些截面上的质量流量，用来检查流量是否守恒；
- 靠 hub（0–20% 叶高）和靠 shroud（80–100% 叶高）两个宽带的平均相对流向角；
- 如果算例目录里有 `candidate.json`，还会用前后缘叶片角算出入射角、偏差角的**近似值**（列名带 `proxy`）。

```powershell
python blade_shape_flow_diagnostics.py --res 'D:\blade optizamation\blade_al_runs\cases\case_000000\Impeller_001.res' --output 'ansys_work\flow_diagnostics.csv'
```

可以重复写多个 `--res`。默认从配置的 `paths.cfx_bin_dir` 中查找 `cfx5post.exe`，也可以用 `--post-exe`、`--inlet`、`--outlet`、`--turbo-domain`、`--le-station`、`--te-station`、`--span-band` 改设置。输出 CSV 必须是新文件。每次后处理的日志保存在 `<输出CSV>.logs/` 下。

### 和展向攻角验证的区别

这个工具用的是宽叶高带的平均流向角，再和端壁处的叶片角相比，两者并不在同一叶高上。角度换算 `β_flow = 180° − Velocity Flow Angle` 也是在本项目算例上实测得到的约定。所以它的结果**只适合在固定设置下看趋势**，不能当作真正的局部攻角，也不能用来判定"匹配合格"。需要严谨的展向攻角时，请用第一部分的工具。

另外，这里的流量是单通道值，没有乘叶片数。程序也没有核对 `candidate.json` 与 `.res` 是否真的对应，输出中的 `candidate_res_identity_verified` 恒为 False。

### 已有的观察（探索性）

以下结果来自早期对 13 个成功算例的检查。样本是挑选出来的，不是随机抽取，原始数据也没有入库，仅供参考：

- 进出口质量流量相差约 0.02%–0.10%；进口到出口的熵升约 934–987 J/(kg·K)，前缘到后缘约 836–884 J/(kg·K)。
- 效率与进出口熵升的相关系数约 −0.44，与叶道内熵升约 −0.64，与四处角差绝对值的平均约 −0.78。这只是小样本相关，不能说明角差导致效率变化。
- 四处角差绝对值的中位数：前缘 hub 4.9°、前缘 shroud 1.7°、后缘 hub 5.0°、后缘 shroud 10.5°。后缘 shroud 偏大，值得进一步核查；但当前搜索把 `shroud_beta_4` 固定了，暂时没法直接调整这个角。
- **测量位置影响很大**：case_000000 的后缘 shroud 角差，在出口边界上约为 −19.6°，在 0.78 截面上约为 −8.6°。

### 能不能把角度匹配加进优化

现阶段不建议把"角度匹配"作为第三个优化目标。更稳妥的顺序是：

1. 用更多同工况、收敛经过审查的算例，确认角差、熵升和效率之间的关系；
2. 在 CFD-Post 中目视确认截面位置和角度约定；
3. 确定可接受的角差范围。如果关系稳定，优先作为**约束或筛选条件**，保留效率 + 流量两个目标；
4. 需要调整后缘 shroud 角时，再考虑解除 `shroud_beta_4` 的固定。

---

## 注意事项

- 所有"质量合格""同工况""计划完成"的标记，都只说明流程按规则走完了，不能代替人工检查残差、守恒、网格和工况。
- 不同测量定义（截面、分带数、新旧方法）得到的结果不能相互比较。
- 验证配置里的工况和几何确认都是**人工声明**，程序按你填的值比较，无法替你证明它们是对的。

## 参考资料（ANSYS 2025 R1 官方文档）

- [正/负向质量流量的计算方法](https://innovationspace.ansys.com/knowledge/forums/topic/how-to-calculate-the-positive-or-negative-mass-flow-rate-through-an-arbitrary-plane-in-cfd-post/)
- [Turbo 变量与流向角定义](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfd_post/i1306553.html)
- [Blade Aligned 坐标与 Turbo Charts](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfd_post/i1362519.html)
- [CFX-Post 批处理 session](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfd_post/i1309479.html)
- [CFX 求解器输出表格](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfx_solv/i1299644.html)
- [从初始文件续算与继承历史的区别](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfx_mod/mod_ic_continuinghistory.html)
