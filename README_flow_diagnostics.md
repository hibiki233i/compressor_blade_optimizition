# 进出口熵增与叶片角匹配：现阶段评估

## 结论

适合把**熵增和近叶片流向角**加入后处理诊断；现在不适合把“角度完全匹配”直接加为第三个优化目标。现有双目标仍为 `Efficiency` 与 `MassFlow`。在定义角差阈值、统一单位并验证角度与效率/损失的关系之前，不改历史 `training_data.csv`、Pareto 规则或恢复凭据。

熵是损失量，不是角度。角匹配需要同一展向位置的**转子相对流向角**和**叶片金属角**；前缘入射角与后缘偏差角应分别在前缘上游、后缘下游的固定截面评估。CFD-Post 的 `Velocity Flow Angle` 是相对速度在叶间平面的角度；本项目的 CFturbo beta 与它在已检查算例中近似满足 `beta_flow = 180° - Velocity Flow Angle`。这个换算是当前模型的实测约定，不能直接套用到其他转向或几何。CLI 输出的列名故意带有 `proxy`。

## 已实现的只读提取

`blade_shape_flow_diagnostics.py` 从指定 `.res` 调用现有 CFX-Post 25.1，临时生成 `.cse`，不修改原始 `.res`、算例目录、训练 CSV 或主求解链。它读取：

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

## 对现有结果的观察

用当前工程的 13 份已成功 `.res` 做了探索性检查：选取效率较高/较低的算例及按编号分散的算例，样本并非随机抽取。13 份均成功提取，入口/出口质量流量相对差约为 0.02%–0.10%。入口到出口静熵升高约 934–987 J/(kg·K)；近前缘到近后缘的熵升约 836–884 J/(kg·K)。这说明叶道内部和后缘至出口的混合都可能贡献损失，但这些量还未经过收敛与测量面敏感性全面验证。

在这 13 份样本中，效率与进出口熵升的 Pearson 相关约为 -0.44，与近叶道熵升约为 -0.64；四处角差代理值的绝对值均值与效率约为 -0.78。这是小样本关联，不是角差造成效率变化的证据，也不能直接用来定惩罚权重。四处角差绝对值的中位数分别为：前缘 hub 4.9°、前缘 shroud 1.7°、后缘 hub 5.0°、后缘 shroud 10.5°。后缘 shroud 值值得进一步核查；当前 7 变量局部搜索把 `shroud_beta_4_deg_offset` 固定，暂时无法直接调整该金属角。

测量位置很关键。`case_000000` 在计算域出口边界得到的 shroud 出口角差代理值约为 -19.6°，在近后缘 0.78 截面约为 -8.6°。把近叶片截面改为前缘 0.20/0.24、后缘 0.80/0.76 时，四处角差也有变化；后缘 shroud 在这三组位置间约为 -7.0° 至 -8.6°。因此优化时必须固定截面定义，并先检查几何、网格和流量分布对该指标的影响。

## 单位问题与下一步

当前 `Extract_Results.cse` 写入 `CFX_Results.txt` 时直接打印 CFX 内部单位数值；此工程结果文件的基本单位是 mm、g、K。以 `case_000000` 为例，现有训练 CSV 的 `MassFlow=4.18154` 实际对应约 **0.00418154 kg/s**（10 个叶道），而 `Power=156032338344.22` 按内部单位换算约为 **156.03 W**。当前 `MassFlow` 数值相当于 g/s，`Power` 数值相当于内部功率单位。若将新算例直接改成 SI 后追加旧 CSV，就会混合两个单位体系并破坏代理模型、Pareto 容差和诊断；应先做带版本、可回滚的历史数据迁移，并同步缩放质量流量容差。提取工具本身始终显式输出 SI，不写训练 CSV。

建议的优化接入顺序：先用更多相同工况且经收敛审查的算例验证近前缘/后缘角差、熵升和效率的关系；在 CFD-Post 中目视确认 Blade Aligned 截面与 beta 角约定；再确定可接受的入射/偏差范围。若结果稳定，优先将过大的角差作为**约束或候选筛选诊断**，保留当前效率/流量双目标；需要调整后缘 shroud 时再评估是否解冻对应变量。新指标必须有明确单位、方向、失败时的处理及历史样本兼容策略。

相关 ANSYS 2025 R1 官方说明：[批处理 session](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfd_post/i1309479.html)、[Power Syntax evaluate](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfx_ref/i1321248.html)、[质量流量加权函数](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfx_ref/quant_func_list.html)、[Turbo 变量初始化命令](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfd_post/i1308668.html)、[流向角定义](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfd_post/i1306553.html)、[Blade Aligned 前后缘位置](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfd_post/i1362519.html)。
