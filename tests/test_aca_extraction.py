from __future__ import annotations

import csv
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import blade_shape_aca_extraction as aca
import blade_shape_incidence_validation as v

LINE = 'Spanwise Plot of Beta at LE Line'
HEADER = f'Span Normalized,Velocity Beta ACA on {LINE} [ degree ]'
# synthetic session: only the settings this module reads; never a real CFX-Post input
SESSION = """COMMAND FILE:
  CFX Post Version = 25.1
END
TURBO LINE: Spanwise Plot of Beta at LE Line
  Turbo Domain List = R1
  Turbo Line Mode = Blade Aligned
  Streamwise Location = 0.251
  Span Points = 20
  Circumferential Average Mode = Area
END
EXPORT:
  Export File = aca_raw.csv
  Precision = 12
END
>export
>quit
"""


def raw_export(points=20, beta=lambda j: -10.6 - 2.5 * j, span=lambda j: j / 19, extra=''):
    rows = '\n'.join(f'{span(j):.12e},{beta(j):.12g}' for j in range(points))
    return f'[Name]\n{LINE}\n\n[Spatial Fields]\nSpan Normalized\n\n[Data]\n{HEADER}\n{rows}\n{extra}'


class ParseTests(unittest.TestCase):
    def test_session_settings_and_export_name(self):
        settings = aca.session_settings(SESSION)
        self.assertEqual(settings['Streamwise Location'], ['0.251'])
        self.assertEqual(settings['Turbo Domain List'], ['R1'])
        self.assertEqual(aca.export_name(settings), 'aca_raw.csv')
        for bad in ('D:\\exports\\aca_raw.csv', '/tmp/aca.csv', '../aca.csv', '\\exports\\aca_raw.csv',
                    'D:aca_raw.csv', 'sub\\..\\..\\aca.csv'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                aca.export_name({'Export File': [bad]})
        with self.assertRaises(ValueError):
            aca.export_name({})

    def test_tidy_keeps_raw_text_and_reports_quadrant(self):
        header, rows = aca.parse_generic_export(raw_export(extra='\n[Lines]\n0,1\n'))
        tidy, checks = aca.tidy_export(header, rows)
        self.assertEqual(len(tidy), 20)
        self.assertEqual(tidy[0], dict(span=rows[0][0], beta_cfx_deg=rows[0][1]))
        self.assertTrue(checks['legacy_aca_quadrant_ok'])
        self.assertEqual(checks['line_name'], LINE)
        _, checks = aca.tidy_export(*aca.parse_generic_export(raw_export(beta=lambda j: 5.0 - j)))
        self.assertFalse(checks['legacy_aca_quadrant_ok'])

    def test_tidy_rejects_wrong_layout(self):
        cases = {
            'points': raw_export(points=19),
            'span': raw_export(span=lambda j: (j / 19) ** 1.1),
            'units': raw_export().replace('[ degree ]', '[ rad ]'),
            'finite': raw_export(beta=lambda j: float('nan') if j == 3 else -20.0),
            'table': raw_export().replace('[Data]', '[Table]'),
        }
        for name, text in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                aca.tidy_export(*aca.parse_generic_export(text))


class ExtractTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        case = self.root / 'case'
        case.mkdir()
        self.res = case / 'sliptip_007.res'
        self.res.write_text('test only')
        self.post = self.root / 'cfx5post.exe'
        self.post.touch()
        self.session = self.root / 'extract_aca.cse'
        self.session.write_text(SESSION)

    def fake_post(self, text=None, code=0, error=''):
        def run(command, **kwargs):
            cwd = Path(kwargs['cwd'])
            self.assertEqual(command[1:3], ['-batch', str(cwd / aca.SESSION_NAME)])
            if text is not None:
                (cwd / 'aca_raw.csv').write_text(text)
            if error:
                (cwd / 'cfdpost_error.log').write_text(error)
            return subprocess.CompletedProcess(command, code, stdout='mock post')
        return patch.object(aca.subprocess, 'run', side_effect=run)

    def test_success_writes_two_column_csv_and_evidence(self):
        out = self.root / 'aca'
        with self.fake_post(raw_export()):
            summary = aca.extract_aca(self.res, self.post, self.session, out)
        self.assertEqual(summary['status'], 'complete')
        self.assertFalse(summary['incidence_calculated'])
        csv_path = Path(summary['aca_csv'])
        self.assertEqual(csv_path.name, 'sliptip_007_beta_aca_20.csv')
        with csv_path.open(newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(list(rows[0]), ['span', 'beta_cfx_deg'])
        self.assertEqual(len(rows), 20)
        inputs = json.loads((out / 'extraction_inputs.json').read_text())
        self.assertEqual(inputs['session_settings']['Streamwise Location'], ['0.251'])
        self.assertIn(str(self.res.resolve()), inputs['input_identities'])
        for name in ('extract_aca.cse', 'cfxpost.log', 'returncode.json', 'aca_raw.csv'):
            self.assertTrue((out / name).is_file(), name)
        # the CSV is accepted unchanged by the legacy entry point
        legacy, result = v.legacy_profile(rows, 70.0, 20.0)
        self.assertEqual(len(legacy), 20)
        self.assertEqual(self.res.read_text(), 'test only')
        with self.assertRaises(FileExistsError):
            aca.extract_aca(self.res, self.post, self.session, out)

    def test_failures_keep_logs_and_never_write_csv(self):
        for name, post in {
            'returncode': self.fake_post(raw_export(), code=1),
            'missing_export': self.fake_post(None),
            'error_log': self.fake_post(raw_export(), error='Licence error'),
            'points': self.fake_post(raw_export(points=21)),
        }.items():
            with self.subTest(name=name):
                out = self.root / f'fail_{name}'
                with post, self.assertRaises((RuntimeError, ValueError)):
                    aca.extract_aca(self.res, self.post, self.session, out)
                state = json.loads((out / aca.SUMMARY_NAME).read_text())
                self.assertEqual(state['status'], 'failed')
                self.assertTrue((out / 'cfxpost.log').is_file())
                self.assertEqual(list(out.glob('*_beta_aca_20.csv')), [])

    def test_zero_exit_with_post_error_reports_cause(self):
        out = self.root / 'post_error'
        with self.fake_post(raw_export(), error='2026/10/09\nExpressionEvaluator - Error in Beta'), \
                self.assertRaisesRegex(RuntimeError, 'returncode=0.*Error in Beta'):
            aca.extract_aca(self.res, self.post, self.session, out)

    def test_input_changed_during_extraction_fails(self):
        def run(command, **kwargs):
            (Path(kwargs['cwd']) / 'aca_raw.csv').write_text(raw_export())
            self.res.write_text('rewritten')
            return subprocess.CompletedProcess(command, 0, stdout='')
        with patch.object(aca.subprocess, 'run', side_effect=run), \
                self.assertRaisesRegex(ValueError, 'Input changed'):
            aca.extract_aca(self.res, self.post, self.session, self.root / 'changed')

    def test_rejects_unsafe_inputs_before_cfx_post(self):
        with patch.object(aca.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'independent'):
                aca.extract_aca(self.res, self.post, self.session, self.res.parent / 'aca')
            with self.assertRaisesRegex(ValueError, 'existing .res'):
                aca.extract_aca(self.session, self.post, self.session, self.root / 'a')
            self.session.write_text(SESSION.replace('aca_raw.csv', 'D:\\aca_raw.csv'))
            with self.assertRaisesRegex(ValueError, 'relative'):
                aca.extract_aca(self.res, self.post, self.session, self.root / 'b')
            run.assert_not_called()
        self.assertFalse((self.root / 'b').exists())

    def test_cli_subcommand(self):
        out = self.root / 'cli'
        with self.fake_post(raw_export()), patch('builtins.print'):
            code = v.main(['aca', '--res', str(self.res), '--post-exe', str(self.post),
                           '--session', str(self.session), '--output-dir', str(out)])
        self.assertEqual(code, 0)
        self.assertTrue((out / 'sliptip_007_beta_aca_20.csv').is_file())


if __name__ == '__main__':
    unittest.main()
