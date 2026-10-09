# 展向攻角验证入口

入口为 `python blade_shape_incidence_validation.py`，在 `code/` 仓库中运行。所有目标路径均由参数或 JSON 提供，不绑定某个叶轮名称。用途是复现旧指标、提取有符号速度三角形、检验对比工况、运行已有参数化支持的进口角敏感性试验。它不向原优化训练目录追加数据，不修改原 Pareto 定义，不把攻角变成第三目标。

## 1. 目标路径与几何确认

PowerShell 示例；路径变量替换为本机真实路径，输出必须选独立的新目录：

```powershell
$TargetRes = 'D:\my_project\target\Impeller_001.res'
$Geometry = 'D:\my_project\target\target.cft'
$PostExe = 'D:\ANSYS Inc\v251\CFX\bin\cfx5post.exe'
$ValidationRoot = 'D:\my_project\incidence_validation'
$TargetSpec = Join-Path $ValidationRoot 'target.json'
python blade_shape_incidence_validation.py init --res "$TargetRes" --geometry-source "$Geometry" --output "$TargetSpec"
```

`init` 生成**待核对配置**，并尝试预填可识别的字段。若几何来源是本项目的 `candidate.json` 或可解析的 `.cft-batch`，会预填 hub/shroud 前缘角；若 `.res` 同目录有本项目的 `candidate.json`，也会尝试读取。几何来源为 `.cft` 且候选文件位于别处时，可额外传 `--candidate "<candidate.json>"`。若 `.res` 同目录的 `cfx_state.json` 记录了与该 `.res` 哈希一致的已完成求解，还会预填其声明的叶片数。JSON 中的 `prefill_sources` 和 `prefill_inputs` 分别记录字段来源和文件身份；来源文件随后改变，提取会拒绝继续。预填值仍需核对，它们不会自动使 `geometry_verified=true`。

没有可解析文件时，`init` 仍可只登记路径并生成模板；可选的 `--candidate` 若为空、不是 JSON 或不符合本项目格式，程序会跳过角度预填、保留警告和待填字段，不会伪造角度。再次使用同一输出路径时，仅对 `.res` 和几何来源相同的验证配置补齐仍为空的可预填字段，保留人工填写的值，并先生成带时间戳的备份；不同算例或无效 JSON 不会被覆盖。CLI 会列出预填字段、警告和仍需确认的字段。`init` 不启动 CFX-Post，也不从二进制 `.res` 自动读取转速、入口条件或介质；当前没有通用 `.cft` 角度解析器。`extract` 必须在能访问真实文件和 CFX-Post 的机器运行。相对 `res_path` / `geometry_source` 按 spec 所在目录解释，其他 CLI 路径按当前工作目录解释。

编辑生成的 JSON，确认并填写：

- `hub_beta_deg`、`shroud_beta_deg`：目标实际前缘金属角，单位度，相对于指定周向参考方向。即使由候选或 batch 预填，也须核对它与目标 `.res` 的几何身份及角度约定。
- `geometry_verified`：核对几何来源、`.res` 对应关系、`BetaModeLE=Linear`，以及 CFturbo 与 Turbo 展向坐标一致后才设 `true`。此字段记录人的确认，不代表程序已解析并独立证明几何身份。
- `measurement.normal_sign`：+1 或 -1，使 `normal_sign * (W dot Normal)` 的正方向为下游；先在 CFD-Post 目视和数值确认。
- `measurement.theta_reference_sign`：+1 表示从正周向量角，-1 表示从负周向量角。若通常的转子相对周向速度为负且 CFturbo 从负周向朝正流向量角，使用 -1；不得未经核对照抄。
- `conditions`：实际 `rpm`、入口总压 Pa、入口总温 K、`fluid_id`、叶片数 `n_blades`。这些是用户声明的工况，代码不自动从 `.res` 证明它们。
- `conditions.simulated_passages`：模拟的实际通道数，默认 1；整轮模型填 `n_blades`。整体流量按 `n_blades/simulated_passages` 换算。
- `measurement.bands`（默认 20）、`le_station`（默认 0.22）、`turbo_domain`（默认 R1）、`max_reverse_fraction`（默认 0.01）。

前缘金属角采用线性插值。非线性展向角模板不应设置 `geometry_verified=true` 来绕过限制；本版本尚未支持其金属角读取。`le_station=0.22` 是 Blade Aligned 坐标，不是前缘上游某个弦长百分比。比较前必须统一截面定义。

## 2. CFX-Post 展向提取

