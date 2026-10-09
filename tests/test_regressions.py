"""Regression tests for defects fixed during the GUI/robustness review."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import blade_shape_active_learning as al  # noqa: E402
from blade_gui.project import Project, config_errors, validate_config  # noqa: E402
from tests.test_gui import PYSIDE_AVAILABLE, ProjectFixture, make_config  # noqa: E402


class CliConfigFlagTests(unittest.TestCase):
    def test_config_before_subcommand_is_not_overwritten(self):
        parser = al.build_parser()
        self.assertEqual(parser.parse_args(['--config', 'other.json', 'run']).config, 'other.json')
        self.assertEqual(parser.parse_args(['run', '--config', 'x.json']).config, 'x.json')
        self.assertEqual(parser.parse_args(['diagnose']).config, 'blade_shape_config.json')
        self.assertEqual(parser.parse_args(['--config', 'a.json', 'run-boundary', '--plan', 'p',
                                            '--stage', 'pairs', '--max-new-cfd', '1']).config, 'a.json')

    def test_implicit_run_keeps_top_level_config(self):
        seen = []

        def stop(path):
            seen.append(path)
            raise SystemExit(0)

        with patch.object(al.sys, 'argv', ['prog', '--config', 'other.json']), \
                patch.object(al, 'load_config', side_effect=stop):
            with self.assertRaises(SystemExit):
                al.main()
        self.assertEqual(seen, ['other.json'])


class ParetoAndGeometryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.config = make_config(self.out)

    def tearDown(self):
        self._tmp.cleanup()

    def test_success_row_without_finite_objectives_never_enters_a_front(self):
        frame = pd.DataFrame([
            dict(Efficiency=0.80, MassFlow=3.0, status='success', run_id='case_000000'),
            dict(Efficiency=np.nan, MassFlow=9.9, status='success', run_id='case_000001'),
            dict(Efficiency=0.81, MassFlow=2.9, status='success', run_id='case_000002'),
            dict(Efficiency=0.99, MassFlow=9.9, status='failed', run_id='case_000003'),
        ])
        front = al.write_pareto(self.config, frame)
        strict = pd.read_csv(self.out / 'pareto_front_strict.csv')
        self.assertEqual(sorted(front.run_id), ['case_000000', 'case_000002'])
        self.assertNotIn('case_000001', set(strict.run_id))
        self.assertNotIn('case_000003', set(strict.run_id))

    def test_geometry_script_receives_configured_blade_count(self):
        self.config['runtime']['n_blades'] = 12
        self.config['paths'].update(cfturbo_exe='CFturbo.exe', turbogrid_exe='cfxtg.exe')
        candidate = self.out / 'case' / 'candidate.json'
        candidate.parent.mkdir()
        candidate.write_text('{}', encoding='utf-8')
        with patch.object(al.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(al.run_geometry(self.config, candidate)[0], True)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('-BladeCount') + 1], '12')
        # passed even when it equals the script's own default
        self.config['runtime']['n_blades'] = 10
        with patch.object(al.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            al.run_geometry(self.config, candidate)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('-BladeCount') + 1], '10')

    def test_powershell_script_declares_the_blade_count_parameter(self):
        script = (Path(al.__file__).with_name('Run-BladeShapeGeometryMeshing.ps1')).read_text(encoding='utf-8')
        header = script.split('$ErrorActionPreference', 1)[0]
        self.assertIn('$BladeCount', header)
        self.assertEqual(script.count('-BladeCount $BladeCount'), 2)


class MalformedConfigTests(unittest.TestCase):
    def test_wrong_json_types_are_errors_not_crashes(self):
        for broken in ({'paths': 'nope', 'variables': []},
                       {'variables': ['x', {'name': 'a', 'lower': 0, 'upper': 1}]},
                       {'runtime': [], 'variables': []}):
            with self.subTest(broken=broken):
                self.assertTrue(config_errors(validate_config(broken)))

    def test_project_opens_a_config_with_a_bad_variables_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'paths': {'output_dir': tmp}, 'variables': [1, 2]}), encoding='utf-8')
            project = Project(path)
            self.assertFalse(project.config_valid)
            self.assertEqual(project.output_dir, Path(tmp))


@unittest.skipUnless(PYSIDE_AVAILABLE, 'PySide6 is not installed')
class GuiRegressionTests(ProjectFixture):
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
        self.window.show()
        for _ in range(6):
            self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        super().tearDown()

    def test_pareto_point_click_opens_the_case(self):
        dashboard = self.window.pages[0]
        dashboard.pareto_chart.pointClicked.emit({'series': 'Pareto 前沿', 'label': 'case_000001', 'index': 0})
        self.app.processEvents()
        self.assertEqual(self.window.stack.currentIndex(), 4)
        self.assertEqual(self.window.pages[4]._selected.run_id, 'case_000001')

    def test_gate_card_reads_the_nested_schema_written_by_diagnose(self):
        from blade_gui.pages.dashboard import gate_rows

        gate = {'passed': False, 'minimum_points': 6, 'objectives': {
            'Efficiency': {'n': 7, 'mae': .0002, 'tolerance': .0003, 'coverage_2sigma': .857, 'passed': True},
            'MassFlow': {'n': 0, 'passed': False}}}
        rows = gate_rows(gate)
        self.assertEqual([row[0] for row in rows], ['Efficiency', 'MassFlow'])
        self.assertTrue(rows[0][4])
        self.assertIn('86%', rows[0][3])
        self.assertIsNone(rows[1][1])
        (self.out / 'local_diagnostic_gate.json').write_text(json.dumps(gate), encoding='utf-8')
        self.ctx.reload()
        self.app.processEvents()
        texts = [label.text() for label in self.window.pages[0].gate_card.findChildren(type(self.window.page_title))]
        self.assertTrue(any('MAE' in text for text in texts))

    def test_cards_created_without_hint_can_show_one_later(self):
        from blade_gui.widgets import Card

        card = Card('', '')
        card.set_title('标题')
        card.set_hint('提示')
        card.show()
        self.app.processEvents()
        self.assertTrue(card.title_label.isVisible())
        self.assertTrue(card.hint_label.isVisible())

    def test_step_series_is_not_filled_by_previous_marker_brush(self):
        from PySide6.QtGui import QColor
        from blade_gui.charts import Series, XYChart

        chart = XYChart()
        chart.resize(400, 300)
        chart.set_series([
            Series('markers', QColor('#ff0000'), [0, 1], [0, 0], kind='scatter', marker=3, z=0),
            Series('front', QColor('#00ff00'), [0, 0.5, 1], [0, 1, 0.2], kind='step', marker=0, z=1),
        ])
        image = chart.grab().toImage()
        plot = chart._plot

        def pixel(fx, fy):
            return image.pixelColor(int(plot.left() + plot.width() * fx), int(plot.bottom() - plot.height() * fy)).name()

        # Below the plateau the leaked brush used to fill the closed step path
        # red; it must look like plain background (sampled where nothing is drawn).
        self.assertEqual(pixel(0.75, 0.3), pixel(0.25, 0.85))

    def test_runner_stop_does_not_block_the_event_loop(self):
        import sys
        import time
        from blade_gui.runner import CommandRunner

        runner = CommandRunner(Path.cwd())
        lines = []
        runner.line.connect(lambda _channel, text: lines.append(text))
        # The child ignores SIGTERM (like a Windows console process ignores
        # terminate()) and reports once the handler is installed.
        runner.start([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                                            'print("ready", flush=True); time.sleep(30)'])
        deadline = time.monotonic() + 10
        while 'ready' not in lines and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.02)
        self.assertIn('ready', lines)
        started = time.monotonic()
        runner.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        deadline = time.monotonic() + 10
        while runner.running and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.05)
        self.assertFalse(runner.running)


if __name__ == '__main__':
    unittest.main()
