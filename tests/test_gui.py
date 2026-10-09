"""Tests for the desktop GUI layer.

The command builders and the data layer are pure Python and always run.  The
widget tests need PySide6 and a Qt platform plugin; they run offscreen and are
skipped when PySide6 is not installed, so ``unittest discover`` stays green on
a machine that only uses the command line.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

CODE_DIR = Path(__file__).resolve().parent.parent

from blade_gui import commands  # noqa: E402
from blade_gui.project import (  # noqa: E402
    Issue,
    Project,
    config_errors,
    read_config_file,
    save_config,
    validate_config,
)

try:  # pragma: no cover - exercised only when PySide6 exists
    import PySide6  # noqa: F401

    PYSIDE_AVAILABLE = True
except Exception:  # noqa: BLE001
    PYSIDE_AVAILABLE = False


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
VARIABLES = [
    {"name": "hub_beta_0_deg_offset", "lower": -3.0, "upper": 4.0},
    {"name": "shroud_beta_0_deg_offset", "lower": -3.0, "upper": 3.0},
    {"name": "hub_theta_deg_offset", "lower": -1.5, "upper": 1.5},
]
# The rest of the 12 meanline offsets, held at 0, so the config maps onto the geometry.
FIXED_ZERO = [
    *(f"hub_beta_{i}_deg_offset" for i in range(1, 5)),
    *(f"shroud_beta_{i}_deg_offset" for i in range(1, 5)),
    "shroud_theta_deg_offset",
]
VARIABLES += [{"name": name, "lower": -1.0, "upper": 1.0} for name in FIXED_ZERO]

TRAINING_HEADER = [
    "hub_beta_0_deg_offset", "shroud_beta_0_deg_offset", "hub_theta_deg_offset", *FIXED_ZERO,
    "Efficiency", "PressureRatio", "MassFlow", "Power", "totalpressureratio",
    "sample_phase", "doe_index", "al_iteration", "batch_index", "selection_rank",
    "selection_source", "experiment_id", "design_role", "status", "failure_stage",
    "message", "case_dir", "run_id",
]

TRAINING_ROWS = [
    [0.1, -0.2, 0.0, 0.80, 1.10, 3.00, 100.0, 1.10, "doe", 0, "", "", "", "", "", "", "success", "", "", "case_000000", "case_000000"],
    [0.5, -0.1, 0.1, 0.82, 1.11, 3.20, 101.0, 1.11, "doe", 1, "", "", "", "", "", "", "success", "", "", "case_000001", "case_000001"],
    [0.9, 0.0, 0.2, 0.81, 1.09, 3.40, 99.0, 1.09, "active_learning", "", 0, 1, 1, "ehvi", "", "", "success", "", "", "case_000002", "case_000002"],
    [1.2, 0.3, -0.1, "", "", "", "", "", "active_learning", "", 0, 2, 2, "uncertainty", "", "", "failed", "mesh", "TurboGrid failed", "case_000003", "case_000003"],
]

TRAINING_ROWS = [row[:3] + [0.0] * len(FIXED_ZERO) + row[3:] for row in TRAINING_ROWS]

DIAGNOSTIC_HEADER = [
    "iteration", "run_id", "status", "selection_source", "candidate_role",
    "hv_gain", "local_radius_norm", "pareto_rows_before", "pareto_rows_after",
    "pred_Efficiency", "std_Efficiency", "true_Efficiency", "prediction_error_Efficiency",
    "pred_MassFlow", "std_MassFlow", "true_MassFlow", "prediction_error_MassFlow",
]

DIAGNOSTIC_ROWS = [
    [0, "case_000002", "success", "ehvi", "ehvi", 0.001, 0.15, 2, 2, 0.815, 0.004, 0.81, 0.005, 3.35, 0.02, 3.40, -0.05],
]


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def make_config(output_dir: Path) -> dict:
    return {
        "paths": {
            "output_dir": str(output_dir),
            "powershell_exe": "pwsh.exe",
            "geometry_script_path": "Run-BladeShapeGeometryMeshing.ps1",
            "base_cft": "base.cft",
            "cft_batch_template": "base.cft-batch",
            "turbogrid_template": "BaseMeshing.tst",
            "template_cfx": "BaseModel.cfx",
            "template_cse": "Extract_Results.cse",
        },
        "runtime": {
            "initial_samples": 2, "iterations": 1, "batch_size": 1,
            "max_new_cfd": 2, "seed": 7, "cfx_cores": 4, "n_blades": 10,
        },
        "surrogate": {"model": "gp", "fallback_model": "rbf_ridge_ensemble",
                      "ehvi_y_samples": 256, "ehvi_validation_samples": 1024},
        "pareto": {"use_engineering_tolerance": True,
                   "tolerances": {"Efficiency": 0.0003, "MassFlow": 0.006}},
        "constraints": {
            "max_beta_offset_deg": 6.0, "max_theta_offset_deg": 1.5,
            "max_generated_beta_step_deg": 6.0, "baseline_step_margin_deg": 0.5,
            "duplicate_distance_norm": 0.02,
        },
        "variables": VARIABLES,
        "search": {
            "active_variables": ["hub_beta_0_deg_offset", "hub_theta_deg_offset"],
            "fixed_variables": {"shroud_beta_0_deg_offset": -0.2, **{name: 0.0 for name in FIXED_ZERO}},
            "slice_tolerance_norm": 1e-8,
        },
        "refinement": {
            "challenger_min_samples": 12, "diagnostic_min_points": 6, "diagnostic_window": 18,
            "candidate_roles": ["ehvi", "uncertainty", "diversity"],
            "boundary_variables": ["hub_beta_0_deg_offset"],
            "extension": {"variable": "hub_beta_0_deg_offset", "value": 5.0},
            "local_search": {
                "enabled": True, "fraction": 0.7, "initial_radius_norm": 0.15,
                "min_radius_norm": 0.05, "max_radius_norm": 0.3,
                "successes_to_expand": 3, "failures_to_shrink": 3,
                "min_relative_hv_gain": 0.0001,
            },
        },
    }


class ProjectFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / "runs"
        # GUI settings (remembered inputs, window geometry) never touch the user's real file
        settings = patch.dict(os.environ, {"BLADE_GUI_SETTINGS": str(self.root / "gui_settings.ini")})
        settings.start()
        self.addCleanup(settings.stop)
        (self.out / "cases").mkdir(parents=True)
        for run_id in ("case_000000", "case_000001", "case_000002", "case_000003"):
            (self.out / "cases" / run_id).mkdir()
        (self.out / "cases" / "case_000002" / "candidate.json").write_text(
            json.dumps({"run_id": "case_000002", "x": [0.9, 0.0, 0.2]}), encoding="utf-8"
        )
        (self.out / "cases" / "case_000003" / "stage.log").write_text(
            "TurboGrid failed\n", encoding="utf-8"
        )
        write_csv(self.out / "training_data.csv", TRAINING_HEADER, TRAINING_ROWS)
        write_csv(self.out / "pareto_front.csv", TRAINING_HEADER, TRAINING_ROWS[:3])
        write_csv(self.out / "pareto_front_strict.csv", TRAINING_HEADER, TRAINING_ROWS[:2])
        write_csv(self.out / "active_learning_diagnostics.csv", DIAGNOSTIC_HEADER, DIAGNOSTIC_ROWS)
        (self.out / "local_diagnostic_gate.json").write_text(json.dumps({
            "expected_points": 6, "successful_points": 6, "passed": False,
            "mae_Efficiency": 0.0009, "tolerance_Efficiency": 0.0003,
            "covered_2sigma_Efficiency": 6, "passed_Efficiency": False,
            "mae_MassFlow": 0.011, "tolerance_MassFlow": 0.006,
            "covered_2sigma_MassFlow": 6, "passed_MassFlow": False,
            "first_iteration": 0, "last_iteration": 1,
        }), encoding="utf-8")

        self.config = make_config(self.out)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.config, indent=2), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()


# --------------------------------------------------------------------------
# command builders (no Qt)
# --------------------------------------------------------------------------
class TestCommandBuilders(unittest.TestCase):
    def test_run_command_shape(self) -> None:
        spec = commands.build_run("cfg.json", initial_samples=6, iterations=2,
                                  batch_size=3, max_new_cfd=10, seed=42, resume=True)
        self.assertEqual(spec.name, "run")
        self.assertEqual(spec.args[0], "run")
        self.assertEqual(spec.args[1:3], ["--config", "cfg.json"])
        self.assertIn("--resume", spec.args)
        self.assertEqual(spec.args[spec.args.index("--initial-samples") + 1], "6")
        self.assertEqual(spec.args[spec.args.index("--batch-size") + 1], "3")

    def test_config_flag_follows_subcommand(self) -> None:
        """argparse lets a sub-parser default clobber a pre-subcommand --config."""
        for spec in (
            commands.build_run("c.json"),
            commands.build_write_candidate("c.json"),
            commands.build_diagnose("c.json"),
            commands.build_write_boundary_plan("c.json", center_run_id="r", plan="p.json"),
            commands.build_run_boundary("c.json", plan="p.json", stage="singles", max_new_cfd=1),
        ):
            self.assertEqual(spec.args[0], spec.name)
            self.assertGreater(spec.args.index("--config"), 0, spec.name)

    def test_argv_includes_entry_script(self) -> None:
        spec = commands.build_diagnose("c.json")
        argv = spec.argv("python3")
        self.assertEqual(argv[0], "python3")
        self.assertEqual(argv[1], "-u")
        self.assertEqual(argv[2], commands.ENTRY_SCRIPT)
        self.assertIn(commands.ENTRY_SCRIPT, spec.preview("python3"))

    def test_check_pre_uses_cfx_runner(self) -> None:
        spec = commands.build_check_pre("c.json", working_dir="/tmp/case")
        self.assertEqual(spec.script, commands.CFX_SCRIPT)
        self.assertIn("--working-dir", spec.args)

    def test_boundary_stages(self) -> None:
        self.assertEqual(commands.BOUNDARY_STAGES, ["singles", "pairs", "extension"])
        spec = commands.build_run_boundary("c.json", plan="p.json", stage="pairs", max_new_cfd=6)
        self.assertEqual(spec.args[spec.args.index("--stage") + 1], "pairs")

    def test_candidate_flags(self) -> None:
        spec = commands.build_write_candidate("c.json", index=3, dry_run=True, offline=True)
        self.assertIn("--dry-run", spec.args)
        self.assertIn("--offline", spec.args)


# --------------------------------------------------------------------------
# config validation + persistence (no Qt)
# --------------------------------------------------------------------------
class TestConfigLayer(ProjectFixture):
    def test_valid_config_has_no_errors(self) -> None:
        issues = validate_config(self.config)
        self.assertEqual([i for i in issues if i.level == "error"], [])

    def test_detects_inverted_bounds_and_broken_partition(self) -> None:
        broken = json.loads(json.dumps(self.config))
        broken["variables"][0]["lower"] = 5.0
        broken["variables"][0]["upper"] = 1.0
        broken["search"]["active_variables"] = ["hub_beta_0_deg_offset"]
        broken["search"]["fixed_variables"] = {"shroud_beta_0_deg_offset": -0.2}
        errors = config_errors(validate_config(broken))
        messages = " ".join(item.message for item in errors)
        self.assertIn("lower", messages)
        self.assertIn("搜索空间", messages)

    def test_unmapped_variable_name_is_an_error(self) -> None:
        # A consistent rename passes the search-space check but would never reach CFturbo.
        renamed = json.loads(json.dumps(self.config))
        renamed["variables"][-1]["name"] = "splitter_theta_deg_offset"
        fixed = renamed["search"]["fixed_variables"]
        fixed["splitter_theta_deg_offset"] = fixed.pop("shroud_theta_deg_offset")
        messages = " ".join(item.message for item in config_errors(validate_config(renamed)))
        self.assertIn("几何映射", messages)
        self.assertIn("splitter_theta_deg_offset", messages)
        self.assertNotIn("搜索空间", messages)

    def test_missing_paths_are_warnings_not_errors(self) -> None:
        issues = validate_config(self.config)
        self.assertEqual(config_errors(issues), [])
        self.assertTrue(any(i.level == "warning" for i in issues))

    def test_save_config_round_trips_and_backs_up(self) -> None:
        changed = json.loads(json.dumps(self.config))
        changed["runtime"]["iterations"] = 5
        save_config(self.config_path, changed)
        self.assertEqual(read_config_file(self.config_path)["runtime"]["iterations"], 5)
        backups = list((self.root / "config_backups").glob("*.bak"))
        self.assertEqual(len(backups), 1)
        # private keys are stripped on write
        changed["_config_path"] = "/tmp/x"
        save_config(self.config_path, changed)
        self.assertNotIn("_config_path", json.loads(self.config_path.read_text()))


# --------------------------------------------------------------------------
# data layer (no Qt)
# --------------------------------------------------------------------------
class TestProjectLayer(ProjectFixture):
    def setUp(self) -> None:
        super().setUp()
        self.project = Project(self.config_path)

    def test_summary(self) -> None:
        summary = self.project.summary()
        self.assertEqual(summary.attempts, 4)
        self.assertEqual(summary.successes, 3)
        self.assertEqual(summary.failures, 1)
        self.assertAlmostEqual(summary.best_efficiency, 0.82)
        self.assertAlmostEqual(summary.best_massflow, 3.40)
        self.assertEqual(summary.phases.get("doe"), 2)
        self.assertEqual(summary.phases.get("active_learning"), 2)
        self.assertEqual(summary.failure_stages.get("mesh"), 1)
        self.assertEqual(summary.pareto_count, 3)
        self.assertEqual(summary.strict_count, 2)

    def test_training_is_normalized_and_cached(self) -> None:
        first = self.project.training()
        second = self.project.training()
        self.assertEqual(len(first), 4)
        self.assertIn("status", first.columns)
        # a copy is handed out so callers cannot poison the cache
        first.loc[0, "status"] = "mutated"
        self.assertEqual(self.project.training().loc[0, "status"], "success")
        self.assertIsNot(first, second)

    def test_best_so_far_is_monotonic(self) -> None:
        xs, eff, flow = self.project.best_so_far()
        self.assertEqual(len(xs), 3)
        self.assertEqual(eff, sorted(eff))
        self.assertEqual(flow, sorted(flow))

    def test_gate_and_missing_artifacts(self) -> None:
        gate = self.project.gate()
        self.assertIsNotNone(gate)
        self.assertFalse(gate["passed"])
        self.assertIsNone(self.project.json_artifact("does_not_exist.json"))
        self.assertEqual(self.project.pending(), {})

    def test_cases_join_training_rows(self) -> None:
        records = {item.run_id: item for item in self.project.cases()}
        self.assertEqual(len(records), 4)
        self.assertTrue(records["case_000002"].ok)
        self.assertEqual(records["case_000002"].failure_stage, "")
        self.assertFalse(records["case_000003"].ok)
        self.assertEqual(records["case_000003"].failure_stage, "mesh")
        self.assertEqual(records["case_000002"].files, 1)
        self.assertEqual(records["case_000003"].files, 1)

    def test_case_files_and_log_tail(self) -> None:
        from blade_gui.project import tail_text

        record = next(item for item in self.project.cases() if item.run_id == "case_000003")
        files = self.project.case_files(record)
        self.assertEqual([item["name"] for item in files], ["stage.log"])
        self.assertIn("TurboGrid failed", tail_text(files[0]["path"]))
        self.assertEqual(tail_text(self.root / "missing.log"), "")

    def test_empty_output_dir_is_safe(self) -> None:
        empty = Project(self.config_path, self.root / "nothing_here")
        self.assertEqual(empty.summary().attempts, 0)
        self.assertEqual(empty.cases(), [])
        self.assertTrue(empty.pareto().empty)
        self.assertEqual(empty.best_so_far(), ([], [], []))

    def test_unreadable_config_reports_error(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        project = Project(bad)
        self.assertFalse(project.config_valid)
        self.assertTrue(project.load_error)
        self.assertIsInstance(project.issues[0], Issue)

    def test_validation_does_not_raise_on_corrupt_config(self) -> None:
        issues = validate_config({"paths": {}, "variables": "nonsense"})
        self.assertTrue(config_errors(issues))


# --------------------------------------------------------------------------
# widget construction (needs PySide6)
# --------------------------------------------------------------------------
@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 is not installed")
class TestGuiWidgets(ProjectFixture):
    app = None

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        super().setUp()
        from blade_gui.context import AppContext
        from blade_gui.main_window import MainWindow

        self.ctx = AppContext(self.config_path, self.out)
        self.window = MainWindow(self.ctx)
        self.window.resize(1400, 880)
        self.window.show()
        self._settle()

    def tearDown(self) -> None:
        self.window.close()
        self.window.deleteLater()
        self._settle()
        super().tearDown()

    def _settle(self, rounds: int = 6) -> None:
        for _ in range(rounds):
            self.app.processEvents()

    def test_all_pages_construct_and_paint(self) -> None:
        from blade_gui.pages import PAGE_CLASSES

        self.assertEqual(len(self.window.pages), len(PAGE_CLASSES))
        for index in range(len(PAGE_CLASSES)):
            self.window._select_page(index)
            self._settle()
            image = self.window.grab().toImage()
            self.assertFalse(image.isNull())
            self.assertGreater(image.width(), 600)
            page = self.window.pages[index]
            self.assertGreater(page.width(), 600)

    def test_dashboard_renders_data(self) -> None:
        self.window._select_page(0)
        self._settle()
        page = self.window.pages[0]
        self.assertTrue(page.pareto_chart.isVisible())
        self.assertFalse(page.pareto_empty.isVisible())
        self.assertGreaterEqual(len(page.pareto_chart._series), 1)
        self.assertTrue(page.conv_chart.isVisible())
        self.assertEqual(page.tile_attempts.value.text(), "4")
        self.assertEqual(page.tile_pareto.value.text(), "3")
        self.assertEqual(page.phase_chart._items[0][0], "初始 DOE")
        self.assertGreater(page.gate_body.count(), 0)

    def test_analytics_parity_axes_are_unified(self) -> None:
        self.window._select_page(1)
        self._settle()
        page = self.window.pages[1]
        parity = page.parity_cards["Efficiency"].chart
        self.assertTrue(parity.diagonal)
        (x_lo, x_hi), (y_lo, y_hi), _, _ = parity._auto_limits()
        self.assertAlmostEqual(x_lo, y_lo)
        self.assertAlmostEqual(x_hi, y_hi)
        self.assertEqual(len(page.corr_card.chart._items), 3)

    def test_config_form_round_trips(self) -> None:
        self.window._select_page(2)
        self._settle()
        page = self.window.pages[2]
        self.assertEqual(page.variable_table.rowCount(), len(VARIABLES))
        collected = page._collect()
        self.assertEqual(collected["runtime"]["iterations"], 1)
        self.assertEqual(collected["variables"][0]["upper"], 4.0)
        self.assertEqual(collected["search"]["fixed_variables"],
                         {"shroud_beta_0_deg_offset": -0.2, **{name: 0.0 for name in FIXED_ZERO}})
        self.assertEqual(collected["refinement"]["local_search"]["fraction"], 0.7)
        self.assertEqual([item.level for item in validate_config(collected) if item.level == "error"], [])

    def test_config_edit_marks_dirty_and_saves(self) -> None:
        from PySide6.QtWidgets import QDoubleSpinBox

        self.window._select_page(2)
        self._settle()
        page = self.window.pages[2]
        self.assertFalse(page.dirty_badge.isVisible())
        spin = page.variable_table.cellWidget(0, 1)
        self.assertIsInstance(spin, QDoubleSpinBox)
        spin.setValue(-2.5)
        self._settle()
        self.assertTrue(page.dirty_badge.isVisible())
        page.save()
        self._settle()
        self.assertFalse(page.dirty_badge.isVisible())
        self.assertEqual(read_config_file(self.config_path)["variables"][0]["lower"], -2.5)

    def test_switch_config_discard_prompt_keeps_files_separate(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        other_path = self.root / "other.json"
        other_config = make_config(self.root / "other_runs")
        other_config["runtime"]["iterations"] = 17
        other_path.write_text(json.dumps(other_config), encoding="utf-8")

        page = self.window.pages[2]
        page.variable_table.cellWidget(0, 1).setValue(-2.5)
        self.assertTrue(page._dirty)
        with patch("blade_gui.main_window.QFileDialog.getOpenFileName", return_value=(str(other_path), "")), \
             patch("blade_gui.main_window.QMessageBox.question", return_value=QMessageBox.No) as ask:
            self.window._choose_config()
        ask.assert_called_once()
        self.assertEqual(self.ctx.project.config_path, self.config_path)
        self.assertTrue(page._dirty)

        with patch("blade_gui.main_window.QFileDialog.getOpenFileName", return_value=(str(other_path), "")), \
             patch("blade_gui.main_window.QMessageBox.question", return_value=QMessageBox.Yes):
            self.window._choose_config()
        self._settle()
        self.assertEqual(self.ctx.project.config_path, other_path)
        self.assertFalse(page._dirty)
        self.assertEqual(page._bindings["runtime.iterations"][1].value(), 17)
        self.assertEqual(page.variable_table.cellWidget(0, 1).value(), -3.0)
        self.assertEqual(read_config_file(other_path)["runtime"]["iterations"], 17)
        self.assertEqual(read_config_file(self.config_path)["variables"][0]["lower"], -3.0)

    def test_direct_config_switch_reloads_dirty_form_before_save(self) -> None:
        other_path = self.root / "other.json"
        other_config = make_config(self.root / "other_runs")
        other_config["runtime"]["iterations"] = 17
        other_path.write_text(json.dumps(other_config), encoding="utf-8")
        page = self.window.pages[2]
        page.variable_table.cellWidget(0, 1).setValue(-2.5)
        self.ctx.set_config_path(other_path)
        self._settle()
        self.assertFalse(page._dirty)
        self.assertEqual(page._bindings["runtime.iterations"][1].value(), 17)
        page.save()
        self.assertEqual(read_config_file(other_path)["runtime"]["iterations"], 17)
        self.assertEqual(read_config_file(other_path)["variables"][0]["lower"], -3.0)

    def test_run_controls_use_configured_output_with_data_override(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        configured = self.root / "configured_empty"
        configured.mkdir()
        edited = read_config_file(self.config_path)
        edited["paths"]["output_dir"] = str(configured)
        self.config_path.write_text(json.dumps(edited), encoding="utf-8")
        self.ctx.reload()

        page = self.window.pages[3]
        self.assertEqual(len(self.ctx.project.training()), 4)
        self.assertEqual(page.plan_center.count(), 0)
        self.assertNotIn("已有 4 条", page.guard.text.text())
        with patch("blade_gui.pages.run_page.QDesktopServices.openUrl") as open_url:
            page._open_output()
        self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()), configured)
        with patch("blade_gui.pages.run_page.QMessageBox.question", return_value=QMessageBox.No) as ask:
            page.start()
        self.assertNotIn("已有训练数据", ask.call_args.args[2])

        edited["paths"]["output_dir"] = str(self.out)
        self.config_path.write_text(json.dumps(edited), encoding="utf-8")
        self.ctx.set_data_dir(configured)
        self.assertEqual(len(self.ctx.project.training()), 0)
        self.assertEqual(page.plan_center.count(), 3)
        self.assertIn("已有 4 条", page.guard.text.text())
        with patch("blade_gui.pages.run_page.QDesktopServices.openUrl") as open_url:
            page._open_output()
        self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()), self.out)
        with patch("blade_gui.pages.run_page.QMessageBox.question", return_value=QMessageBox.No) as ask:
            page.start()
        self.assertIn("已有训练数据", ask.call_args.args[2])

    def test_run_guard_detects_pending_in_configured_output(self) -> None:
        configured = self.root / "configured_pending"
        configured.mkdir()
        (configured / "pending_evaluations.json").write_text(
            json.dumps({"version": 1, "entries": [{"run_id": "case_000004"}]}), encoding="utf-8"
        )
        edited = read_config_file(self.config_path)
        edited["paths"]["output_dir"] = str(configured)
        self.config_path.write_text(json.dumps(edited), encoding="utf-8")
        self.ctx.reload()
        page = self.window.pages[3]
        self.assertIn("1 个待处理算例", page.guard.text.text())

    def test_run_page_builds_every_action(self) -> None:
        self.window._select_page(3)
        self._settle()
        page = self.window.pages[3]
        self.assertEqual(page.stack.count(), 5)
        for index in range(5):
            page.action_box.setCurrentIndex(index)
            self._settle(2)
            spec = page._build_spec()
            self.assertEqual(spec.args[0], page.action_box.currentData())
            self.assertIn("blade_shape_active_learning.py", page.preview.toPlainText())
        self.assertEqual(page.run_max.value(), 2)
        self.assertEqual(page.plan_center.count(), 3)

    def test_case_browser_selects_and_explains_missing_dir(self) -> None:
        self.window._select_page(4)
        self._settle()
        page = self.window.pages[4]
        self.assertEqual(page.table.rowCount(), 4)
        page.select_run("case_000003")
        self._settle()
        self.assertIsNotNone(page._selected)
        self.assertEqual(page._selected.run_id, "case_000003")
        self.assertIn("TurboGrid failed", page.detail_message.text())
        self.assertIn("TurboGrid failed", page.log_view.toPlainText())
        page.search.setText("no-such-case")
        self._settle(2)
        self.assertEqual(page.table.rowCount(), 0)
        self.assertTrue(page.table_empty.isVisible())

    def test_command_runner_reports_missing_program(self) -> None:
        from blade_gui.runner import CommandRunner

        runner = CommandRunner(CODE_DIR)
        seen: list[str] = []
        runner.failed.connect(seen.append)
        self.assertTrue(runner.start(["/nonexistent/interpreter", "-c", "pass"]))
        for _ in range(40):
            self.app.processEvents()
            if seen:
                break
        self.assertTrue(seen, "expected a FailedToStart signal")
        self.assertFalse(runner.running)

    def test_clear_layout_removes_nested_layouts(self) -> None:
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

        from blade_gui.widgets import clear_layout

        host = QWidget()
        outer = QVBoxLayout(host)
        inner = QHBoxLayout()
        inner.addWidget(QLabel("a"))
        nested = QVBoxLayout()
        nested.addWidget(QLabel("b"))
        inner.addLayout(nested)
        outer.addLayout(inner)
        outer.addWidget(QLabel("c"))
        self.assertEqual(outer.count(), 2)
        clear_layout(outer)
        self.assertEqual(outer.count(), 0)
        self.assertEqual(inner.count(), 0)
        self.assertEqual(nested.count(), 0)

    def test_repeated_refresh_does_not_accumulate_widgets(self) -> None:
        """Auto-refresh rebuilds the gate and environment cards; they must replace.

        Counting layout items is not enough: the original defect removed the
        layout item while its nested layout kept the old widgets alive and
        visible. Descendant widget counts expose that leak.
        """
        from PySide6.QtWidgets import QWidget

        def descendants(widget) -> int:
            return len(widget.findChildren(QWidget))

        self.window._select_page(0)
        self._settle()
        dashboard = self.window.pages[0]
        gate_baseline = descendants(dashboard.gate_card)
        gate_items = dashboard.gate_body.count()
        self.assertGreater(gate_baseline, 3)

        self.window._select_page(3)
        self._settle()
        run_page = self.window.pages[3]
        env_baseline = descendants(run_page.env_body.parentWidget() or run_page)
        env_items = run_page.env_body.count()
        self.assertGreater(env_baseline, 4)

        for _ in range(4):
            self.ctx.reload()
            self._settle(4)

        self.assertEqual(descendants(dashboard.gate_card), gate_baseline)
        self.assertEqual(descendants(run_page.env_body.parentWidget() or run_page), env_baseline)
        self.assertEqual(dashboard.gate_body.count(), gate_items)
        self.assertEqual(run_page.env_body.count(), env_items)
        self.assertEqual(run_page.plan_center.count(), 3)
        self.assertIs(run_page.ctx.project, self.ctx.project)

    def test_config_page_explains_every_advanced_field(self) -> None:
        from blade_gui.field_help import HELP
        from blade_gui.pages.config_page import ConfigPage, SECTIONS

        page = next(p for p in self.window.pages if isinstance(p, ConfigPage))
        undocumented = [spec.key for section in SECTIONS for spec in section.fields
                        if spec.kind != "path" and spec.key not in HELP]
        self.assertEqual(undocumented, [])
        row = page._rows["constraints.duplicate_distance_norm"]
        self.assertIn("归一化", row.label.toolTip())
        self.assertTrue(row.hint.text().startswith("单变量 ≈ "))
        before = row.hint.text()
        page._bindings["constraints.duplicate_distance_norm"][1].setValue(0.05)
        self.assertNotEqual(row.hint.text(), before)
        self.assertTrue(page._dirty)


if __name__ == "__main__":
    unittest.main()