```powershell
python blade_shape_incidence_validation.py extract --spec "$TargetSpec" --post-exe "$PostExe" --output-dir "$ValidationRoot\target_20bands_022"
```

在旋转域使用相对 `Velocity u/v/w` 与面法向求正向和逆向质量通量；按等宽 `Span Normalized` 带积分。各带先以**正向质量通量**平均有符号的 `Velocity Streamwise` 与 `Velocity Circumferential`，再计算：

`beta_f = degrees(atan2(W_streamwise_bar, theta_reference_sign * W_theta_bar))`

`i_geo = wrap180(beta_b - beta_f)`，正负号与报告的 `i=beta_b-beta_f` 一致。流角使用 Turbo 流向/周向平面，不能将 `Velocity Streamwise` 无条件视为子午速度模；若原研究用另一套局部基底，需先统一定义。

金属角在带内质量加权叶高 `s_mass` 上线性插值，避免拿端壁金属角直接对比宽展向带。正向权重保证 RMS 非负；逆向通量单独记录。某带无正向通量、平均流向速度非正、逆向通量占该带总绝对通量的比例超过阈值，或带积分净流量与 CFX `massFlow()` 相差超过 1%，均使 `quality_ok=false`，整体攻角指标留空，CLI 返回 2。

生成：

- `profile.csv`：带界限、正/逆向流量 kg/s、质量加权叶高、平均相对速度 m/s、金属角、流角、几何攻角（度）。
- `summary.json`：质量加权 RMS、质量加权平均绝对攻角、最大绝对攻角、整轮净流量和质量标记。
- `inputs.json`：配置与 `.res`、几何来源的 SHA-256。提取后再次核对身份，文件改变则失败。
- `extract.cse`、`command.json`、`cfxpost.log`、`returncode.json`、`flow_diagnostics.tsv`、`state.json`：完整执行证据；错误日志若生成也保留。

这是流动诊断，不是最小损失攻角标定。`numerical_acceptance_verified=false`、`min_loss_angle_calibrated=false` 始终明确保留。`quality_ok` 不验证残差、网格独立性、物性适用性或稳定裕度。禁止把经验 Stanitz 修正直接作为已验证的真实最优角。

CFX session 复用现有 Turbo 初始化与截面生成器。分带 `if()` 的零值使用积分前通量密度单位：质量通量为 `kg m^-2 s^-1`，速度加权通量为 `kg m^-1 s^-2`；`areaInt()` 后才按 `kg s^-1` 和 `kg m s^-2` 归一化。若误用积分后单位，CFX-Post 会报 `The 'true' and 'false' expressions have inconsistent dimensions`。提取异常会显示返回码、日志路径及首条错误，失败目录保留，修复后应选择新目录重新提取。

2026-10-08 已对本机 `case_000000/Impeller_001.res` 使用 CFX-Post 25.1 完成一次真实的 20 带、0.22 截面提取，返回码为 0，分带质量闭合与诊断质量检查通过。这证明该算例的表达式可执行；仍需对照 CFD-Post 手工积分、速度三角形与几何角度约定，才能形成工程结论。离线测试检查公式、命令和失败路径。

## 3. 复现报告的 20 点旧指标

把原始导出的目标曲线整理成严格的两列 CSV：`span,beta_cfx_deg`。要求 20 行、按 `j/19` 从 0 到 1 排序；叶高精度至少 6 位小数。这里读取的是原始 `Velocity Beta ACA` 数据，不是当前模块产生的质量加权流角，也不是旧报告中已计算出的攻角。

### 从 `.res` 自动导出 ACA CSV

`aca` 子命令让 CFX-Post 在已有 `.res` 上重放**保存的** Turbo 展向测量线 session（原流程的 `extract_aca.cse`：`R1`、Blade Aligned `0.251`、Hub→Shroud 20 点含边界、Equal Distance、Area 周向平均、`Beta = 90 [degree] - Velocity Flow Angle`），不生成几何、网格，也不求解：

```powershell
python blade_shape_incidence_validation.py aca --res 'D:\Kn\sliptip_las_temre_007.res' `
  --post-exe 'D:\ANSYS Inc\v251\CFD-Post\bin\cfx5post.exe' `
  --session 'D:\compressor_blade_optimizition\ansys_work\legacy_aca_20261001_181929\extract_aca.cse' `
  --output-dir "$ValidationRoot\aca_007"
