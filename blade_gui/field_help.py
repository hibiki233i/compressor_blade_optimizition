"""Explanations for the 项目设置 fields.

Each entry says what the value does *in this code*, a recommended value or
range, and how to derive it. ``live_hint`` turns normalised values into degrees
for the current variable bounds. No Qt imports: the config page renders these,
and tests check that every entry names a field that exists.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

# What else a change touches; appended to the entries that are part of it.
PHYSICAL = ("属于物理签名（refinement.physical_signature）：存在未完成的待评估队列或冻结的边界方案时修改，"
            "续跑会报 “Inputs changed … inspect before resuming” 并停下；已完成的记录不受影响。")
SLICE = "属于 slice_id：修改后当前切片的预测校准与诊断窗口从零开始累计。"
LOCAL = "改动任一局部搜索设置都会重置 local_search_state.json（半径回到初始值，成功/失败计数清零）。"
CFX = "属于 CFX 输入签名：已有算例在签名改变后不再复用其结果（cfx_state.json 会拒绝）。"
#: short names for the notes above, used as tags in the full help sheet
NOTE_TAGS = {PHYSICAL: "物理签名", SLICE: "切片校准", LOCAL: "局部搜索状态", CFX: "CFX 签名"}
NORM = "“归一化”指各变量先除以自身范围（上限 − 下限）再求欧氏距离，因此 0.02 相当于单个变量移动其范围的 2%。"


@dataclass(frozen=True)
class Help:
    hint: str                 # short text for the hint column
    text: str                 # what it does, recommendation, how to derive
    notes: tuple[str, ...] = ()


HELP: dict[str, Help] = {
    # ------------------------------------------------------------ runtime / CFX
    "runtime.cfx_cores": Help(
        "≤ 物理核数",
        "CFX 求解的本地并行分区数（cfx5solve -par-local -part N）。\n"
        "推荐：不超过本机物理核数（不算超线程）；单通道网格较小时分区过多反而变慢，每个分区宜保留数万个以上节点。",
        (CFX,)),
    "cfx_convergence.rms_target": Help(
        "推荐 1e-5；上限 1e-5",
        "最终验收门槛：.out 中最后一次输出的各方程 RMS 残差的最大值必须 ≤ 此值，否则该算例记为失败，结果不进入训练集。"
        "代码只接受 (0, 1e-5]。\n"
        "推荐：1e-5 是叶轮机械稳态计算常用的“充分收敛”水平；要分辨很小的效率差可取 1e-6，但迭代数与失败率会上升。\n"
        "残差只是必要条件：效率、流量监测点是否走平以及进出口质量守恒仍需人工检查。",
        (CFX, PHYSICAL)),
    "cfx_convergence.restart_iterations": Help(
        "1500–2000",
        "首次求解用完模板中的最大迭代数仍未达到 RMS 门槛时，自动从该结果续算一次，续算的最大迭代数取此值。"
        "只续算一次；代码限定为 1500–2000 的整数。",
        (CFX, PHYSICAL)),
    "cfx_convergence.flow_analysis": Help(
        "与模板一致",
        "CFX 模板中 FLOW 对象的名称（默认 “Flow Analysis 1”），用于写入收敛控制 CCL。必须与 BaseModel.cfx 中的名称完全一致，否则 CFX-Pre 找不到该对象。",
        (CFX, PHYSICAL)),
    "runtime.n_blades": Help(
        "主叶片数",
        "两个作用：① 不等于 10 时以 -BladeCount 传给 TurboGrid，决定周期面；② 把单通道的 MassFlow 和 Power 乘以此数，换算成整圈。\n"
        "必须等于 CFturbo 模板中的主叶片数。当前模板未启用分流叶片。",
        (CFX, PHYSICAL)),
    "runtime.rpm": Help(
        "仅记录；check-pre --def 可核对",
        "当前代码不会把它传给 CFturbo 或 CFX：实际转速由 CFX 模板（BaseModel.cfx）决定。"
        "它只进入物理签名，用来标明这批数据对应的工况。请填写与模板一致的值；在这里修改不会改变 CFD。\n"
        "核对：blade_shape_cfx_runner.py check-pre --def 已有算例的 Impeller.def，会用 cfx5cmds 只读导出其 CCL，"
        "把各 Angular Velocity（数值或命名表达式，rpm 或 rad/s）与此值比较；不一致时返回 1，"
        "无法解析的 CEL 表达式报告为 unverified。",
        (PHYSICAL,)),
    "runtime.mass_flow": Help(
        "仅记录，不写入 CFX",
        "当前代码不会把它传给 CFX：进口条件由模板决定，计算得到的 MassFlow 是结果而不是输入。"
        "它只进入物理签名，用于记录设计工况；在这里修改不会改变 CFD。check-pre --def 不比较此项。",
        (PHYSICAL,)),
    "runtime.alpha0": Help(
        "仅记录，不写入 CFX",
        "当前代码不会把它传给 CFX：进口气流角由模板中的进口边界条件决定。它只进入物理签名；在这里修改不会改变 CFD。"
        "check-pre --def 不比较此项。",
        (PHYSICAL,)),
    "runtime.p_out_pa": Help(
        "写入 MyBackPressure",
        "每个算例都写入 update_bc.ccl：MyBackPressure = 值 [Pa]，由模板的出口边界引用。\n"
        "CFX 边界压力相对于模板的 Reference Pressure：若参考压力为 1 atm，这里填的是相对压力（表压），不是绝对压力。"
        "具体是静压、平均静压还是其他形式，取决于模板出口边界如何引用该表达式。",
        (CFX, PHYSICAL)),
    "runtime.initial_samples": Help(
        "推荐 ≈ 10 × 活动变量数",
        "首次建模前的随机 DOE 点数：training_data.csv 的记录数（含失败）少于此值时，先补随机点，再开始主动学习。\n"
        "推荐：GP 代理常用“10 × 维数”的经验规则，即约 10 × 活动变量数；预算紧张时不宜少于 5 × 维数。"
        "已有数据时只补足差额。"),
    "runtime.iterations": Help(
        "主动学习轮数",
        "主动学习的轮数。每轮重新训练代理模型并选出 batch_size 个点送去 CFD。"
        "本次新增 CFD 总数 ≤ min(iterations × batch_size + DOE 补点数, max_new_cfd)。"),
    "runtime.batch_size": Help(
        "推荐取角色数的整数倍",
        "每轮选出的点数。批内按“候选角色顺序”轮流选点（第 1 个 ehvi，第 2 个 uncertainty，……）。\n"
        "推荐：取角色数（默认 3）的整数倍，使各角色份额相同；批越大，每次训练后获得的新信息越分散。"),
    "runtime.max_new_cfd": Help(
        "本次新增上限",
        "单次运行最多新增的真实 CFD 尝试次数（含失败），是预算的硬上限。续跑时同样生效。"),
    "runtime.candidate_pool_size": Help(
        "300–2000",
        "每轮除 NSGA-II 结果外再生成的候选点数：开启局部搜索时按“局部候选比例”从局部框内抽取，其余为全局随机可行点。"
        "这些点都由代理模型打分。\n"
        "计算量约为 (候选池 + NSGA-II 种群) × EHVI 总采样数，相对一次 CFD 可以忽略。维数越高、约束越紧，越需要更大的候选池。"),
    "runtime.nsga2_pop_size": Help(
        "推荐 ≥ 10 × 活动变量数",
        "在代理模型预测均值上运行的 NSGA-II 种群大小；最后一代并入候选集。推荐不小于 10 × 活动变量数（7 个活动变量时 80 左右）。"),
    "runtime.nsga2_generations": Help(
        "50–100",
        "NSGA-II 代数。只调用代理模型，不消耗 CFD；代数越多，预测前沿越收敛。"
        "由于最终仍由 EHVI 等角色挑点，过度收敛的意义不大，50–100 通常足够。"),
    "runtime.seed": Help(
        "可复现",
        "随机种子。DOE、候选池、NSGA-II 和 EHVI 采样都由它派生（每轮加上固定偏移）。"
        "相同数据和种子会选出相同的点；想换一组候选时修改它。"),
    # ------------------------------------------------------------ surrogate
    "surrogate.model": Help(
        "推荐 gp",
        "gp：高斯过程（Kriging，scikit-learn），给出校准较好的预测不确定度，EHVI 和 uncertainty 角色都依赖它。\n"
        "rbf_ridge_ensemble：RBF 岭回归集成，用模型间的离散度近似不确定度。\n"
        "gp 无法建立（未安装 scikit-learn，或样本少于 3 个）时改用“回退模型”。其他名称是配置错误，不会悄悄换成 RBF。"),
    "surrogate.fallback_model": Help(
        "gp 不可用时使用；none = 报错停止",
        "主模型为 gp 但无法建立（未安装 scikit-learn，或样本少于 3 个）时改用此模型，并在日志打印原因。\n"
        "rbf_ridge_ensemble（默认）：沿用以往行为。none：不换模型，直接报错停止，适合要求全程同一代理模型的对比实验。"
        "选 gp 作为回退没有意义，gp 不可用时同样报错。\n"
        "诊断记录中的 surrogate_model 列记下实际使用的模型，预测校准只用同一模型的历史残差。"),
    "surrogate.ehvi_y_samples": Help(
        "推荐 256",
        "EHVI（期望超体积改进）用蒙特卡洛估计：对每个候选，从预测分布抽取若干目标值样本，求平均超体积增量。"
        "此值是粗估阶段的样本数；蒙特卡洛误差约与 1/√N 成正比。"),
    "surrogate.ehvi_validation_samples": Help(
        "推荐 ≥ 4 × 粗估采样数",
        "EHVI 实际使用的总样本数（不小于粗估采样数），前 ehvi_y_samples 个样本另给出粗估值。"
        "两者之差以 ehvi_sampling_change 写入诊断 CSV。\n"
        "判断方法：若 sampling_change 相对 EHVI 本身很小，采样已足够；若会改变候选排序，应加大采样数。"),
    # ------------------------------------------------------------ pareto
    "pareto.use_engineering_tolerance": Help(
        "推荐开启",
        "开启时，pareto_front.csv 使用 ε-支配：差距在容差以内的点不算被支配，避免前沿里只因数值噪声而胜出的点挤掉其他点。"
        "严格前沿始终另存为 pareto_front_strict.csv。\n"
        "注意：关闭后，uncertainty 角色不再用容差统一两个目标的量纲，选点会偏向数值单位较大的目标。"),
    "pareto.tolerances.Efficiency": Help(
        "≈ CFD 数值不确定度",
        "单位与 training_data.csv 中 Efficiency 列相同。它同时用于三处：\n"
        "① 工程 Pareto 的 ε；② uncertainty 角色中预测标准差的归一化尺度；"
        "③ 诊断门：EHVI 点的预测 MAE ≤ 容差，且 2σ 覆盖率 ≥ 5/6 时才算通过。\n"
        "如何确定：取同一几何在可接受设置变化下 Efficiency 的差值，例如 RMS 1e-5 与 1e-6 两次计算之差、相邻两级网格之差（或 GCI），"
        "取其中较大者。不要小于 CFD 本身能分辨的差异。"),
    "pareto.tolerances.MassFlow": Help(
        "≈ CFD 数值不确定度",
        "单位与 training_data.csv 中 MassFlow 列相同（整圈值，已乘叶片数）。三处用途与效率容差相同。\n"
        "如何确定：同上，取收敛水平或网格变化引起的 MassFlow 差值；也可参考进出口质量流量的不平衡量。"),
    # ------------------------------------------------------------ constraints
    "constraints.max_beta_offset_deg": Help(
        "β 相对基准的包络",
        "hub、shroud 各 5 个 β 控制点相对基准的最大绝对偏移（度）。与各变量自身的上下限同时生效：变量边界都在此值以内时，这条约束不起作用。",
        (PHYSICAL,)),
    "constraints.max_theta_offset_deg": Help(
        "θ 相对基准的包络",
        "hub、shroud θ 偏移的最大绝对值（度）。与变量自身上下限同时生效：变量边界都在此值以内时，这条约束不起作用。",
        (PHYSICAL,)),
    "constraints.max_generated_beta_step_deg": Help(
        "叶片角光顺度",
        "把 hub、shroud 的 β 分布线性插值到子午归一化位置 0–1 上 31 个等距点（间距 1/30），要求相邻两点的 |Δβ| 不超过："
        "max(此值, 基准自身的最大相邻步长 + 基准步长余量)。用来限制叶片角突变（局部曲率过大）。\n"
        "如何确定：先看基准的最大相邻步长，再按允许叶型比基准“更陡”多少来设定。设得过小，几乎所有偏离基准的候选都会被拒绝。",
        (PHYSICAL,)),
    "constraints.baseline_step_margin_deg": Help(
        "推荐 0.5",
        "光顺约束的余量。基准本身的最大步长若已超过上一项，实际门槛取“基准最大步长 + 此余量”，保证基准及其附近的平缓变化总是可行。",
        (PHYSICAL,)),
    "constraints.duplicate_distance_norm": Help(
        "推荐 0.01–0.05",
        "候选与已有点（仅比较同一切片的记录）在活动变量上的最小归一化距离，小于此值视为重复而丢弃。" + NORM + "\n"
        "如何确定：取一个几何差值，使它引起的目标变化大于 Pareto 容差（即 CFD 能分辨）。"
        "过大会使候选池凑不满，甚至出现 “No feasible candidates”；过小则把 CFD 预算浪费在噪声级别的差异上。",
        (PHYSICAL,)),
    # ------------------------------------------------------------ search
    "search.slice_tolerance_norm": Help(
        "推荐 1e-8（精确匹配）",
        "当前切片的判定：一条记录的固定变量与当前固定值之间的归一化距离 ≤ 此值时，算作“同一切片”。"
        "挑战模型、局部搜索、重复判定和诊断都只用同切片的记录。" + NORM + "\n"
        "1e-8 表示只认按这组固定值生成的记录（数值上完全相同）。只有在有意把固定值略有差异的旧数据也算进来时才放宽，"
        "例如 0.01（约为各固定变量范围的 1%）。",
        (SLICE, PHYSICAL)),
    # ------------------------------------------------------------ refinement
    "refinement.challenger_min_samples": Help(
        "推荐 ≈ 2 × 活动变量数",
        "同一切片的成功样本达到此数后，另训练一个只用活动变量的“挑战”模型，与全 12 变量的主模型对照（结果写入诊断）。"
        "实际下限为 4。推荐约 2 × 活动变量数；样本过少时挑战模型不可靠。"),
    "refinement.diagnostic_min_points": Help(
        "推荐 6–10",
        "诊断窗口内的前瞻预测点（先预测、后经 CFD 验证）达到此数后，开始两件事："
        "① 用残差放大预测标准差，使 2σ 覆盖率达标；② 判定诊断门（MAE ≤ 容差且 2σ 覆盖率 ≥ 5/6）。\n"
        "覆盖率阈值是 5/6，因此至少需要 6 个点，才能做到“允许 1 个落在区间外”。"),
    "refinement.diagnostic_window": Help(
        "推荐 2–3 × 最少点数",
        "校准和诊断只使用本切片最近的 N 个前瞻预测。窗口越大，统计越稳定，但对模型改进的反应越慢。",
        (SLICE,)),
    "refinement.candidate_roles": Help(
        "逗号分隔，按序轮换",
        "批内选点的角色顺序（循环使用）：\n"
        "ehvi：期望超体积改进最大，即最可能推进 Pareto 前沿；\n"
        "uncertainty：预测标准差 ÷ Pareto 容差的平均值最大，偏重探索；\n"
        "diversity：离已有点最远，用于填补空白。\n"
        "可重复或删减，例如 “ehvi, ehvi, uncertainty” 更偏向利用。"),
    "refinement.boundary_variables": Help(
        "恰好 4 个活动变量",
        "write-boundary-plan 用到的 4 个活动变量：singles 阶段对它们各做 1 个单变量点，pairs 阶段做 6 个两两组合。"
        "通常选取在当前最优点附近已贴近边界的变量。必须恰好 4 个，且互不相同。"),
    "refinement.extension.variable": Help(
        "须为边界变量之一",
        "extension 阶段要外推的变量：先在其上限处计算对照点，再计算超出上限的外推点，用来检验放宽该变量上限是否值得。"),
    "refinement.extension.value": Help(
        "须大于该变量上限",
        "外推点的取值（度），必须严格大于该变量当前的上限。"),
    "refinement.local_search.enabled": Help(
        "信赖域式局部搜索",
        "开启后，围绕同切片 Pareto 前沿上的 3 个点（效率最高、流量最大、拐点）构造局部框，"
        "部分候选和 NSGA-II 初始种群都在这些框内生成。",
        (LOCAL,)),
    "refinement.local_search.fraction": Help(
        "推荐 0.5–0.8",
        "候选池中来自局部框的比例，其余为全局随机可行点。比例越高越偏向利用；数据少或前沿尚未成形时宜调低。",
        (LOCAL,)),
    "refinement.local_search.initial_radius_norm": Help(
        "推荐 0.1–0.2",
        "局部框的初始半宽：每个变量取 中心 ± 半径 × (上限 − 下限)，再截到变量边界内。须满足 最小 ≤ 初始 ≤ 最大。",
        (LOCAL,)),
    "refinement.local_search.min_radius_norm": Help(
        "≥ 重复距离阈值",
        "半径收缩的下限。宜明显大于“重复距离阈值”，否则框内很难找到不重复的候选。",
        (LOCAL,)),
    "refinement.local_search.max_radius_norm": Help(
        "推荐 0.3–0.5",
        "半径扩大的上限。达到 0.5 时，框基本覆盖整个变量范围，局部搜索退化为全局搜索。",
        (LOCAL,)),
    "refinement.local_search.successes_to_expand": Help(
        "推荐 2–3",
        "连续成功（超体积增益 > 阈值）达到此次数后，半径加倍（不超过最大值）。",
        (LOCAL,)),
    "refinement.local_search.failures_to_shrink": Help(
        "推荐 3–5",
        "连续失败（含 CFD 失败或增益不足）达到此次数后，半径减半（不低于最小值）。"
        "取值宜大于“扩大所需成功数”：CFD 噪声较大时，可避免框因偶然失败而过早收缩。",
        (LOCAL,)),
    "refinement.local_search.min_relative_hv_gain": Help(
        "推荐 1e-4（0.01%）",
        "一次 CFD 算作“成功”的门槛：超体积增量 > 此值 × 增加前的超体积。\n"
        "如何确定：门槛过低，CFD 噪声也会被算作成功；可参照“目标各移动一个 Pareto 容差”所带来的超体积相对变化来设定。",
        (LOCAL,)),
}


# ---------------------------------------------------------------- live hints
def _variables(config: dict[str, Any]) -> list[dict[str, Any]]:
    items = config.get("variables")
    return [v for v in items if isinstance(v, dict)] if isinstance(items, list) else []


def _spans(config: dict[str, Any], fixed: bool) -> list[float]:
    search = config.get("search") if isinstance(config.get("search"), dict) else {}
    frozen = search.get("fixed_variables") if isinstance(search.get("fixed_variables"), dict) else {}
    spans = []
    for v in _variables(config):
        try:
            span = float(v["upper"]) - float(v["lower"])
        except (KeyError, TypeError, ValueError):
            continue
        if (v.get("name") in frozen) == fixed and span > 0:
            spans.append(span)
    return spans


def _degrees(value: float, spans: list[float], prefix: str = "") -> str:
    if not spans:
        return ""
    lo, hi = value * min(spans), value * max(spans)
    text = f"{lo:.3g}°" if abs(hi - lo) < 5e-4 else f"{lo:.3g}–{hi:.3g}°"
    return f"{prefix}{text}"


def _max_abs_bound(config: dict[str, Any], names: tuple[str, ...]) -> float | None:
    values = []
    for v in _variables(config):
        if any(token in str(v.get("name", "")) for token in names):
            try:
                values += [abs(float(v["lower"])), abs(float(v["upper"]))]
            except (KeyError, TypeError, ValueError):
                continue
    return max(values) if values else None


def live_hint(key: str, config: dict[str, Any], value: Any) -> str:
    """A hint computed from the current form (degrees, counts), or ``""``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = None
    active = len(_spans(config, fixed=False))
    if number is not None and key == "constraints.duplicate_distance_norm":
        return _degrees(number, _spans(config, False), "单变量 ≈ ")
    if number is not None and key == "search.slice_tolerance_norm":
        if number < 1e-6:
            return "精确匹配"
        return _degrees(number, _spans(config, True), "固定变量 ≈ ")
    if number is not None and key.startswith("refinement.local_search.") and key.endswith("radius_norm"):
        return _degrees(number, _spans(config, False), "± ")
    if key == "runtime.initial_samples" and active:
        return f"推荐 ≈ 10×{active} = {10 * active}"
    if key == "runtime.nsga2_pop_size" and active:
        return f"推荐 ≥ 10×{active} = {10 * active}"
    if key == "refinement.challenger_min_samples" and active:
        return f"推荐 ≈ 2×{active} = {2 * active}"
    if key == "runtime.batch_size" and number is not None:
        roles = (config.get("refinement") or {}).get("candidate_roles") if isinstance(config.get("refinement"), dict) else None
        count = len(roles) if isinstance(roles, list) and roles else 3
        return f"角色数 {count}" + ("，已整除" if number % count == 0 else "，未整除")
    if number is not None and key in ("constraints.max_beta_offset_deg", "constraints.max_theta_offset_deg"):
        bound = _max_abs_bound(config, ("beta",) if "beta" in key else ("theta",))
        if bound is not None:
            return f"边界最大 {bound:g}°：" + ("不起作用" if number >= bound else "比边界更严")
    return ""


