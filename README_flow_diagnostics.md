# 进出口熵增与叶片角匹配：现阶段评估

## 结论

适合把**熵增和近叶片流向角**加入后处理诊断；现在不适合把“角度完全匹配”直接加为第三个优化目标。现有双目标仍为 `Efficiency` 与 `MassFlow`。在定义角差阈值、统一单位并验证角度与效率/损失的关系之前，不改历史 `training_data.csv`、Pareto 规则或恢复凭据。

熵是损失量，不是角度。角匹配需要同一展向位置的**转子相对流向角**和**叶片金属角**；前缘入射角与后缘偏差角应分别在前缘上游、后缘下游的固定截面评估。CFD-Post 的 `Velocity Flow Angle` 是相对速度在叶间平面的角度；本项目的 CFturbo beta 与它在已检查算例中近似满足 `beta_flow = 180° - Velocity Flow Angle`。这个换算是当前模型的实测约定，不能直接套用到其他转向或几何。CLI 输出的列名故意带有 `proxy`。

这里的流向角具体以周向为参考，使用转子域的相对速度；不能换成静止坐标系流向角。现有实现将 **0–20% / 80–100% 展向带的平均流向角**与 **hub / shroud 端点金属角**比较，二者并非严格相同展向位置，也不是先平均速度矢量再求角。因此结果只能用于固定定义下的趋势诊断，不能解释为局部壁面入射角或判定“匹配合格”。两个角差统一定义为 `wrap180(180 - flow_angle - blade_beta)`，取值范围为 [-180°, 180°)；其正负号是本工具约定，使用其他文献的入射/偏差定义时需先核对符号。圆周平均跨越 ±180° 本身不会使角度失效；集中度低才会抑制该角度输出。

## 已实现的只读提取

需要完整展向验证时使用新增 [展向攻角验证入口](README_incidence_validation.md)：它采用有符号速度三角形及 `i=beta_b-beta_f`，与本页原有宽展向带 `proxy` 是不同指标，不能直接混合比较。

`blade_shape_flow_diagnostics.py` 从指定 `.res` 调用现有 CFX-Post 25.1，在独立诊断工作目录生成 `.cse`，不修改原始 `.res`、算例目录、训练 CSV 或主求解链。它读取：

- `R1 Inlet` / `R1 Outlet` 的质量流量加权静熵，分别用带符号和绝对质量通量权重计算，明确换算为 J/(kg·K)；
- 入口、出口及靠近前缘/后缘的截面质量流量，输出相对闭合误差；
- Blade Aligned 位置 0.22（前缘 0.25 之前）和 0.78（后缘 0.75 之后）的静熵与相对流向角；
- 各截面靠 hub 的 0–20% 和靠 shroud 的 80–100% 展向带。角度使用加权正弦/余弦求圆周均值，并报告角度集中度和带内流量占比；
- 算例内 `candidate.json` 存在时的前缘/后缘 beta，以及约定换算后的入射角/偏差角**代理值**。

示例（从仓库根目录运行；可重复指定 `--res`）：

~~~powershell
python blade_shape_flow_diagnostics.py --res "D:\blade optizamation\blade_al_runs\cases\case_000000\Impeller_001.res" --output "ansys_work\flow_diagnostics.csv"
~~~

默认使用 `blade_shape_config.json` 的 `paths.cfx_bin_dir` 寻找 `cfx5post.exe`，边界名为 `R1 Inlet` / `R1 Outlet`，Turbo 域名为 `R1`。其他工程可用 `--post-exe`、`--inlet`、`--outlet`、`--turbo-domain` 指定。截面位置可用 `--le-station`、`--te-station` 调整，展向带宽用 `--span-band` 调整。CFX-Post 可能在返回码为零时仍无法计算单个表达式；工具会检查每个数值、完成标记和错误日志，缺失量不会被写成零。

输出的 `quality_ok` 只检查可提取性、入口/出口及近叶片截面的流量闭合、熵增符号、带内流量占比和流向角集中度。它**不**证明求解收敛、网格独立或工程物理可信。`F:\opt_new` 的 `.res` 可用于验证命令兼容性；那里的设计变量和工况并非本项目当前 12 变量搜索空间，不应与本项目算例混合训练或直接比较优劣。

显式指定 `--post-exe` 时不读取配置文件。输出 CSV 必须是新路径，已有文件会被拒绝覆盖。每次后处理的命令、session、stdout/stderr、返回码、原始 TSV 和 CFX 错误日志（若生成）保留在 `<输出CSV>.logs/blade_flow_*/`，失败时也保留；批量提取失败时不会写出不完整 CSV，已执行算例的日志仍可检查。请把输出路径放在独立分析目录中。直接调用 Python `analyze_res` 时可通过 `log_dir` 指定保留位置，省略时使用系统临时目录下的独立子目录，程序不主动清理。

