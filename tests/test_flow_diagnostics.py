from __future__ import annotations

import math
import unittest

from blade_shape_flow_diagnostics import (
    add_angle_proxies,
    measurement_expressions,
    parse_measurements,
    render_session,
    summarize,
)


class FlowDiagnosticsTests(unittest.TestCase):
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
