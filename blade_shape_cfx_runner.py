from __future__ import annotations

import argparse
import glob
import json
import math
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from blade_shape_runtime import atomic_json, digest, file_identity, output_lock


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class CfxResult:
    success: bool
    metrics: dict[str, float]
    message: str
    failure_stage: str = ""


def _as_posix(path: str | Path) -> str:
    return str(path).replace("\\", "/")


def _cfx_path(path: str | Path) -> str:
    return _as_posix(path)


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
    metrics = {
        "Efficiency": float(values[0]),
        "PressureRatio": float(values[1]),
        "Power": float(values[2]) * n_blades,
        "MassFlow": float(values[3]) * n_blades,
        "totalpressureratio": float(values[4]),
    }
    if not all(math.isfinite(v) for v in metrics.values()):
        raise ValueError("CFX-Post returned non-finite values.")
    return metrics


def write_cfx_pre_inputs(
    working_dir: str | Path,
    *,
    p_out_pa: float,
    template_cfx: str | Path,
) -> dict[str, Path]:
    work = Path(working_dir)
    work.mkdir(parents=True, exist_ok=True)
    gtm_file = work / "Impeller_Mesh.gtm"
    def_file = work / "Impeller.def"
    pre_script = work / "Update_Mesh.pre"
    ccl_file = work / "update_bc.ccl"

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

    pre_script.write_text(
        f"""COMMAND FILE:
  CFX Pre Version = 25.1
END
>load filename={_cfx_path(template_cfx)}
>update
>gtmImport filename={_cfx_path(gtm_file)}, type=GTM, \
units=m, nameStrategy=Assembly
>update
>writeCaseFile filename={_cfx_path(def_file)}, operation=\
write def file
>update
>quit
""",
        encoding="utf-8",
    )
    return {
        "gtm_file": gtm_file,
        "def_file": def_file,
        "pre_script": pre_script,
        "ccl_file": ccl_file,
    }


def run_cfx_pipeline(
    working_dir: str | Path, run_id: str, *, p_out_pa: float,
    cfx_bin_dir: str | Path, template_cfx: str | Path, template_cse: str | Path,
    cores: int = 8, n_blades: int = 10,
) -> CfxResult:
    with output_lock(working_dir):
        return _run_cfx_pipeline(working_dir, run_id, p_out_pa=p_out_pa,
                                 cfx_bin_dir=cfx_bin_dir, template_cfx=template_cfx,
                                 template_cse=template_cse, cores=cores, n_blades=n_blades)