`mass_flow_*_kg_s` 是指定截面的实际模拟流量，单通道模型中为**单通道**值，本工具不乘叶片数。输出新增 `inlet_location`、`outlet_location`、`turbo_domain`、`post_log_dir` 和 `candidate_path` 以追溯设置；`angle_proxy_convention` / `angle_proxy_comparison` 记录角差定义。四个角差列始终存在，无法计算时留空，`angle_proxies_available` 仅表示四个代理值均可计算。缺少 `candidate.json` 时仍可提取熵和流向角；文件存在但角度无效时会报错。

工具只读取 `.res` 同目录下的候选角度，没有校验该候选与 `.res` 的几何身份，故明确输出 `candidate_res_identity_verified=False`。`quality_ok=True` 不代表身份已确认，也不代表角度匹配或代理值必然可用；形成工程结论前需结合原求解链凭据核对候选与结果的对应关系。

## 对现有结果的观察

以下是随远端提交保留的历史探索性记录。仓库未附对应 13 份 `.res`、诊断明细与角度换算的标定证据，当前离线代码审查未复现这些数值。复核时应保存算例清单、提取设置、日志以及候选与结果的对应关系。

用当前工程的 13 份已成功 `.res` 做了探索性检查：选取效率较高/较低的算例及按编号分散的算例，样本并非随机抽取。13 份均成功提取，入口/出口质量流量相对差约为 0.02%–0.10%。入口到出口静熵升高约 934–987 J/(kg·K)；近前缘到近后缘的熵升约 836–884 J/(kg·K)。这说明叶道内部和后缘至出口的混合都可能贡献损失，但这些量还未经过收敛与测量面敏感性全面验证。

在这 13 份样本中，效率与进出口熵升的 Pearson 相关约为 -0.44，与近叶道熵升约为 -0.64；四处角差代理值的绝对值均值与效率约为 -0.78。这是小样本关联，不是角差造成效率变化的证据，也不能直接用来定惩罚权重。四处角差绝对值的中位数分别为：前缘 hub 4.9°、前缘 shroud 1.7°、后缘 hub 5.0°、后缘 shroud 10.5°。后缘 shroud 值值得进一步核查；当前 7 变量局部搜索把 `shroud_beta_4_deg_offset` 固定，暂时无法直接调整该金属角。

测量位置很关键。`case_000000` 在计算域出口边界得到的 shroud 出口角差代理值约为 -19.6°，在近后缘 0.78 截面约为 -8.6°。把近叶片截面改为前缘 0.20/0.24、后缘 0.80/0.76 时，四处角差也有变化；后缘 shroud 在这三组位置间约为 -7.0° 至 -8.6°。因此优化时必须固定截面定义，并先检查几何、网格和流量分布对该指标的影响。

## 单位问题与下一步

当前 `Extract_Results.cse` 写入 `CFX_Results.txt` 时直接打印 CFX 内部单位数值；此工程结果文件的基本单位是 mm、g、K。以 `case_000000` 为例，现有训练 CSV 的 `MassFlow=4.18154` 实际对应约 **0.00418154 kg/s**（10 个叶道），而 `Power=156032338344.22` 按内部单位换算约为 **156.03 W**。当前 `MassFlow` 数值相当于 g/s，`Power` 数值相当于内部功率单位。若将新算例直接改成 SI 后追加旧 CSV，就会混合两个单位体系并破坏代理模型、Pareto 容差和诊断；应先做带版本、可回滚的历史数据迁移，并同步缩放质量流量容差。提取工具本身始终显式输出 SI，不写训练 CSV。

建议的优化接入顺序：先用更多相同工况且经收敛审查的算例验证近前缘/后缘角差、熵升和效率的关系；在 CFD-Post 中目视确认 Blade Aligned 截面与 beta 角约定；再确定可接受的入射/偏差范围。若结果稳定，优先将过大的角差作为**约束或候选筛选诊断**，保留当前效率/流量双目标；需要调整后缘 shroud 时再评估是否解冻对应变量。新指标必须有明确单位、方向、失败时的处理及历史样本兼容策略。

相关 ANSYS 2025 R1 官方说明：[批处理 session](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfd_post/i1309479.html)、[Power Syntax evaluate](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfx_ref/i1321248.html)、[质量流量加权函数](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfx_ref/quant_func_list.html)、[Turbo 变量初始化命令](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfd_post/i1308668.html)、[流向角定义](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfd_post/i1306553.html)、[Blade Aligned 前后缘位置](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfd_post/i1362519.html)。
