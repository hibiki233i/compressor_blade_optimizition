from __future__ import annotations

import glob
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class CfxResult:
    success: bool
    metrics: dict[str, float]
    message: str
    failure_stage: str = ""


def _as_posix(path: str | Path) -> str:
    return str(path).replace("\\", "/")


def _run_logged(cmd: list[str], cwd: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="ignore") as log:
        log.write("COMMAND: " + " ".join(cmd) + "\n\n")
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        while proc.poll() is None:
            time.sleep(2)
        return int(proc.returncode or 0)


def _parse_results(output_txt: Path, n_blades: int) -> dict[str, float]:
    values = output_txt.read_text(encoding="utf-8", errors="ignore").strip().split(",")
    if len(values) < 5:
        raise ValueError(f"Expected at least 5 comma-separated CFX values, got {len(values)}")
    return {
        "Efficiency": float(values[0]),
        "PressureRatio": float(values[1]),
        "Power": float(values[2]) * n_blades,
        "MassFlow": float(values[3]) * n_blades,
        "totalpressureratio": float(values[4]),
    }


def run_cfx_pipeline(
    working_dir: str | Path,
    run_id: str,
    *,
    p_out_pa: float,
    cfx_bin_dir: str | Path,
    template_cfx: str | Path,
    template_cse: str | Path,
    cores: int = 8,
    n_blades: int = 10,
) -> CfxResult:
    """Run CFX-Pre, CFX-Solve, and CFX-Post for a generated TurboGrid mesh."""
    work = Path(working_dir)
    gtm_file = work / "Impeller_Mesh.gtm"
    def_file = work / "Impeller.def"
    pre_script = work / "Update_Mesh.pre"
    ccl_file = work / "update_bc.ccl"
    output_txt = work / "CFX_Results.txt"

    if output_txt.exists():
        try:
            return CfxResult(True, _parse_results(output_txt, n_blades), "Recovered from existing CFX_Results.txt")
        except Exception as exc:
            return CfxResult(False, {}, f"Existing CFX_Results.txt could not be parsed: {exc}", "post")

    if not gtm_file.exists():
        return CfxResult(False, {}, f"TurboGrid mesh not found: {gtm_file}", "mesh")

    cfx_bin = Path(cfx_bin_dir)
    cfx5pre = cfx_bin / "cfx5pre.exe"
    cfx5solve = cfx_bin / "cfx5solve.exe"
    cfx5post = cfx_bin / "cfx5post.exe"
    for exe in [cfx5pre, cfx5solve, cfx5post]:
        if not exe.exists():
            return CfxResult(False, {}, f"Missing CFX executable: {exe}", "environment")

    ccl_file.write_text(
        "\n".join(
            [
                "LIBRARY:",
                "  CEL:",
                "    EXPRESSIONS:",
                f"      MyBackPressure = {p_out_pa} [Pa]",
                "    END",
                "  END",
                "END",
                "",
            ]
        ),
        encoding="utf-8",
    )

    existing_res = sorted(glob.glob(str(work / "*.res")), key=os.path.getmtime)
    if existing_res:
        res_file = Path(existing_res[-1])
    else:
        pre_script.write_text(
            f"""COMMAND FILE:
  CFX Pre Version = 25.1
END
>load filename={_as_posix(template_cfx)}
>update
> gtmImport filename={_as_posix(gtm_file)}, type=GTM, \
units=m, nameStrategy= Assembly
>update
>writeCaseFile filename={_as_posix(def_file)}, operation=\
write def file
>update
>quit
""",
            encoding="utf-8",
        )
        pre_ret = _run_logged([str(cfx5pre), "-batch", str(pre_script)], work, work / "cfx_pre.log")
        if pre_ret != 0 or not def_file.exists():
            return CfxResult(False, {}, f"CFX-Pre failed with exit code {pre_ret}", "pre")

        solve_cmd = [
            str(cfx5solve),
            "-def",
            str(def_file),
            "-ccl",
            str(ccl_file),
            "-double",
            "-par-local",
            "-part",
            str(cores),
            "-batch",
        ]
        solve_ret = _run_logged(solve_cmd, work, work / "cfx_solve.log")
        if solve_ret != 0:
            return CfxResult(False, {}, f"CFX-Solve failed with exit code {solve_ret}", "solve")
        new_res = sorted(glob.glob(str(work / "*.res")), key=os.path.getmtime)
        if not new_res:
            return CfxResult(False, {}, "CFX-Solve finished but no .res file was produced", "solve")
        res_file = Path(new_res[-1])

    post_cmd = [str(cfx5post), "-batch", str(template_cse), "-res", str(res_file)]
    post_ret = _run_logged(post_cmd, work, work / "cfx_post.log")
    if post_ret != 0:
        return CfxResult(False, {}, f"CFX-Post failed with exit code {post_ret}", "post")
    if not output_txt.exists():
        return CfxResult(False, {}, "CFX-Post finished but CFX_Results.txt was not produced", "post")
    try:
        return CfxResult(True, _parse_results(output_txt, n_blades), "Success")
    except Exception as exc:
        return CfxResult(False, {}, f"CFX result parsing failed: {exc}", "post")