def _run_cfx_pipeline(
    working_dir: str | Path, run_id: str, *, p_out_pa: float,
    cfx_bin_dir: str | Path, template_cfx: str | Path, template_cse: str | Path,
    cores: int, n_blades: int,
) -> CfxResult:
    """Accept only results linked to a recorded, successful Solve and matching inputs.

    Process success is recorded here; it is not a residual/conservation certificate.
    Failed/running Solve receipts require inspection, not an automatic second solve.
    """
    work = Path(working_dir)
    mesh, definition = work / 'Impeller_Mesh.gtm', work / 'Impeller.def'
    output, receipt = work / 'CFX_Results.txt', work / 'cfx_state.json'
    mesh_id = file_identity(mesh)
    if not mesh_id:
        return CfxResult(False, {}, f'Mesh missing: {mesh}', 'mesh')
    inputs = {'run_id': run_id, 'mesh': mesh_id,
              'candidate': file_identity(work / 'candidate.json'),
              'template_cfx': file_identity(template_cfx), 'template_cse': file_identity(template_cse),
              'p_out_pa': p_out_pa, 'cores': cores, 'n_blades': n_blades,
              'cfx_bin_dir': str(cfx_bin_dir)}
    if inputs['template_cfx'] is None or inputs['template_cse'] is None:
        return CfxResult(False, {}, 'CFX templates are missing; cannot verify result identity.', 'environment')
    signature = digest(inputs)
    state = json.loads(receipt.read_text()) if receipt.exists() else None
    if state is not None and (state.get('version') != 1 or state.get('signature') != signature):
        return CfxResult(False, {}, 'CFX input identity changed; existing results were not reused.', 'environment')
    if state is None:
        if output.exists() or list(work.glob('*.res')) or definition.exists():
            return CfxResult(False, {}, 'Unverified legacy CFX artifacts: no completion receipt. Inspect/archive or use a fresh case.', 'solve')
        state = {'version': 1, 'signature': signature, 'inputs': inputs, 'stages': {}}
        atomic_json(receipt, state)
    stages = state['stages']
    solve = stages.get('solve', {})
    if solve.get('status') in {'running', 'failed'}:
        return CfxResult(False, {}, 'Previous Solve is incomplete or failed; its .res is not an accepted final result. Inspect the case before a new attempt.', 'solve')
    if solve.get('status') == 'complete':
        res_file = work / solve['result_file']
        if solve.get('exit_code') != 0 or file_identity(res_file) != solve.get('result_identity'):
            return CfxResult(False, {}, 'Completed Solve result is missing or changed.', 'solve')
        post = stages.get('post', {})
        if post.get('status') == 'complete' and file_identity(output) == post.get('result_identity'):
            try:
                return CfxResult(True, _parse_results(output, n_blades), 'Recovered verified completed CFD result')
            except (ValueError, OSError) as exc:
                return CfxResult(False, {}, f'Cannot parse verified post result: {exc}', 'post')
    else:
        res_file = None

    binary = Path(cfx_bin_dir)
    executables = {name: binary / f'cfx5{name}.exe' for name in ['pre', 'solve', 'post']}
    needed = ['post'] if res_file is not None else ['pre', 'solve', 'post']
    for name in needed:
        if not executables[name].is_file():
            return CfxResult(False, {}, f'Missing CFX executable: {executables[name]}', 'environment')

    def invoke(stage: str, command: list[str]) -> int:
        stages[stage] = {'status': 'running', 'command': command}
        atomic_json(receipt, state)
        try:
            code = _run_logged(command, work, work / f'cfx_{stage}.log')
        except OSError as exc:
            stages[stage].update(status='failed', message=str(exc))
            atomic_json(receipt, state)
            raise
        stages[stage].update(status='returned' if code == 0 else 'failed', exit_code=code)
        atomic_json(receipt, state)
        return code

    if res_file is None:
        pre = stages.get('pre', {})
        if pre.get('status') in {'running', 'failed', 'returned'}:
            return CfxResult(False, {}, 'Previous Pre was not recorded as complete; inspect the case.', 'pre')
        if pre.get('status') != 'complete':
            paths = write_cfx_pre_inputs(work, p_out_pa=p_out_pa, template_cfx=template_cfx)
            ret = invoke('pre', [str(executables['pre']), '-batch', str(paths['pre_script'])])
            if ret != 0 or not definition.is_file():
                stages['pre']['status'] = 'failed'; atomic_json(receipt, state)
                return CfxResult(False, {}, f'CFX-Pre failed, exit={ret}', 'pre')
            stages['pre'].update(status='complete', definition_identity=file_identity(definition), ccl_identity=file_identity(work/'update_bc.ccl'))
            atomic_json(receipt, state)
        elif file_identity(definition) != pre.get('definition_identity') or file_identity(work/'update_bc.ccl') != pre.get('ccl_identity'):
            return CfxResult(False, {}, 'Recorded .def/CCL is missing or changed.', 'pre')
        if list(work.glob('*.res')):
            return CfxResult(False, {}, 'Unexpected .res without a completed Solve receipt.', 'solve')
        ret = invoke('solve', [str(executables['solve']), '-def', str(definition), '-ccl', str(work/'update_bc.ccl'),
                               '-double', '-par-local', '-part', str(cores), '-batch'])
        files = sorted(work.glob('*.res'), key=lambda p: p.stat().st_mtime_ns)
        if ret != 0 or not files or files[-1].stat().st_size == 0:
            stages['solve']['status'] = 'failed'; atomic_json(receipt, state)
            return CfxResult(False, {}, f'CFX-Solve failed or produced no usable result, exit={ret}', 'solve')
        res_file = files[-1]
        stages['solve'].update(status='complete', result_file=res_file.name, result_identity=file_identity(res_file))
        atomic_json(receipt, state)
    if output.exists():
        # Preserve an uncommitted/failed Post output, never accept it as the new output.
        output.rename(work / f'CFX_Results.unverified_{time.time_ns()}.txt')
    ret = invoke('post', [str(executables['post']), '-batch', str(template_cse), '-res', str(res_file)])
    if ret != 0 or not output.is_file():
        stages['post']['status'] = 'failed'; atomic_json(receipt, state)
        return CfxResult(False, {}, f'CFX-Post failed, exit={ret}', 'post')
    try:
        metrics = _parse_results(output, n_blades)
    except (ValueError, OSError) as exc:
        stages['post'].update(status='failed', message=str(exc)); atomic_json(receipt, state)
        return CfxResult(False, {}, f'CFX result parsing failed: {exc}', 'post')
    stages['post'].update(status='complete', result_identity=file_identity(output))
    atomic_json(receipt, state)
    return CfxResult(True, metrics, 'Success (recorded Pre/Solve/Post completion)')


def _load_config(path: str | Path) -> dict[str, object]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check_cfx_pre_inputs(config_path: str | Path, working_dir: str | Path) -> int:
    with output_lock(working_dir):
        return _check_cfx_pre_inputs(config_path,working_dir)


def _check_cfx_pre_inputs(config_path: str | Path, working_dir: str | Path) -> int:
    config = _load_config(config_path)
    paths = config["paths"]
    runtime = config["runtime"]
    work = Path(working_dir)
    generated = write_cfx_pre_inputs(
        work,
        p_out_pa=float(runtime["p_out_pa"]),
        template_cfx=paths["template_cfx"],
    )
    cfx_bin = Path(paths["cfx_bin_dir"])
    required = [
        Path(paths["template_cfx"]),
        Path(paths["template_cse"]),
        cfx_bin / "cfx5pre.exe",
        cfx_bin / "cfx5solve.exe",
        cfx_bin / "cfx5post.exe",
    ]
    missing = [str(item) for item in required if not item.exists()]
    text = generated["pre_script"].read_text(encoding="utf-8", errors="ignore")
    stale_tokens = [
        "F:/optimazition",
        "F:\\optimazition",
        "compressor blade optimazition",
        "{",
        "}",
    ]
    stale = [token for token in stale_tokens if token in text]
    print(f"Update_Mesh.pre: {generated['pre_script']}")
    print(f"update_bc.ccl: {generated['ccl_file']}")
    print(f"Expected mesh: {generated['gtm_file']} ({'exists' if generated['gtm_file'].exists() else 'missing'})")
    print(f"Expected def: {generated['def_file']}")
    if missing:
        print("Missing required files/executables:")
        for item in missing:
            print(f"  - {item}")
    if stale:
        print("Stale tokens found in generated CFX-Pre file:")
        for token in stale:
            print(f"  - {token}")
    return 1 if missing or stale else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="CFX runner utilities for blade-shape active learning.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-pre", help="Generate and check CFX-Pre input files without running CFX.")
    check.add_argument("--config", default="blade_shape_config.json")
    check.add_argument("--working-dir", required=True)
    args = parser.parse_args()
    if args.command == "check-pre":
        raise SystemExit(check_cfx_pre_inputs(args.config, args.working_dir))


if __name__ == "__main__":
    main()