```

session 是测量定义的唯一来源：程序复制它到新输出目录，从中解析并记录 `Streamwise Location`、`Span Points`、`Turbo Domain List`、平均方式等设置，而不是写死这些元数据；`Export File` 必须是相对路径（落在输出目录内）。改测量定义时只需换 session。session 未随仓库分发，换机器时需一并拷贝。

成功前要求：返回码 0、导出文件存在、`cfdpost_error.log` 不存在或为空、`.res` 与 session 前后 SHA-256 一致、表头为 `Span Normalized` 与 `Velocity Beta ACA on <线名> [ degree ]`、恰好 20 个有限点且 `|span_j - j/19| ≤ 1e-6`。数值按原文本写入 `<res主体>_beta_aca_20.csv`，不换算、不插值。`legacy_aca_quadrant_ok=false` 时仍写 CSV（`legacy` 会拒绝该象限）。输出目录另含 `extract_aca.cse`、`extraction_inputs.json`、`cfxpost.log`、`returncode.json`、原始导出与 `extraction_summary.json`（`status` 为 `running` / `failed` / `complete`）；失败时保留全部日志。`status=complete` 只表示导出与格式检查完成，不代表攻角复现适用或 CFD 质量已接受。测量线名称含 `LE` 不能证明其恰在前缘，换几何时应核对实际位置。

GUI「验证 → 导出 ACA 20 点 CSV」执行同一命令，成功后自动把 CSV 填入「复现报告20点指标」。

```powershell
$LegacyCsv = 'D:\my_project\exports\target_beta_aca.csv'
$HubBeta = 70.356
$ShroudBeta = 20.209
python blade_shape_incidence_validation.py legacy --csv "$LegacyCsv" --hub-beta-deg $HubBeta --shroud-beta-deg $ShroudBeta --output-dir "$ValidationRoot\legacy_target"
```

角度示例来自所讨论报告，只能在确认对应几何时使用。该模式严格限定 ACA 在已确认的 [-90°, 0°] 象限，复现 `beta_f=90-abs(beta_cfx)`、线性金属角和 20 点算术平均绝对攻角；不把此公式推广到回流或其他象限。输出方法标识与新指标不同，两者不能混合比较。源 CSV 的路径与哈希保存在 summary；复现 2.768° / 4.861° 仍需原始20点数据，程序不内置或伪造这些结果。

## 4. 工况匹配对比与测量敏感性

分别为基准和目标准备 spec 并提取后：

```powershell
python blade_shape_incidence_validation.py compare --baseline "$ValidationRoot\baseline\summary.json" --target "$ValidationRoot\target\summary.json" --flow-tolerance 0.01 --output "$ValidationRoot\comparison.json"
```

只有方法和测量定义一致才比较。比较会检查声明的转速、入口总温总压、介质、叶片数，并按提取后的整轮净流量检查相对差。质量检查失败或流量/工况不匹配时返回 2，`usable_for_matched_point_diagnostic=false`。即使声明工况匹配，也不等于已证明网格和求解数值接受，不能用该标记进行损失因果归因。

截面/分带敏感性入口可以对同一 `.res` 自动提取多组设置：

```powershell
python blade_shape_incidence_validation.py sweep --spec "$TargetSpec" --post-exe "$PostExe" --stations 0.20 0.22 0.24 --bands 20 40 --output-dir "$ValidationRoot\measurement_sweep"
```

该例运行6次只读后处理，生成逐次日志、进度和 `sensitivity.csv`，每次检查来源身份；后处理失败即停，已有日志保留，不自动重试。检查曲线和统计量的稳定性后再选固定定义。这些测量定义不同的结果不能通过 `compare` 冒充同定义设计对照。此处尚不提供自动收敛阈值或自动选“最好截面”。

## 5. 现有参数化下的真实 CFD 敏感性

复制当前 `blade_shape_config.json` 到目标工程配置，并把 `paths` 指向实际目标的 `.cft/.cft-batch`、TurboGrid/CFX 模板和软件路径。路径应优先使用绝对路径，计划生成时会将路径按现有 CLI 的当前工作目录规则固化。目标几何必须满足当前两条主叶片平均线和12变量接口；不支持凭空生成中间叶高控制点。

```powershell
$TargetConfig = 'D:\my_project\target_validation_config.json'
$Study = Join-Path $ValidationRoot 'endpoint_study_01'
$AngleStep = 0.25
python blade_shape_incidence_validation.py plan --config "$TargetConfig" --step-deg $AngleStep --output-dir "$Study"
```

不带 `--candidate` 时，中心是目标 batch 模板本身（12个偏移全为零）。带 `--candidate <candidate.json>` 时，中心使用已有候选，代码会重新从基准模板与参数向量生成几何并核对，不接受不匹配的候选。

每个背压点生成5个设计：中心、hub进口角±step、shroud进口角±step。其余坐标固定在目标值，只有**独立试验配置**的搜索切片会相应调整；原配置文件和原运行目录不变。所有扰动仍须通过原变量边界及几何约束，越界会拒绝整个计划，不自动扩大边界。需要背压扫描时显式添加 `--pressures-pa`，数值单位及压力基准沿用原 CFX runner 的 `MyBackPressure`，必须根据实际模板定义选择，不能把其他工程的12 Pa直接照搬。

```powershell
# 只有在工程路径、模板工况和预算确认后运行；以下最多新增1次真实CFD
python blade_shape_incidence_validation.py run --plan "$Study\plan.json" --max-new-cfd 1
# 继续后续尚未尝试的点；不自动重算失败或中断点
python blade_shape_incidence_validation.py run --plan "$Study\plan.json" --max-new-cfd 1 --resume
```

调用原 `evaluate_true_cfd`，沿用几何、网格、CFX和各阶段失败记录。结果写入 `$Study\cfd` 的独立训练CSV及case目录，并在 `progress.json` 中记录计划内工况/角色对应的结果；这些CSV沿用主程序历史单位，不能当作SI流量。原始结果的每个 case 都需再用本验证模块提取SI流量和攻角。

计划冻结配置、候选和物理输入的身份，并保存 SHA-256 签名。源文件改变后拒绝执行；每次外部计算前先写入 `running`，失败即停，中断后保留运行状态，`--resume` 不会自动重试该点。计划全部完成只说明流程完成，不能替代CFD数值接受。恢复失败/中断点需要人工核查；本入口刻意不提供跳过失败或强制覆写开关。

背压扫描只提供不同工作点的样本，不自动保证等流量。必须依据各点实际提取流量用 `compare` 检查，必要时另建预算明确的新背压计划；不能把插值或代理预测冒充已计算的等流量点。该入口尚未实现自动等流量寻根、非线性展向前缘几何、稳定裕度判定或最小损失角标定。

## 验证与来源

```powershell
python -m py_compile blade_shape_incidence_validation.py
python -m unittest tests.test_incidence_validation -v
python -m unittest discover -s tests
```

测试使用合成数据和外部调用替身，覆盖角度象限、回流、质量闭合、旧指标、身份、对比工况、预算、续跑及失败/中断不重算。真实Windows验证时先做一份已有 `.res` 的只读提取，对照CFD-Post手工积分和速度三角形，再决定是否运行预算化敏感性CFD。

正/逆向通量表达式依据 [ANSYS Knowledge](https://innovationspace.ansys.com/knowledge/forums/topic/how-to-calculate-the-positive-or-negative-mass-flow-rate-through-an-arbitrary-plane-in-cfd-post/)；Turbo变量及坐标定义见 [CFD-Post 2025 R1](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfd_post/i1306553.html) 与 [Turbo Charts](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfd_post/i1362519.html)。

## GUI 与 CFX 残差门槛

桌面GUI的“验证”页（`Ctrl+6`）可通过表单执行上述七个入口，仍调用本CLI子进程。“准备验证配置”可选填 `candidate.json` 预填角度，也可从优化算例填入；执行后须打开生成的 JSON 补齐日志列出的字段。右侧“攻角诊断”只读显示本CLI写出的 `profile.csv` / `summary.json` / `sensitivity.csv` / 对比 JSON / `progress.json`：β_b 与 β_f 的展向分布、i(span)、逆流占比、测量定义敏感性、同定义结果的叠加与 Δi，以及敏感性CFD点之间的有限差分斜率；它不重算攻角，也不把这些显示量当作最小损失角标定。详见 [GUI 说明](README_blade_gui.md#6-验证展向几何攻角诊断)。项目设置页可调整共用的 `cfx_convergence` 设置。

敏感性CFD复用原runner，因此同样要求末次 `.out` 中各方程RMS达到1e-5（或配置的更严格值）。初次正常耗尽步数而未达标时，从末次 `.res` 流场出发追加最多1500–2000步（范围见 `blade_shape_convergence.RESTART_ITERATIONS_RANGE`），默认2000；计数重新开始，仍不达标则作为solve失败保留证据并停止当前验证方案。该受控的一次追加属于同一设计点，不是失败点的无限自动重试。`--max-new-cfd` 是设计点预算，最多可能对应两倍的求解器调用。详细行为见 [CFD残差接受规则](README_blade_shape_active_learning.md#mandatory-final-rms-acceptance-and-one-bounded-continuation)。