# ---------------------------------------------------------------- rendering
def _paragraphs(entry: Help, *, tags: bool = False) -> str:
    parts = [html.escape(line) for line in entry.text.split("\n")]
    if tags and entry.notes:
        parts.append("<i>修改影响：" + html.escape("、".join(NOTE_TAGS[note] for note in entry.notes)) + "</i>")
    else:
        parts += [f"<i>{html.escape(note)}</i>" for note in entry.notes]
    return "".join(f"<p style='margin:0 0 5px 0'>{part}</p>" for part in parts)


def tooltip_html(key: str, label: str) -> str:
    """Rich-text tooltip (Qt wraps rich text, plain text would be one long line)."""
    entry = HELP.get(key)
    if entry is None:
        return ""
    return (f"<div style='max-width:420px'><b>{html.escape(label)}</b>"
            f" <span style='color:gray'>{html.escape(key)}</span>{_paragraphs(entry)}</div>")


def help_document_html(sections: list[tuple[str, list[tuple[str, str]]]]) -> str:
    """Every documented field, grouped like the form: ``[(section, [(key, label)])]``."""
    out = ["<p>" + html.escape(NORM) + "</p>", "<p><b>修改影响</b>（各参数末尾标注）：</p>"]
    out += [f"<p style='margin:0 0 4px 12px'><b>{html.escape(tag)}</b>：{html.escape(note)}</p>"
            for note, tag in NOTE_TAGS.items()]
    for title, fields in sections:
        rows = [(key, label) for key, label in fields if key in HELP]
        if not rows:
            continue
        out.append(f"<h3>{html.escape(title)}</h3>")
        for key, label in rows:
            out.append(f"<p style='margin:8px 0 2px 0'><b>{html.escape(label)}</b>"
                       f" <span style='color:gray'>{html.escape(key)} · {html.escape(HELP[key].hint)}</span></p>")
            out.append(_paragraphs(HELP[key], tags=True))
    return "".join(out)
