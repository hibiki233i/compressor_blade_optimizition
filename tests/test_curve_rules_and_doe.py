"""Geometry rules on the Bezier beta curve, the initial design, and provisional labels."""
from __future__ import annotations

import copy
import importlib.util
import json
import unittest
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

import blade_shape_active_learning as b
import blade_shape_refinement as r
from tests import legacy_box

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'synthetic_meanline.cft-batch'
HAS_SKLEARN = importlib.util.find_spec('sklearn') is not None


def shipped() -> dict:
    config = json.loads(Path('blade_shape_config.json').read_text())
    config['paths']['cft_batch_template'] = str(FIXTURE)
    return config


class CurveRuleTests(unittest.TestCase):
    def setUp(self):
        self.config = shipped()
        self.baseline = b.extract_baseline(self.config)
        self.names = b.variable_names(self.config)

    def test_curve_passes_through_the_end_points_and_damps_inner_offsets(self):
        xs = self.baseline.hub_x
        curve = b.beta_curve(xs, np.array([1., 0., 0., 0., 2.]))
        self.assertAlmostEqual(curve[0], 1.)
        self.assertAlmostEqual(curve[-1], 2.)
        for i, gain in ((1, .4219), (2, .375), (3, .4219)):
            unit = np.zeros(5)
            unit[i] = 1.
            self.assertAlmostEqual(b.beta_curve(xs, unit).max(), gain, places=2)

    def test_the_cap_applies_to_the_curve_not_to_the_control_offsets(self):
        cap = self.config['constraints']['max_beta_offset_deg']
        x = np.zeros(12)
        x[self.names.index('hub_beta_2_deg_offset')] = cap + 5  # moves the curve by 0.375 of that
        self.assertEqual(b.constraint_violations(self.config, self.baseline, x), [])
        wide = copy.deepcopy(self.config)
        for v in wide['variables']:
            v.update(lower=-30., upper=30.)
        together = np.zeros(12)
        for i in range(5):
            together[self.names.index(f'hub_beta_{i}_deg_offset')] = cap + .5  # the whole curve moves by cap + .5
        self.assertIn('hub_beta_offset', b.constraint_violations(wide, self.baseline, together))
        self.assertNotIn('shroud_beta_offset', b.constraint_violations(wide, self.baseline, together))

    def test_the_earlier_box_stays_feasible(self):
        # Every point the earlier rules accepted in the earlier box is still accepted.
        legacy = legacy_box.pin(shipped(), frozen=True)
        rng = np.random.default_rng(3)
        lb, ub = b.lower_bounds(legacy), b.upper_bounds(legacy)
        for _ in range(200):
            x = r.enforce_fixed(legacy, lb + rng.random(12) * (ub - lb))
            self.assertEqual(b.constraint_violations(legacy, self.baseline, x), [])


