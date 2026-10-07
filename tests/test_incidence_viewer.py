"""Incidence-diagnosis viewer: Qt-free readers plus offscreen widget checks."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from blade_gui import validation_data as vd  # noqa: E402
from tests import validation_samples as samples  # noqa: E402
from tests.test_gui import PYSIDE_AVAILABLE, ProjectFixture  # noqa: E402


def hub_heavy(span: float) -> float:
    return 3.0 - 8.0 * span + 9.0 * span * span


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_extract_profile_round_trips_cli_numbers(self):
        summary = samples.write_extract(self.root / 'target', samples.make_spec(), hub_heavy)
        result = vd.load_result(self.root / 'target')
        self.assertIsInstance(result, vd.ProfileResult)
        self.assertFalse(result.is_legacy)
        self.assertTrue(result.quality_ok)
        self.assertEqual(len(result.rows), 20)
        self.assertAlmostEqual(result.rms, summary['incidence_rms_mass_deg'])
        self.assertAlmostEqual(result.max_abs, summary['max_abs_incidence_deg'])
        # display span is the CLI's mass-weighted band span, inside each band
        for span, row in zip(result.span(), result.rows):
            self.assertTrue(row['span_low'] <= span <= row['span_high'])
        for span, value in zip(result.span(), result.column('incidence_deg')):
            self.assertAlmostEqual(value, hub_heavy(span), places=6)
        self.assertEqual(result.metal_endpoints(), (34.0, 22.0))
        self.assertAlmostEqual(sum(result.forward_share()), 1.0)
        self.assertEqual(result.total_reverse_fraction(), 0.0)
        # the summary file and profile.csv resolve to the same result
        self.assertIsInstance(vd.load_result(self.root / 'target' / 'summary.json'), vd.ProfileResult)

    def test_quality_failure_keeps_bands_but_no_overall_metric(self):
        samples.write_extract(self.root / 'bad', samples.make_spec(), hub_heavy, reverse=lambda s: .05 if s > .9 else 0.)
        result = vd.load_result(self.root / 'bad')
        self.assertFalse(result.quality_ok)
        self.assertIsNone(result.rms)
        self.assertTrue(any('reverse flux' in issue for issue in result.quality_issues))
        fractions = [value for value in result.reverse_fraction() if value]
        self.assertTrue(fractions and max(fractions) > .01)

    def test_legacy_profile(self):
        samples.write_legacy(self.root / 'legacy')
        result = vd.load_result(self.root / 'legacy')
        self.assertTrue(result.is_legacy)
        self.assertEqual(len(result.span()), 20)
        self.assertEqual(result.metal_endpoints(), (34.0, 22.0))
        self.assertAlmostEqual(result.max_abs, max(abs(v) for v in result.column('incidence_deg')))
        self.assertIsNone(result.rms)

    def test_overlay_requires_same_method_and_measurement(self):
        samples.write_extract(self.root / 'a', samples.make_spec(), hub_heavy)
        samples.write_extract(self.root / 'b', samples.make_spec(target_id='other'), lambda s: hub_heavy(s) + 1.0)
        samples.write_extract(self.root / 'c', samples.make_spec(le_station=.2), hub_heavy)
        samples.write_legacy(self.root / 'legacy')
        a, b, c, legacy = (vd.load_result(self.root / name) for name in ('a', 'b', 'c', 'legacy'))
        self.assertEqual(vd.comparable(b, a), (True, ''))
        for delta in vd.incidence_delta(b, a):
            self.assertAlmostEqual(delta[1], 1.0, places=6)
        self.assertFalse(vd.comparable(c, a)[0])
        self.assertEqual(vd.incidence_delta(c, a), [])
        self.assertIn('方法不同', vd.comparable(legacy, a)[1])
        self.assertAlmostEqual(vd.flow_difference(b, a), 0.0)

    def test_sweep_reads_table_and_each_measurement_profile(self):
        rows = samples.write_sweep(self.root / 'sweep')
        result = vd.load_result(self.root / 'sweep')
        self.assertIsInstance(result, vd.SweepResult)
        self.assertEqual(len(result.rows), len(rows))
        self.assertEqual(len(result.profiles), len(rows))
        self.assertEqual(result.status, 'complete')
        self.assertIn('截面 0.2', result.profiles[0][0])

    def test_comparison_and_sources(self):
        samples.write_extract(self.root / 'a', samples.make_spec(), hub_heavy)
        samples.write_extract(self.root / 'b', samples.make_spec(), lambda s: hub_heavy(s) - .5)
        data = samples.write_compare(self.root / 'comparison.json', self.root / 'a', self.root / 'b')
        result = vd.load_result(self.root / 'comparison.json')
        self.assertIsInstance(result, vd.ComparisonResult)
        self.assertTrue(data['usable_for_matched_point_diagnostic'])
        self.assertEqual([p.parent.name for p in result.source_summaries()], ['a', 'b'])

    def test_plan_slopes_are_central_differences_of_completed_points(self):
        samples.write_plan(self.root / 'plan', step=.25)
        result = vd.load_result(self.root / 'plan')
        self.assertIsInstance(result, vd.PlanResult)
        self.assertAlmostEqual(result.step_deg, .25)
        slopes = {row['variable']: row for row in result.slopes()}
        # synthetic η = .82 + .004 Δhub − .02 Δhub² − .002 Δshroud
        self.assertEqual(slopes['hub']['scheme_Efficiency'], 'central')
        self.assertAlmostEqual(slopes['hub']['dEfficiency_ddeg'], .004)
        self.assertAlmostEqual(slopes['hub']['curvature_Efficiency'], -.04)
        self.assertAlmostEqual(slopes['shroud']['dEfficiency_ddeg'], -.002)
        self.assertAlmostEqual(slopes['hub']['dMassFlow_ddeg'], .05)

    def test_plan_uses_one_sided_or_no_slope_for_missing_points(self):
        samples.write_plan(self.root / 'plan', complete={0, 2}, failed={1})
        result = vd.load_result(self.root / 'plan' / 'plan.json')
        self.assertEqual(result.status, 'failed')
        slopes = {row['variable']: row for row in result.slopes()}
        self.assertEqual(slopes['hub']['scheme_Efficiency'], 'forward')
        self.assertIsNone(slopes['hub']['curvature_Efficiency'])
        self.assertIsNone(slopes['shroud']['dEfficiency_ddeg'])
        self.assertEqual([p.status for p in result.points][:3], ['complete', 'failed', 'complete'])

    def test_spec_and_failed_extraction(self):
        draft = samples.make_spec(geometry_verified=False)
        draft['conditions']['rpm'] = None
        (self.root / 'spec.json').write_text(json.dumps(draft), encoding='utf-8')
        spec = vd.load_result(self.root / 'spec.json')
        self.assertIsInstance(spec, vd.SpecResult)
        self.assertIn('conditions.rpm', spec.pending)
        self.assertTrue(any(item.startswith('geometry_verified') for item in spec.pending))
        failed = self.root / 'failed_extract'
        failed.mkdir()
        (failed / 'state.json').write_text(json.dumps(dict(status='failed', failure_stage='post', message='boom')),
                                           encoding='utf-8')
        result = vd.load_result(failed)
        self.assertIsInstance(result, vd.FailedResult)
        with self.assertRaises(ValueError):
            vd.load_result(self.root / 'nothing_here')

    def test_case_inputs_follow_completion_receipt(self):
        case = self.root / 'case_000007'
        case.mkdir()
        (case / 'candidate.json').write_text('{}', encoding='utf-8')
        (case / 'Impeller_001.res').write_bytes(b'old')
        (case / 'Impeller_002.res').write_bytes(b'new')
        (case / 'cfx_state.json').write_text(json.dumps(
            {'stages': {'solve': {'status': 'complete', 'result_file': 'Impeller_002.res'}}}), encoding='utf-8')
        found = vd.case_validation_inputs(case)
        self.assertEqual(Path(found['res']).name, 'Impeller_002.res')
        self.assertEqual(Path(found['candidate']).name, 'candidate.json')
        self.assertEqual(found['notes'], [])
        (case / 'cfx_state.json').unlink()
        found = vd.case_validation_inputs(case)
        self.assertEqual(found['res'], '')
        self.assertTrue(found['notes'])

    def test_suggested_paths_never_exist(self):
        first = vd.suggest_new_path(self.root, 'extract')
        Path(first).mkdir()
        second = vd.suggest_new_path(self.root, 'extract')
        self.assertNotEqual(first, second)
        self.assertFalse(Path(second).exists())


@unittest.skipUnless(PYSIDE_AVAILABLE, 'PySide6 is not installed')
class ViewerWidgetTests(ProjectFixture):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        from blade_gui.context import AppContext
        from blade_gui.main_window import MainWindow
        self.ctx = AppContext(self.config_path, self.out)
        self.window = MainWindow(self.ctx)
        self.window.resize(1440, 900)
        self.window.show()
        self.window._select_page(5)
        self.page = self.window.pages[5]
        self.view = self.page.view
        self.vroot = self.root / 'validation'
        self._settle()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self._settle()
        super().tearDown()

    def _settle(self, rounds=6):
        for _ in range(rounds):
            self.app.processEvents()

    def test_profile_panel_plots_angles_incidence_and_overlay(self):
        samples.write_extract(self.vroot / 'target', samples.make_spec(target_id='B'), hub_heavy)
        samples.write_extract(self.vroot / 'base', samples.make_spec(target_id='A'), lambda s: hub_heavy(s) - 1)
        self.assertTrue(self.view.load(self.vroot / 'target'))
        self._settle()
        panel = self.view.profile_panel
        self.assertEqual(panel.title.text(), 'B')
        self.assertEqual(panel.table.rowCount(), 20)
        self.assertTrue(panel.incidence_card.chart.isVisible())
        names = [series.name for series in panel.incidence_card.chart._series]
        self.assertIn('i · B', names)
        self.view.set_baseline(self.vroot / 'base')
        self._settle()
        self.assertEqual(panel.extra_card.title_label.text(), 'Δi（目标 − 基准）')
        delta = next(s for s in panel.extra_card.chart._series if s.name == 'Δi')
        for value in delta.ys:
            self.assertAlmostEqual(value, 1.0, places=6)
        self.assertTrue(self.view.clear_button.isEnabled())
        self.assertFalse(self.window.grab().toImage().isNull())

    def test_incompatible_overlay_is_refused(self):
        samples.write_extract(self.vroot / 'target', samples.make_spec(), hub_heavy)
        samples.write_extract(self.vroot / 'other_station', samples.make_spec(le_station=.2), hub_heavy)
        self.view.load(self.vroot / 'target')
        self.view.set_baseline(self.vroot / 'other_station')
        self._settle()
        self.assertIsNone(self.view.baseline)
        self.assertIn('测量定义不同', self.view.profile_panel.message.text.text())

    def test_sweep_plan_compare_and_spec_panels(self):
        samples.write_sweep(self.vroot / 'sweep')
        self.view.load(self.vroot / 'sweep')
        self.assertEqual(self.view.sweep_panel.table.rowCount(), 6)
        samples.write_plan(self.vroot / 'plan')
        self.view.load(self.vroot / 'plan')
        self.assertEqual(self.view.plan_panel.slopes.rowCount(), 2)
        self.assertEqual(self.view.plan_panel.points.rowCount(), 5)
        samples.write_extract(self.vroot / 'a', samples.make_spec(), hub_heavy)
        samples.write_extract(self.vroot / 'b', samples.make_spec(), hub_heavy)
        samples.write_compare(self.vroot / 'cmp.json', self.vroot / 'a', self.vroot / 'b')
        self.view.load(self.vroot / 'cmp.json')
        self._settle()
        self.assertTrue(self.view.kv_panel.action.isVisibleTo(self.view.kv_panel))
        self.view.kv_panel.action.click()
        self._settle()
        self.assertIsInstance(self.view.result, vd.ProfileResult)
        self.assertEqual(self.view.result.path.name, 'b')
        self.assertEqual(self.view.baseline.path.name, 'a')
        (self.vroot / 'spec.json').write_text(json.dumps(samples.make_spec(geometry_verified=False)), encoding='utf-8')
        self.view.load(self.vroot / 'spec.json')
        self.assertIn('待确认', self.view.kv_panel.badge.text())

    def test_unreadable_path_reports_error(self):
        self.assertFalse(self.view.load(self.root / 'missing'))
        self.assertIn('无法读取', self.view.error.text.text())

    def test_finished_extract_loads_output_and_init_fills_from_case(self):
        output = self.vroot / 'extract_1'
        samples.write_extract(output, samples.make_spec(), hub_heavy)
        self.page.action_box.setCurrentIndex(1)
        self.page.forms['extract']['output_dir'].setText(str(output))
        self.assertIn('输出已存在', self.page.guard.text.text())
        self.page._running_action = 'extract'
        self.page.finished(0, 'normal')
        self.assertEqual(self.page.tabs.currentIndex(), 0)
        self.assertIsInstance(self.view.result, vd.ProfileResult)

        case = self.out / 'cases' / 'case_000002'
        (case / 'Impeller_001.res').write_bytes(b'r')
        self.page._sync_cases()
        index = self.page.case_box.findData(str(case))
        self.assertGreaterEqual(index, 0)
        self.page.case_box.setCurrentIndex(index)
        self.page.fill_from_case()
        fields = self.page.forms['init']
        self.assertEqual(Path(fields['res'].text()).name, 'Impeller_001.res')
        self.assertEqual(Path(fields['candidate'].text()).name, 'candidate.json')
        self.assertTrue(fields['output'].text().endswith('spec.json'))
        self.assertIn('人工确认', self.page.guard.text.text())

    def test_browse_output_dir_appends_new_child(self):
        with patch('blade_gui.pages.validation_page.QFileDialog.getExistingDirectory', return_value=str(self.root)):
            widget = self.page.forms['extract']['output_dir']
            self.page.browse(widget, 'output_dir', 'extract')
        chosen = Path(widget.text())
        self.assertEqual(chosen.parent, self.root)
        self.assertTrue(chosen.name.startswith('extract_'))
        self.assertFalse(chosen.exists())


if __name__ == '__main__':
    unittest.main()
