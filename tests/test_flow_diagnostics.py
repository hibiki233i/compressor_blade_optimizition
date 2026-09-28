from __future__ import annotations

import math
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blade_shape_flow_diagnostics import (
    add_angle_proxies,
    analyze_res,
    main,
    measurement_expressions,
    parse_measurements,
    render_session,
    read_candidate_angles,
    summarize,
)


class FlowDiagnosticsTests(unittest.TestCase):
    def test_proxy_wrap_missing_angles_and_nonfinite_candidate(self) -> None:
        result = {"le_hub_flow_angle_deg": -179.0, "blade_hub_leading_deg": 1.0}
        add_angle_proxies(result)
        self.assertEqual(result["le_hub_incidence_proxy_deg"], -2.0)
        self.assertIsNone(result["te_hub_deviation_proxy_deg"])
        self.assertFalse(result["angle_proxies_available"])
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            candidate = {"geometry": {"hub_beta_rad": [1.0, float("nan")],
                                      "shroud_beta_rad": [1.0, 0.8]}}
            (case / "candidate.json").write_text(json.dumps(candidate))
            with self.assertRaisesRegex(ValueError, "Non-finite hub"):
                read_candidate_angles(case / "test.res")

    def test_post_failure_preserves_evidence_and_resolves_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            res = root / "test.res"
            res.write_text("unchanged result")
            post = root / "cfx5post.exe"
            post.touch()
            logs = root / "logs"
            with patch("blade_shape_flow_diagnostics.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, stdout="expression failed")
                with self.assertRaisesRegex(RuntimeError, "logs retained"):
                    analyze_res(res, post, inlet="R1 Inlet", outlet="R1 Outlet",
                                span_band=0.2, log_dir=logs)
            work = next(logs.iterdir())
            self.assertEqual((work / "cfxpost.log").read_text(), "expression failed")
            self.assertTrue((work / "Extract_Flow_Diagnostics.cse").is_file())
            self.assertTrue((work / "failure.txt").is_file())
            self.assertEqual(json.loads((work / "command.json").read_text())["argv"][0], str(post.resolve()))
            self.assertEqual(res.read_text(), "unchanged result")

    def test_post_error_log_is_rejected_even_with_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            res, post = root / "test.res", root / "post.exe"
            res.touch()
            post.touch()
            def fake_run(command, **kwargs):
                work = kwargs["cwd"]
                (work / "flow_diagnostics.tsv").write_text("__complete__\t1\n")
                (work / "cfdpost_error.log").write_text("Failed to evaluate")
                return subprocess.CompletedProcess(command, 0, stdout="done")
            with patch("blade_shape_flow_diagnostics.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "Failed to evaluate"):
                    analyze_res(res, post, inlet="R1 Inlet", outlet="R1 Outlet",
                                span_band=0.2, log_dir=root / "logs")

    def test_cli_override_without_config_and_refuses_output_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "diagnostics.csv"
            argv = ["diagnostics", "--config", str(root / "missing.json"),
                    "--post-exe", str(root / "post.exe"), "--res", str(root / "test.res"),
                    "--output", str(output)]
            with patch("sys.argv", argv), patch("blade_shape_flow_diagnostics.analyze_res", return_value={"quality_ok": True}) as analyze:
                self.assertEqual(main(), 0)
                self.assertEqual(analyze.call_count, 1)
            before = output.read_bytes()
            with patch("sys.argv", argv), patch("blade_shape_flow_diagnostics.analyze_res") as analyze:
                with self.assertRaises(SystemExit):
                    main()
                analyze.assert_not_called()
            self.assertEqual(output.read_bytes(), before)

    def test_session_uses_explicit_units_turbo_surfaces_and_safe_names(self) -> None:
        expressions = measurement_expressions("R1 Inlet", "R1 Outlet", 0.2)
        session = render_session(expressions)
        self.assertIn(">turbo init", session)
        self.assertIn(">turbo more vars", session)
        self.assertIn("Streamwise Location = 0.220000", session)
        self.assertIn("Streamwise Location = 0.780000", session)
        self.assertIn("massFlowAveAbs(Static Entropy)\\@R1 Inlet / 1 [J kg^-1 K^-1]", session)
        self.assertIn("massFlow()\\@R1 Outlet / 1 [kg s^-1]", session)
        with self.assertRaises(ValueError):
            measurement_expressions('R1"; die "bad', "R1 Outlet", 0.2)
        with self.assertRaises(ValueError):
            render_session(expressions, le_station=0.3)

    def test_post_output_cannot_silently_turn_missing_measurement_into_zero(self) -> None:
        required = {"entropy_in_j_kg_k", "mass_flow_in_kg_s"}
        good = "entropy_in_j_kg_k\t42\nmass_flow_in_kg_s\t0.001\n__complete__\t1\n"
        self.assertEqual(parse_measurements(good, required)["mass_flow_in_kg_s"], 0.001)
        for bad in (
            "entropy_in_j_kg_k\t\nmass_flow_in_kg_s\t0.001\n__complete__\t1\n",
            "entropy_in_j_kg_k\t42\n__complete__\t1\n",
            "entropy_in_j_kg_k\t42\nmass_flow_in_kg_s\t0.001\n",
            "entropy_in_j_kg_k\t42\nentropy_in_j_kg_k\t42\n"
            "mass_flow_in_kg_s\t0.001\n__complete__\t1\n",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_measurements(bad, required)

    def test_entropy_quality_and_circular_angle_average(self) -> None:
        values = {name: 0.0 for name in measurement_expressions("R1 Inlet", "R1 Outlet", 0.2)}
        values.update(
            mass_flow_in_kg_s=0.001,
            mass_flow_out_kg_s=-0.001,
            mass_flow_le_kg_s=-0.001,
            mass_flow_te_kg_s=-0.001,
            entropy_in_j_kg_k=100.0,
            entropy_out_j_kg_k=120.0,
            entropy_le_j_kg_k=102.0,
            entropy_te_j_kg_k=118.0,
            entropy_signed_in_j_kg_k=100.0,
            entropy_signed_out_j_kg_k=120.0,
        )
        for side in ("in", "out", "le", "te"):
            for band in ("hub", "shroud"):
                prefix = f"{side}_{band}"
                values[f"{prefix}_mass_fraction"] = 0.2
                values[f"{prefix}_entropy_weighted"] = 20.0
                # Two wrapped angles around +/-180 should average near 180.
                values[f"{prefix}_angle_sin_weighted"] = 0.0
                values[f"{prefix}_angle_cos_weighted"] = -0.2
        result = summarize(values)
        self.assertTrue(result["quality_ok"])
        self.assertEqual(result["delta_entropy_j_kg_k"], 20.0)
        self.assertEqual(result["delta_entropy_blade_passage_j_kg_k"], 16.0)
        self.assertTrue(math.isclose(result["le_hub_flow_angle_deg"], 180.0))
        result.update(blade_hub_leading_deg=70.0, blade_hub_trailing_deg=45.0)
        add_angle_proxies(result)
        self.assertEqual(result["le_hub_incidence_proxy_deg"], -70.0)
        self.assertEqual(result["te_hub_deviation_proxy_deg"], -45.0)
        values["mass_flow_out_kg_s"] = -0.0008
        self.assertFalse(summarize(values)["quality_ok"])


if __name__ == "__main__":
    unittest.main()