class DoeTests(unittest.TestCase):
    def setUp(self):
        self.config = shipped()
        self.baseline = b.extract_baseline(self.config)
        self.empty = np.empty((0, 12))

    def test_baseline_first_then_spread_points_inside_the_rules(self):
        points = b.doe_samples(self.config, self.baseline, 8, 42, self.empty)
        self.assertEqual(len(points), 8)
        np.testing.assert_array_equal(points[0], np.zeros(12))
        cap = self.config['doe']['max_shroud_front_raise_deg']
        limit = self.config['constraints']['duplicate_distance_norm']
        for i, x in enumerate(points):
            self.assertEqual(b.constraint_violations(self.config, self.baseline, x), [])
            self.assertLessEqual(b.shroud_front_raise_deg(self.config, self.baseline, x), cap + 1e-9)
            others = np.delete(np.array(points), i, 0)
            self.assertGreaterEqual(r.active_distances(self.config, x[None, :], others)[0], limit)
        self.assertTrue(all(x[self.names().index('shroud_theta_deg_offset')] == 0 for x in points))

    def names(self):
        return b.variable_names(self.config)

    def test_without_a_doe_section_the_earlier_design_is_kept(self):
        self.config.pop('doe')
        existing = self.empty
        earlier = []
        for x in b.lhs_samples(self.config, 6 * 4, 7):
            if len(earlier) >= 6:
                break
            if not b.constraint_violations(self.config, self.baseline, x) and b.is_far_enough(self.config, x, existing):
                earlier.append(x)
                existing = np.vstack([existing, x[None, :]])
        np.testing.assert_array_equal(np.array(b.doe_samples(self.config, self.baseline, 6, 7, self.empty)),
                                      np.array(earlier))

    def test_more_designs_never_crowd_the_points_and_existing_points_count(self):
        def closest(points, anchors):
            pts = np.array(points)
            return min(r.active_distances(self.config, pts[i:i + 1], np.vstack([np.delete(pts, i, 0), anchors]))[0]
                       for i in range(len(pts)))
        anchor = r.enforce_fixed(self.config, np.full(12, .5))[None, :]
        self.config['doe'].update(include_baseline=False, lhs_designs=1)
        one = b.doe_samples(self.config, self.baseline, 6, 11, anchor)
        self.config['doe']['lhs_designs'] = 20
        twenty = b.doe_samples(self.config, self.baseline, 6, 11, anchor)
        self.assertEqual((len(one), len(twenty)), (6, 6))
        self.assertGreaterEqual(closest(twenty, anchor), closest(one, anchor))

    def test_settings_are_checked(self):
        for bad in ({'pool': 3}, {'lhs_designs': 0}, {'lhs_designs': 2.5}, {'include_baseline': 'yes'},
                    {'max_shroud_front_raise_deg': True}, {'max_shroud_front_raise_deg': float('nan')}):
            config = copy.deepcopy(self.config)
            config['doe'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                b.doe_settings(config)
        self.config['doe']['max_shroud_front_raise_deg'] = None
        self.assertIsNone(b.doe_settings(self.config)['max_shroud_front_raise_deg'])


class ProvisionalLabelTests(unittest.TestCase):
    def test_settings_are_checked(self):
        config = shipped()
        for bad in ({'train_on_review': 'yes'}, {'review_noise_tol': -1}, {'review_noise_tol': True}):
            broken = copy.deepcopy(config)
            broken['surrogate'].update(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                r.review_training(broken)
        config['surrogate']['train_on_review'] = False
        self.assertEqual(r.label_statuses(config), ('success',))

    @unittest.skipUnless(HAS_SKLEARN, 'GP surrogate needs scikit-learn')
    def test_a_provisional_row_pulls_the_gp_less_than_an_accepted_one(self):
        config = shipped()
        rng = np.random.default_rng(5)
        lb, ub = b.lower_bounds(config), b.upper_bounds(config)
        x = r.enforce_fixed(config, lb + rng.random((14, 12)) * (ub - lb))
        unit = b.normalize_x(config, x)
        y = np.column_stack([.76 + .01 * unit[:, 0] - .005 * unit[:, 5], 4.2 + .1 * unit[:, 4]])
        y[-1] += [.006, .1]  # an outlier: ten engineering tolerances above the trend
        frame = pd.DataFrame({'status': ['success'] * 13 + ['review']})
        noise = r.provisional_noise(config, frame)
        self.assertEqual(noise[:-1].max(), 0.)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')  # length scales of irrelevant inputs run to their bound
            plain = b.GpKrigingSurrogate(config, seed=0).fit(x, y)
            loose = b.GpKrigingSurrogate(config, seed=0).fit(x, y, noise)
        miss_plain = np.abs(plain.predict(x[-1:])[0] - y[-1])
        miss_loose = np.abs(loose.predict(x[-1:])[0] - y[-1])
        self.assertTrue(np.all(miss_loose > miss_plain), (miss_plain, miss_loose))
        with self.assertRaises(ValueError):
            b.GpKrigingSurrogate(config, seed=0).fit(x, y, noise[:-1])


if __name__ == '__main__':
    unittest.main()
