"""Read-only CFX-Post diagnostics for entropy rise and boundary flow angles.

These values are observations for assessing a future incidence/deviation metric.
The inlet/outlet *boundaries* are not necessarily the blade leading/trailing edges.
Nothing here changes the optimizer's objectives or its CFD completion receipts.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path


LOCATION_NAME = re.compile(r"[A-Za-z0-9 _.-]+")
RAW_NAME = "flow_diagnostics.tsv"
ANGLE_CONVENTION = "wrap180(180 - relative_flow_angle_deg - blade_beta_deg)"


def measurement_expressions(inlet: str, outlet: str, span_band: float) -> dict[str, str]:
    """CEL expressions with explicit SI conversion, in a stable output order."""
    if not 0.05 <= span_band <= 0.4:
        raise ValueError("span_band must be between 0.05 and 0.4.")
    for location in (inlet, outlet):
        if not LOCATION_NAME.fullmatch(location):
            raise ValueError(f"Unsafe CFX location name: {location!r}")

    expressions: dict[str, str] = {}
    for side, location in (
        ("in", inlet), ("out", outlet), ("le", "Near LE"), ("te", "Near TE")
    ):
        at = f"\\@{location}"  # Escape @ inside a Power Syntax Perl string.
        expressions[f"mass_flow_{side}_kg_s"] = f"massFlow(){at} / 1 [kg s^-1]"
        expressions[f"entropy_{side}_j_kg_k"] = (
            f"massFlowAveAbs(Static Entropy){at} / 1 [J kg^-1 K^-1]"
        )
        expressions[f"entropy_signed_{side}_j_kg_k"] = (
            f"massFlowAve(Static Entropy){at} / 1 [J kg^-1 K^-1]"
        )
        for band, condition in (
            ("hub", f"Span Normalized < {span_band:.6f}"),
            ("shroud", f"Span Normalized > {1 - span_band:.6f}"),
        ):
            mask = f"if({condition}, 1, 0)"
            expressions[f"{side}_{band}_mass_fraction"] = f"massFlowAveAbs({mask}){at}"
            expressions[f"{side}_{band}_entropy_weighted"] = (
                f"massFlowAveAbs(if({condition}, Static Entropy, "
                f"0 [J kg^-1 K^-1])){at} / 1 [J kg^-1 K^-1]"
            )
            expressions[f"{side}_{band}_angle_sin_weighted"] = (
                f"massFlowAveAbs(if({condition}, sin(Velocity Flow Angle), 0)){at}"
            )
            expressions[f"{side}_{band}_angle_cos_weighted"] = (
                f"massFlowAveAbs(if({condition}, cos(Velocity Flow Angle), 0)){at}"
            )
    return expressions


def render_session(
    expressions: dict[str, str], *, le_station: float = 0.22,
    te_station: float = 0.78, turbo_domain: str = "R1",
) -> str:
    """Build a CFX-Post 25.1 session that fails closed on absent measurements."""
    if not 0 < le_station < 0.25 or not 0.75 < te_station < 1:
        raise ValueError("Near-edge stations must lie upstream of 0.25 and downstream of 0.75.")
    if not LOCATION_NAME.fullmatch(turbo_domain):
        raise ValueError(f"Unsafe turbo domain name: {turbo_domain!r}")
    lines = [
        "COMMAND FILE:",
        "  CFX Post Version = 25.1",
        "END",
        ">turbo init",
        ">turbo more vars",
    ]
    for name, station in (("Near LE", le_station), ("Near TE", te_station)):
        lines.extend([
            f"TURBO SURFACE: {name}",
            "  Option = Constant Blade Aligned",
            f"  Streamwise Location = {station:.6f}",
            "  Surface Type = Slice",
            f"  Turbo Domain List = {turbo_domain}",
            "  Draw Faces = Off",
            "  Draw Lines = Off",
            "END",
        ])
    lines.append(f'! open(my $out, ">{RAW_NAME}") or die "Cannot create {RAW_NAME}: $!";')
    for index, (name, expression) in enumerate(expressions.items()):
        lines.append(f'! my ($v{index}, $u{index}) = evaluate("{expression}");')
        lines.append(f'! print $out "{name}\\t$v{index}\\n";')
    lines.extend(['! print $out "__complete__\\t1\\n";', "! close($out);", ">quit", ""])
    return "\n".join(lines)


def parse_measurements(text: str, required: set[str]) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 2:
            raise ValueError(f"Malformed CFX-Post output row: {line!r}")
        key, raw = fields
        if key in values:
            raise ValueError(f"Duplicate CFX-Post field: {key}")
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"Missing or invalid CFX-Post field {key}: {raw!r}") from exc
        if not math.isfinite(value):
            raise ValueError(f"Non-finite CFX-Post field: {key}")
        values[key] = value
    if values.get("__complete__") != 1 or required != values.keys() - {"__complete__"}:
        missing = sorted(required - values.keys())
        extra = sorted(values.keys() - required - {"__complete__"})
        raise ValueError(f"Incomplete CFX-Post output; missing={missing}, extra={extra}")
    del values["__complete__"]
    return values


def summarize(values: dict[str, float], *, min_band_fraction: float = 0.02) -> dict[str, object]:
    result: dict[str, object] = dict(values)
    result["delta_entropy_j_kg_k"] = values["entropy_out_j_kg_k"] - values["entropy_in_j_kg_k"]
    result["delta_entropy_signed_j_kg_k"] = (
        values["entropy_signed_out_j_kg_k"] - values["entropy_signed_in_j_kg_k"]
    )
    mass_in, mass_out = values["mass_flow_in_kg_s"], values["mass_flow_out_kg_s"]
    result["mass_flow_closure_relative"] = (
        abs(abs(mass_in) - abs(mass_out)) / max(abs(mass_in), abs(mass_out))
        if max(abs(mass_in), abs(mass_out)) > 0 else math.inf
    )
    issues: list[str] = []
    if mass_in * mass_out >= 0:
        issues.append("inlet/outlet mass-flow signs are not opposite")
    if result["mass_flow_closure_relative"] > 0.01:
        issues.append("inlet/outlet mass-flow mismatch exceeds 1%")
    if result["delta_entropy_j_kg_k"] < 0:
        issues.append("negative boundary entropy rise")
    if abs(result["delta_entropy_j_kg_k"] - result["delta_entropy_signed_j_kg_k"]) > 0.01 * max(
        abs(result["delta_entropy_j_kg_k"]), 1.0
    ):
        issues.append("signed and absolute-flux entropy rises differ by over 1%")

    for side in ("in", "out", "le", "te"):
        for band in ("hub", "shroud"):
            prefix = f"{side}_{band}"
            fraction = values[f"{prefix}_mass_fraction"]
            result[f"{prefix}_entropy_j_kg_k"] = None
            result[f"{prefix}_flow_angle_deg"] = None
            result[f"{prefix}_angle_coherence"] = None
            if fraction < min_band_fraction:
                issues.append(f"{prefix} band carries under {min_band_fraction:.0%} of absolute mass flow")
                continue
            result[f"{prefix}_entropy_j_kg_k"] = values[f"{prefix}_entropy_weighted"] / fraction
            sine = values[f"{prefix}_angle_sin_weighted"] / fraction
            cosine = values[f"{prefix}_angle_cos_weighted"] / fraction
            coherence = math.hypot(sine, cosine)
            result[f"{prefix}_angle_coherence"] = coherence
            if coherence < 0.8:
                issues.append(f"{prefix} angle distribution has low circular concentration")
                continue
            result[f"{prefix}_flow_angle_deg"] = math.degrees(math.atan2(sine, cosine))
    result["delta_entropy_blade_passage_j_kg_k"] = (
        values["entropy_te_j_kg_k"] - values["entropy_le_j_kg_k"]
    )
    if result["delta_entropy_blade_passage_j_kg_k"] < 0:
        issues.append("negative near-blade entropy rise")
    for side in ("le", "te"):
        relative = abs(abs(values[f"mass_flow_{side}_kg_s"]) - abs(mass_in)) / abs(mass_in) if mass_in else math.inf
        result[f"mass_flow_{side}_vs_in_relative"] = relative
        if relative > 0.01:
            issues.append(f"{side} station mass flow differs from inlet by over 1%")
    result["quality_issues"] = "; ".join(issues)
    result["quality_ok"] = not issues
    return result


def read_candidate_angles(res_path: Path) -> dict[str, float | str]:
    candidate_path = res_path.parent / "candidate.json"
    if not candidate_path.is_file():
        return {}
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    geometry = candidate["geometry"]
    result: dict[str, float | str] = {"run_id": str(candidate.get("run_id", res_path.parent.name))}
    for span in ("hub", "shroud"):
        beta = geometry[f"{span}_beta_rad"]
        if not isinstance(beta, list) or len(beta) < 2:
            raise ValueError(f"Invalid {span} beta array in {candidate_path}")
        if not all(math.isfinite(float(value)) for value in beta):
            raise ValueError(f"Non-finite {span} beta angle in {candidate_path}")
        result[f"blade_{span}_leading_deg"] = math.degrees(float(beta[0]))
        result[f"blade_{span}_trailing_deg"] = math.degrees(float(beta[-1]))
    return result


def add_angle_proxies(result: dict[str, object]) -> None:
    """Project-specific convention check; never used as an optimizer target."""
    result["angle_proxy_convention"] = ANGLE_CONVENTION
    result["angle_proxy_comparison"] = "span_band_flow_vs_endpoint_metal_angle"
    available = 0
    for span in ("hub", "shroud"):
        for station, edge, label in (
            ("le", "leading", "incidence"), ("te", "trailing", "deviation")
        ):
            flow = result.get(f"{station}_{span}_flow_angle_deg")
            blade = result.get(f"blade_{span}_{edge}_deg")
            key = f"{station}_{span}_{label}_proxy_deg"
            result[key] = None
            if flow is None or blade is None:
                continue
            if not all(math.isfinite(float(value)) for value in (flow, blade)):
                raise ValueError(f"Non-finite angle for {key}")
            # In this rotor, the CFturbo beta convention empirically maps to
            # 180 deg minus CFD-Post's relative Velocity Flow Angle.
            difference = 180.0 - float(flow) - float(blade)
            result[key] = (
                (difference + 180.0) % 360.0 - 180.0
            )
            available += 1
    result["angle_proxies_available"] = available == 4


def analyze_res(
    res_path: Path, post_exe: Path, *, inlet: str, outlet: str, span_band: float,
    le_station: float = 0.22, te_station: float = 0.78, turbo_domain: str = "R1",
    log_dir: Path | None = None,
) -> dict[str, object]:
    res_path = res_path.resolve()
    post_exe = post_exe.resolve()
    if not res_path.is_file() or res_path.suffix.lower() != ".res":
        raise ValueError(f"CFX result file not found: {res_path}")
    if not post_exe.is_file():
        raise ValueError(f"CFX-Post executable not found: {post_exe}")
    expressions = measurement_expressions(inlet, outlet, span_band)
    session_text = render_session(
        expressions, le_station=le_station, te_station=te_station,
        turbo_domain=turbo_domain,
    )
    candidate_angles = read_candidate_angles(res_path)
    if log_dir is not None:
        log_dir = log_dir.resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
    # Keep the session, raw measurements and logs even if parsing fails.
    work = Path(tempfile.mkdtemp(prefix="blade_flow_", dir=log_dir))
    try:
        session = work / "Extract_Flow_Diagnostics.cse"
        session.write_text(session_text, encoding="utf-8")
        command = [str(post_exe), "-batch", str(session), "-res", str(res_path)]
        (work / "command.json").write_text(
            json.dumps({"argv": command, "cwd": str(work)}, indent=2), encoding="utf-8",
        )
        process = subprocess.run(
            command,
            cwd=work,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        (work / "cfxpost.log").write_text(process.stdout, encoding="utf-8")
        (work / "returncode.txt").write_text(str(process.returncode), encoding="utf-8")
        output = work / RAW_NAME
        errors = work / "cfdpost_error.log"
        detail = errors.read_text(encoding="utf-8", errors="replace") if errors.exists() else ""
        if process.returncode != 0 or not output.is_file() or detail.strip():
            raise RuntimeError(
                f"CFX-Post failed for {res_path} (exit {process.returncode}). "
                f"{detail.strip() or process.stdout[-2000:]}"
            )
        values = parse_measurements(output.read_text(encoding="utf-8"), set(expressions))
    except Exception as exc:
        (work / "failure.txt").write_text(str(exc), encoding="utf-8")
        raise RuntimeError(f"Flow diagnostics failed; logs retained at {work}: {exc}") from exc
    result = summarize(values)
    result.update(candidate_angles)
    add_angle_proxies(result)
    result["post_log_dir"] = str(work)
    result["inlet_location"] = inlet
    result["outlet_location"] = outlet
    result["turbo_domain"] = turbo_domain
    result["candidate_path"] = str(res_path.parent / "candidate.json") if candidate_angles else ""
    result["candidate_res_identity_verified"] = False
    result["res_path"] = str(res_path)
    result["res_size_bytes"] = res_path.stat().st_size
    result["span_band"] = span_band
    result["near_le_blade_aligned"] = le_station
    result["near_te_blade_aligned"] = te_station
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("blade_shape_config.json"))
    parser.add_argument("--res", action="append", type=Path, required=True, help="Repeat for multiple .res files.")
    parser.add_argument("--output", type=Path, required=True, help="Write a new diagnostics CSV.")
    parser.add_argument("--post-exe", type=Path, help="Override cfx5post.exe from config paths.cfx_bin_dir.")
    parser.add_argument("--inlet", default="R1 Inlet")
    parser.add_argument("--outlet", default="R1 Outlet")
    parser.add_argument("--span-band", type=float, default=0.2)
    parser.add_argument("--le-station", type=float, default=0.22)
    parser.add_argument("--te-station", type=float, default=0.78)
    parser.add_argument("--turbo-domain", default="R1")
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"Output already exists; choose a new diagnostics CSV: {args.output}")
    if args.post_exe is not None:
        post_exe = args.post_exe
    else:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        post_exe = Path(config["paths"]["cfx_bin_dir"]) / "cfx5post.exe"
    rows = [
        analyze_res(
            path, post_exe, inlet=args.inlet, outlet=args.outlet,
            span_band=args.span_band, le_station=args.le_station,
            te_station=args.te_station, turbo_domain=args.turbo_domain,
            log_dir=args.output.with_name(args.output.name + ".logs"),
        )
        for path in args.res
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with args.output.open("x", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} read-only flow diagnostics to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
