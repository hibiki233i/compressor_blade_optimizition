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
from blade_shape_signature import physical_signature  # noqa: E402
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
        ]).assign(physical_signature=physical_signature(self.config))
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

    def test_result_awaiting_review_is_counted_filtered_and_opens_the_review_action(self):
        path = self.out / 'training_data.csv'
        frame = pd.read_csv(path)
        frame.loc[frame.run_id.eq('case_000002'), 'status'] = 'review'
        frame.to_csv(path, index=False)
        self.ctx.reload()
        self.app.processEvents()
        summary = self.ctx.project.summary()
        self.assertEqual((summary.successes, summary.reviews, summary.failures), (2, 1, 1))
        self.assertEqual(summary.failure_stages, {'mesh': 1})

        cases = next(page for page in self.window.pages if hasattr(page, 'select_run'))
        self.window._select_page(self.window.pages.index(cases))
        cases.status_filter.setCurrentIndex(cases.status_filter.findData('review'))
        self.app.processEvents()
        self.assertEqual(cases.table.rowCount(), 1)
        cases.select_run('case_000002')
        self.app.processEvents()
        self.assertFalse(cases.review_button.isHidden())
        cases.review_button.click()
        self.app.processEvents()

        run = next(page for page in self.window.pages if hasattr(page, 'request_review'))
        self.assertIs(self.window.stack.currentWidget(), run)
        self.assertEqual(run.action_box.currentData(), 'review')
        self.assertEqual(run.review_run.currentText(), 'case_000002')
        run.review_decision.setCurrentIndex(run.review_decision.findData('accept'))
        run.review_reason.setText('P-Mass flat at 1.3e-5')
        args = run._build_spec().args
        self.assertEqual(args[0], 'review')
        self.assertEqual(args[-4:], ['--run-id', 'case_000002', '--accept', 'P-Mass flat at 1.3e-5'])
        # a case that is not awaiting review in the configured output dir clears the choice
        run.request_review('case_999999')
        self.assertEqual(run.review_run.currentIndex(), -1)
        with self.assertRaisesRegex(ValueError, '请选择待审核的算例'):
            run._build_spec()

    def test_a_case_seen_in_another_data_dir_cannot_be_reviewed_in_the_configured_one(self):
        import shutil
        # both directories hold a case_000002 awaiting review: IDs repeat across outputs
        other = self.root / 'other_runs'
        shutil.copytree(self.out, other)
        for directory in (self.out, other):
            frame = pd.read_csv(directory / 'training_data.csv')
            frame.loc[frame.run_id.eq('case_000002'), 'status'] = 'review'
            frame.to_csv(directory / 'training_data.csv', index=False)
        self.ctx.set_data_dir(other)
        self.app.processEvents()
        self.assertFalse(self.ctx.project.views_configured_output)
        cases = next(page for page in self.window.pages if hasattr(page, 'select_run'))
        cases.select_run('case_000002')
        self.app.processEvents()
        self.assertFalse(cases.review_button.isHidden())
        self.assertFalse(cases.review_button.isEnabled())

        run = next(page for page in self.window.pages if hasattr(page, 'request_review'))
        run.request_review('case_000002', str(other))
        self.assertGreaterEqual(run.review_run.findText('case_000002'), 0)  # the configured one exists...
        self.assertEqual(run.review_run.currentIndex(), -1)                  # ...but is not chosen

        self.ctx.set_data_dir(None)
        self.app.processEvents()
        cases.select_run('case_000002')
        self.app.processEvents()
        self.assertTrue(cases.review_button.isEnabled())
        run.request_review('case_000002', str(self.out))
        self.assertEqual(run.review_run.currentText(), 'case_000002')

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

    def _start_with_grandchild(self, runner):
        """Child and grandchild both ignore SIGTERM, like console programs on Windows."""
        import sys
        import time
        lines = []
        runner.line.connect(lambda _channel, text: lines.append(text))
        runner.start([sys.executable, '-c',
                      'import signal,subprocess,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                      'g = subprocess.Popen([sys.executable, "-c", "import signal,time; '
                      'signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"]); '
                      'print("grandchild", g.pid, flush=True); time.sleep(60)'])
        deadline = time.monotonic() + 10
        while not any(line.startswith('grandchild') for line in lines) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.02)
        return int(next(line for line in lines if line.startswith('grandchild')).split()[1])

    @staticmethod
    def _alive(pid):
        import sys
        if sys.platform == 'win32':  # os.kill(pid, 0) would terminate it on Windows
            listing = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'], capture_output=True, text=True).stdout
            return str(pid) in listing.split()
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def _wait_until(self, condition, seconds=10):
        import time
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.05)
        return condition()

    def test_runner_stop_ends_the_processes_the_cli_started(self):
        # The CLI starts cfx5solve, which starts solver-mpi.exe; stopping only
        # the CLI left the solver running (seen as exit=62097 reason=crashed).
        from blade_gui.runner import CommandRunner

        runner = CommandRunner(Path.cwd())
        grandchild = self._start_with_grandchild(runner)
        self.assertTrue(self._alive(grandchild))
        runner.stop()
        self.assertTrue(self._wait_until(lambda: not runner.running))
        self.assertTrue(self._wait_until(lambda: not self._alive(grandchild)), 'grandchild survived Stop')

    def test_descendants_follow_parent_links_only(self):
        from blade_gui.runner import descendants

        pairs = [(1, 0), (10, 1), (11, 10), (12, 11), (13, 10), (20, 1), (21, 20), (30, 30)]
        self.assertEqual(sorted(descendants(10, pairs)), [11, 12, 13])
        self.assertEqual(descendants(12, pairs), [])
        self.assertEqual(descendants(30, pairs), [])

    def test_stop_ends_a_process_whose_parent_already_exited(self):
        # CLI -> middle -> leaf; the middle exits, so the leaf is no longer in
        # the CLI's tree. The identity recorded while it was must still be used.
        import sys
        from blade_gui.runner import CommandRunner

        runner = CommandRunner(Path.cwd())
        lines = []
        runner.line.connect(lambda _channel, text: lines.append(text))
        leaf = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'
        middle = (f'import subprocess,sys,time; l = subprocess.Popen([sys.executable, "-c", {leaf!r}]); '
                  'print("leaf", l.pid, flush=True); time.sleep(2)')
        runner.start([sys.executable, '-c',
                      'import signal,subprocess,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                      f'subprocess.run([sys.executable, "-c", {middle!r}]); '
                      'print("middle-done", flush=True); time.sleep(60)'])
        self.assertTrue(self._wait_until(lambda: any(line.startswith('leaf') for line in lines)))
        runner._track()  # what the periodic tracker does while the middle is alive
        pid = int(next(line for line in lines if line.startswith('leaf')).split()[1])
        self.assertTrue(self._wait_until(lambda: 'middle-done' in lines))
        runner.stop()
        self.assertTrue(self._wait_until(lambda: not runner.running))
        self.assertTrue(self._wait_until(lambda: not self._alive(pid)), 'orphaned leaf survived Stop')

    def test_a_cli_that_exits_by_itself_reports_processes_it_left_running(self):
        import signal
        import sys
        from blade_gui.runner import CommandRunner

        runner = CommandRunner(Path.cwd())
        lines = []
        runner.line.connect(lambda _channel, text: lines.append(text))
        runner.start([sys.executable, '-c',
                      'import subprocess,sys,time; g = subprocess.Popen([sys.executable, "-c", "import time; '
                      'time.sleep(60)"]); print("grandchild", g.pid, flush=True); time.sleep(1.5)'])
        self.assertTrue(self._wait_until(lambda: any(line.startswith('grandchild') for line in lines)))
        pid = int(next(line for line in lines if line.startswith('grandchild')).split()[1])
        try:
            runner._track()  # what the periodic tracker does while the CLI runs
            self.assertTrue(self._wait_until(lambda: not runner.running))
            warning = [line for line in lines if '命令已结束' in line]
            self.assertEqual(len(warning), 1)
            self.assertIn(f'PID={pid}', warning[0])
        finally:
            os.kill(pid, signal.SIGTERM)

    def test_a_stale_parent_pid_does_not_adopt_unrelated_processes(self):
        # On Windows a process keeps the PID of a parent that exited long ago; once
        # that PID is reused by this run's CLI, the old process must not look like a child.
        import sys
        import blade_gui.runner as runner_module
        from blade_gui.runner import CommandRunner

        runner = CommandRunner(Path.cwd())
        runner.start([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.assertTrue(self._wait_until(lambda: runner.running))
        root, started = runner._root_pid, runner._started_at

        class Fake:
            def __init__(self, pid, ppid, created, name):
                self.pid, self.info = pid, {'ppid': ppid, 'create_time': created, 'name': name}

        table = [Fake(root, 1, started + .1, 'python.exe'),
                 Fake(900001, root, started - 86400, 'explorer.exe'),   # parent died a day ago
                 Fake(900002, 900001, started + 1, 'notepad.exe'),     # started from that one
                 Fake(900003, root, started + .5, 'cfx5solve.exe'),    # this run's child
                 Fake(900004, 900003, started + .6, 'solver-mpi.exe')]
        with patch.object(runner_module.psutil, 'process_iter', return_value=table):
            runner._track()
        self.assertEqual(sorted(pid for pid, _ in runner._known), sorted([root, 900003, 900004]))
        runner._known = {}  # the fakes must not be signalled
        runner.stop()
        self.assertTrue(self._wait_until(lambda: not runner.running))

    def test_stop_ends_cfx_processes_outside_the_tree_only_in_the_run_directory(self):
        import sys
        import psutil
        import blade_gui.runner as runner_module
        from blade_gui.runner import CommandRunner

        inside_dir = Path(tempfile.mkdtemp(dir=self.root))
        outside_dir = Path(tempfile.mkdtemp(dir=self.root))
        runner = CommandRunner(Path.cwd())
        runner.start([sys.executable, '-c', 'import time; time.sleep(60)'], scope=[inside_dir])
        self.assertTrue(self._wait_until(lambda: runner.running))
        # Started by the test, not the CLI: like MPI workers launched by a service.
        sleeper = [sys.executable, '-c', 'import time; time.sleep(60)']
        inside = subprocess.Popen(sleeper, cwd=inside_dir)
        outside = subprocess.Popen(sleeper, cwd=outside_dir)
        try:
            name = psutil.Process(inside.pid).name().lower()
            with patch.object(runner_module, 'CFX_PROCESS_NAMES', frozenset({name})):
                runner.stop()
            self.assertTrue(self._wait_until(lambda: not runner.running))
            self.assertTrue(self._wait_until(lambda: inside.poll() is not None), 'scoped CFX process survived')
            self.assertIsNone(outside.poll(), 'a process in another directory was stopped')
        finally:
            for proc in (inside, outside):
                proc.kill()
                proc.wait()

    def test_closing_the_window_asks_and_then_ends_the_run(self):
        from PySide6.QtWidgets import QMessageBox
        import blade_gui.main_window as main_window

        runner = self.ctx.command_runners[0]
        grandchild = self._start_with_grandchild(runner)
        try:
            with patch.object(main_window.QMessageBox, 'question', return_value=QMessageBox.No):
                self.assertFalse(self.window.close())
            self.assertTrue(runner.running)
            with patch.object(main_window.QMessageBox, 'question', return_value=QMessageBox.Yes) as asked:
                self.assertTrue(self.window.close())
            asked.assert_called_once()
            self.assertFalse(runner.running)  # stop(wait=True) returns once the CLI is gone
            self.assertTrue(self._wait_until(lambda: not self._alive(grandchild)), 'grandchild survived close')
        finally:
            runner.stop(wait=True)


if __name__ == '__main__':
    unittest.main()
