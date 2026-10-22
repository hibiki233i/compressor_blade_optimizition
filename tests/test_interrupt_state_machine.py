"""Cancellation races use real receipts and disposable Python subprocesses only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import blade_shape_active_learning as b
import blade_shape_interrupt as interruption
import blade_shape_pending as pending
import blade_shape_signature as signatures
from blade_shape_runtime import atomic_json, file_identity
from tests import test_refinement
from tests.synthetic_pipeline import fake_geometry


METRICS = dict(Efficiency=.8, MassFlow=4.5, PressureRatio=2., Power=1., totalpressureratio=2.1)


def stop_child(process: subprocess.Popen) -> None:
    """Always reap a test-owned child, including when the regression fails."""
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


class InterruptedAttemptStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_stop_token_cannot_archive_an_explicitly_failed_attempt(self):
        for index, stage in enumerate(('geometry', 'pre', 'solve', 'solve_restart', 'post', 'process')):
            with self.subTest(stage=stage):
                case = self.root/f'case_{index:06d}'
                run_id = case.name
                attempt = interruption.begin_case(case, run_id, 'same-inputs', [1.])
                failure = {'status': 'failed', 'exit_code': 7, 'message': 'tool already failed'}
                if stage == 'geometry':
                    receipt = case/'geometry_state.json'
                    atomic_json(receipt, failure)
                elif stage == 'process':
                    receipt = case/'active_process.json'
                    atomic_json(case/'geometry_state.json', {'status': 'running'})
                    atomic_json(receipt, {'status': 'finished', 'exit_code': 7, 'cancelled': False,
                                          'owner': attempt['owner'], 'run_id': run_id})
                else:
                    receipt = case/'cfx_state.json'
                    atomic_json(receipt, {'version': 2, 'stages': {stage: failure}})
                interruption.request_stop(attempt['owner'])
                before = {path: file_identity(path) for path in (receipt, case/interruption.ATTEMPT)}
                try:
                    interruption.begin_case(case, run_id, 'same-inputs', [1.])
                except (ValueError, RuntimeError):
                    pass
                self.assertEqual(before, {path: file_identity(path) for path in before},
                                 'a stop request must preserve an already failed attempt')
                self.assertFalse((case/'attempts').exists(), 'a failed tool must not be archived for automatic retry')
                self.assertFalse((case/interruption.RETRY).exists())

    def test_active_receipt_failure_after_spawn_reaps_the_child(self):
        original_popen = subprocess.Popen
        for index, error_type in enumerate((KeyboardInterrupt, OSError)):
            with self.subTest(error=error_type.__name__):
                case = self.root/f'case_{index:06d}'
                interruption.begin_case(case, case.name, 'same-inputs', [1.])
                children = []
                injected = False

                def spawn(*args, **kwargs):
                    process = original_popen(*args, **kwargs)
                    children.append(process)
                    self.addCleanup(stop_child, process)
                    return process

                def write_receipt(path, value):
                    nonlocal injected
                    if Path(path).name == 'active_process.json' and not injected:
                        injected = True
                        raise error_type('interrupted while recording the spawned child')
                    atomic_json(path, value)

                with patch.object(interruption.subprocess, 'Popen', side_effect=spawn), \
                        patch.object(interruption, 'atomic_json', side_effect=write_receipt):
                    with self.assertRaises(error_type):
                        interruption.run_logged([sys.executable, '-c', 'import time; time.sleep(30)'],
                                                case, case/'external.log')
                self.assertTrue(injected)
                self.assertTrue(children, 'the injected fault must happen after a real child was started')
                for process in children:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.fail('receipt failure left a live subprocess writing outside optimizer ownership')

    def test_request_stop_merges_process_ids_from_concurrent_processes(self):
        owner_env = interruption.stop_environment(self.root)
        owner = {'token': owner_env[interruption.TOKEN_ENV], 'stop_file': owner_env[interruption.STOP_ENV]}
        interruption.request_stop(owner)
        barrier = self.root/'writers.ready'
        script = (
            'import json,sys,time; from pathlib import Path; '
            'from blade_shape_interrupt import request_stop; '
            'owner=json.loads(sys.argv[1]); barrier=Path(sys.argv[2]); ids=json.loads(sys.argv[3]); '
            'print("ready",flush=True); deadline=time.monotonic()+15\n'
            'while not barrier.exists():\n'
            ' if time.monotonic()>deadline: raise RuntimeError("writer barrier timed out")\n'
            ' time.sleep(.005)\n'
            'for pid in ids:\n'
            ' request_stop(owner,process_ids=[pid])\n'
            ' time.sleep(.002)\n'
        )
        expected = set()
        children = []
        for writer in range(4):
            ids = list(range(10000+writer*16, 10000+(writer+1)*16))
            expected.update(ids)
            process = subprocess.Popen([sys.executable, '-c', script, json.dumps(owner), str(barrier), json.dumps(ids)],
                                       cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, encoding='utf-8')
            children.append(process)
            self.addCleanup(stop_child, process)
        for process in children:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
        barrier.write_text('start', encoding='utf-8')
        for process in children:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stdout+stderr)
        receipt = json.loads(Path(owner['stop_file']).read_text(encoding='utf-8'))
        self.assertEqual(receipt['token'], owner['token'])
        self.assertEqual(set(receipt['process_ids']), expected, 'one stop writer overwrote another writer\'s process IDs')

    def test_archive_replay_rejects_a_different_journal_attempt(self):
        for index, changed in enumerate(({'run_id': 'case_999999'}, {'signature': 'other-inputs'}, {'x': [2.]})):
            with self.subTest(changed=changed):
                case = self.root/f'case_{index:06d}'
                attempt = interruption.begin_case(case, case.name, 'same-inputs', [1.])
                (case/'partial.txt').write_text('preserve this evidence', encoding='utf-8')
                interruption.request_stop(attempt['owner'])
                original_move = interruption.shutil.move

                def fail_before_attempt_move(source, target):
                    if Path(source).name == interruption.ATTEMPT:
                        raise OSError('archive interrupted before moving the attempt receipt')
                    return original_move(source, target)

                with patch.object(interruption.shutil, 'move', side_effect=fail_before_attempt_move):
                    with self.assertRaises(OSError):
                        interruption.begin_case(case, case.name, 'same-inputs', [1.])
                journal_path = case/interruption.RETRY
                journal = json.loads(journal_path.read_text(encoding='utf-8'))
                self.assertEqual(journal['attempts'][0]['status'], 'archiving')
                journal['attempts'][0]['attempt'].update(changed)
                atomic_json(journal_path, journal)
                before = {path.relative_to(case): file_identity(path) for path in case.rglob('*') if path.is_file()}
                with self.assertRaises((ValueError, RuntimeError)):
                    interruption.begin_case(case, case.name, 'same-inputs', [1.])
                after = {path.relative_to(case): file_identity(path) for path in case.rglob('*') if path.is_file()}
                self.assertEqual(before, after, 'journal identity conflict must not move or rewrite evidence')


    def test_reused_pids_do_not_block_retry_but_the_recorded_process_does(self):
        if interruption.process_started_at(os.getpid()) is None:
            self.skipTest('process start times are unavailable on this platform')
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(stop_child, child)
        started = interruption.process_started_at(child.pid)
        self.assertIsNotNone(started)
        before = started - 60  # evidence written a minute before this process existed

        def owned_case(name):
            case = self.root/name
            attempt = interruption.begin_case(case, 'case_000000', 'same-inputs', [1.])
            return case, attempt['owner']

        def stop_receipt(owner, processes=None, mtime=None):
            receipt = {'token': owner['token'], 'process_ids': [child.pid]}
            if processes is not None:
                receipt['processes'] = {str(child.pid): processes}
            atomic_json(Path(owner['stop_file']), receipt)
            if mtime is not None:
                os.utime(owner['stop_file'], (mtime, mtime))

        def active(case, **record):
            atomic_json(case/'active_process.json', {'status': 'running', 'pid': child.pid, **record})

        def pids_file(case, mtime=None):
            path = case/'Impeller_001.dir/pids'
            path.parent.mkdir()
            path.write_text(f'| host | 0 | {child.pid} |\n')
            if mtime is not None:
                os.utime(path, (mtime, mtime))

        def legacy_active(case, owner, live):
            active(case)
            if not live:
                os.utime(case/'active_process.json', (before, before))

        sources = {
            'stop receipt start time': lambda case, owner, live: stop_receipt(
                owner, {'started_at': started if live else started - 60}),
            'legacy stop receipt': lambda case, owner, live: stop_receipt(owner, mtime=None if live else before),
            'active process start time': lambda case, owner, live: active(
                case, started_at=started if live else started - 60),
            'legacy active process': legacy_active,
            'CFX pids file': lambda case, owner, live: pids_file(case, mtime=None if live else before),
        }
        for index, (source, record) in enumerate(sources.items()):
            with self.subTest(source=source):
                case, owner = owned_case(f'reused_{index}')
                record(case, owner, False)
                interruption.assert_idle(case)  # an unrelated process now holds the PID
                case, owner = owned_case(f'live_{index}')
                record(case, owner, True)
                with self.assertRaisesRegex(RuntimeError, str(child.pid)):
                    interruption.assert_idle(case)

    def test_finished_process_keeps_its_exit_and_the_next_stage_honours_a_late_stop(self):
        case = self.root/'case_000000'
        attempt = interruption.begin_case(case, case.name, 'same-inputs', [1.])
        original_write = interruption.atomic_json

        def finish_then_stop(path, value):
            original_write(path, value)
            if Path(path).name == 'active_process.json' and value.get('status') == 'finished':
                interruption.request_stop(attempt['owner'])

        with patch.object(interruption, 'atomic_json', side_effect=finish_then_stop):
            code = interruption.run_logged([sys.executable, '-c', 'pass'], case, case/'cfx_solve.log')
        self.assertEqual(code, 0, 'a finished Solve must reach its stage receipt before the stop applies')
        with patch.object(interruption.subprocess, 'Popen', side_effect=AssertionError('next stage started after stop')):
            with self.assertRaises(interruption.RunInterrupted):
                interruption.run_logged([sys.executable, '-c', 'pass'], case, case/'cfx_post.log')

    def test_only_an_accepted_input_change_retries_a_stopped_attempt(self):
        case = self.root/'case_000000'
        attempt = interruption.begin_case(case, case.name, 'old-inputs', [1.])
        (case/'partial.txt').write_text('stopped work', encoding='utf-8')
        interruption.request_stop(attempt['owner'])
        with self.assertRaisesRegex(ValueError, 'inputs changed'):
            interruption.begin_case(case, case.name, 'new-inputs', [1.], accepted={'new-inputs'})
        self.assertFalse((case/'attempts').exists())
        interruption.begin_case(case, case.name, 'new-inputs', [1.], accepted={'old-inputs', 'new-inputs'})
        self.assertEqual((case/'attempts/interrupted_0001/partial.txt').read_text(encoding='utf-8'), 'stopped work')
        self.assertEqual(json.loads((case/interruption.ATTEMPT).read_text())['signature'], 'new-inputs')


class CommittedOutcomeCancellationTests(unittest.TestCase):
    setUp = test_refinement.RefinementTests.setUp

    def test_committed_terminal_row_survives_ctrl_c_without_duplicate_or_recalculation(self):
        outcomes = [('success', ''), ('review', ''), ('failed', 'geometry_rules'),
                    ('failed', 'geometry'), ('failed', 'solve'), ('failed', 'post')]
        for index, (status, stage) in enumerate(outcomes):
            with self.subTest(status=status, stage=stage):
                run_id = f'case_{index:06d}'
                case = self.root/'cases'/run_id
                metadata = {'sample_phase': 'doe', 'doe_index': index}

                def commit_then_interrupt(*args, **kwargs):
                    row = {**b.vector_to_sample(self.config, self.center), **METRICS, **metadata,
                           'run_id': run_id, 'case_dir': str(case), 'status': status,
                           'failure_stage': stage, 'message': 'already committed result',
                           'external_note': 'preserve committed metadata',
                           signatures.COLUMN: signatures.physical_signature(self.config)}
                    if status == 'failed':
                        row.update({key: np.nan for key in b.RESULT_COLUMNS})
                    b.append_row(self.config, row)
                    raise KeyboardInterrupt('Ctrl+C immediately after CSV commit')

                with patch.object(b, '_evaluate_true_cfd', side_effect=commit_then_interrupt):
                    try:
                        b.evaluate_true_cfd(self.config, self.baseline, self.center, index, **metadata)
                    except KeyboardInterrupt:
                        pass
                frame = b.load_training(self.config).loc[lambda value: value.run_id.eq(run_id)]
                self.assertEqual(frame.status.tolist(), [status], 'cancellation appended or replaced a committed result')
                self.assertEqual(frame.iloc[0].external_note, 'preserve committed metadata')
                before = file_identity(b.training_csv_path(self.config))
                with patch.object(b, '_evaluate_true_cfd', side_effect=AssertionError('committed case was recomputed')):
                    result = b.evaluate_true_cfd(self.config, self.baseline, self.center, index, **metadata)
                self.assertEqual(result.row['status'], status)
                self.assertEqual(before, file_identity(b.training_csv_path(self.config)))
                self.assertFalse((case/'attempts').exists())

    def test_unmarked_crash_before_candidate_does_not_start_work_under_an_old_owner(self):
        case = self.root/'cases/case_000000'
        interruption.begin_case(case, 'case_000000', signatures.physical_signature(self.config), self.center.tolist())
        before = file_identity(case/interruption.ATTEMPT)
        with patch.object(b, 'run_geometry', side_effect=AssertionError('unmarked crash restarted geometry')), \
                patch.object(b, 'run_cfx_pipeline', side_effect=AssertionError('unmarked crash restarted CFX')):
            with self.assertRaisesRegex(ValueError, 'Unmarked running'):
                b.evaluate_true_cfd(self.config, self.baseline, self.center, 0, sample_phase='doe')
        self.assertEqual(before, file_identity(case/interruption.ATTEMPT))
        self.assertFalse((case/'candidate.json').exists())
        self.assertTrue(b.load_training(self.config).empty)

    def test_failure_before_csv_then_stop_is_finalized_without_recalculation_or_budget(self):
        root = self.root
        for stage in ('geometry', 'pre', 'solve', 'solve_restart', 'post'):
            with self.subTest(stage=stage):
                self.root = root/stage
                self.config['paths']['output_dir'] = str(self.root)
                pending.enqueue(self.config, [b.SelectedCandidate(self.center, 1, 'doe')], [{'sample_phase': 'doe'}])
                def fail_then_stop(case):
                    failure = {'status': 'failed', 'message': 'genuine tool failure'}
                    if stage == 'geometry':
                        atomic_json(case/'geometry_state.json', failure)
                    else:
                        atomic_json(case/'cfx_state.json', {'version': 2, 'stages': {stage: failure}})
                    raise KeyboardInterrupt('stop after durable failure, before training CSV')
                def geometry(config, candidate_path, **kwargs):
                    return fail_then_stop(candidate_path.parent) if stage == 'geometry' else fake_geometry(config, candidate_path, **kwargs)
                with patch.object(b, 'run_geometry', side_effect=geometry), \
                        patch.object(b, 'run_cfx_pipeline', side_effect=lambda case, *a, **kw: fail_then_stop(case)):
                    with self.assertRaises(KeyboardInterrupt):
                        pending.run(self.config, self.baseline, 1)
                with patch.object(b, 'run_geometry', side_effect=AssertionError('failed case reran')), \
                        patch.object(b, 'run_cfx_pipeline', side_effect=AssertionError('failed solver reran')):
                    result = pending.run(self.config, self.baseline, 0)
                self.assertEqual(result['new_attempts'], 0)
                self.assertEqual(b.load_training(self.config).status.tolist(), ['failed'])
                self.assertTrue(pending.load(self.config)['entries'][0]['finalized'])
                self.assertFalse((self.root/'cases/case_000000/attempts').exists())

    def test_verified_geometry_handoff_uses_the_new_gui_stop_owner(self):
        case = self.root/'cases/case_000000'
        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(b, 'run_cfx_pipeline', side_effect=RuntimeError('crash before any CFX stage')):
            with self.assertRaises(RuntimeError):
                b.evaluate_true_cfd(self.config, self.baseline, self.center, 0, sample_phase='doe')
        old = json.loads((case/interruption.ATTEMPT).read_text())
        geometry = file_identity(case/'geometry_state.json')
        env = interruption.stop_environment(self.root)
        def observe_new_stop(*args, **kwargs):
            owner = interruption.current_owner(case)
            self.assertEqual(owner['token'], env[interruption.TOKEN_ENV])
            self.assertNotEqual(owner['token'], old['owner']['token'])
            interruption.request_stop(owner)
            interruption.check_stop(case)
        with patch.dict('os.environ', env), \
                patch.object(b, 'run_geometry', side_effect=AssertionError('verified geometry was recomputed')), \
                patch.object(b, 'run_cfx_pipeline', side_effect=observe_new_stop):
            with self.assertRaises(KeyboardInterrupt):
                b.evaluate_true_cfd(self.config, self.baseline, self.center, 0, sample_phase='doe')
        self.assertEqual(geometry, file_identity(case/'geometry_state.json'))
        self.assertEqual(b.load_training(self.config).status.tolist(), ['interrupted'])
        self.assertTrue(interruption.retry_requested(case))

    def test_natural_nonzero_exit_is_recorded_as_failure_even_when_stop_arrives_before_return(self):
        original_write = interruption.atomic_json
        stop_written = False

        def finish_then_stop(path, value):
            nonlocal stop_written
            original_write(path, value)
            if (Path(path).name == 'active_process.json' and value.get('status') == 'finished'
                    and value.get('exit_code') == 7 and not stop_written):
                stop_written = True
                attempt = json.loads((Path(path).parent/interruption.ATTEMPT).read_text(encoding='utf-8'))
                interruption.request_stop(attempt['owner'])

        def failing_geometry(config, candidate_path, dry_run=False):
            code = interruption.run_logged([sys.executable, '-c', 'raise SystemExit(7)'],
                                           candidate_path.parent, candidate_path.parent/'geometry.log')
            return code == 0, f'geometry tool returned {code}'

        with patch.object(interruption, 'atomic_json', side_effect=finish_then_stop), \
                patch.object(b, 'run_geometry', side_effect=failing_geometry), \
                patch.object(b, 'run_cfx_pipeline', side_effect=AssertionError('a failed geometry must not run CFX')):
            result = b.evaluate_true_cfd(self.config, self.baseline, self.center, 0, sample_phase='doe')
        self.assertTrue(stop_written, 'the stop must be injected after the real nonzero process exit')
        self.assertFalse(result.success)
        self.assertEqual(result.row['status'], 'failed')
        self.assertEqual(result.row['failure_stage'], 'geometry')
        self.assertEqual(b.load_training(self.config).status.tolist(), ['failed'])
        self.assertFalse((self.root/'cases/case_000000/attempts').exists())


if __name__ == '__main__':
    unittest.main()
