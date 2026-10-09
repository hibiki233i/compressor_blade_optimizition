from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import blade_shape_local_config as local

ROOT = Path(__file__).resolve().parent.parent


class LocalPathsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config_path = self.root / 'config.json'
        self.ini = self.root / local.LOCAL_INI_NAME
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(local.LOCAL_INI_ENV, None)
        for key in [key for key in os.environ if local.ANSYS_ROOT_ENV.fullmatch(key)]:
            del os.environ[key]

    def test_ini_fills_only_empty_json_paths(self):
        self.ini.write_text('# machine\n[paths]\ncfx_bin_dir = D:\\ANSYS Inc\\v251\\CFX\\bin\n'
                            'output_dir = D:\\runs\nextra_key = E:\\x\n', encoding='utf-8')
        config = {'paths': {'cfx_bin_dir': '', 'output_dir': 'C:\\explicit', 'base_cft': ''}}
        local.apply_local_paths(config, self.config_path)
        self.assertEqual(config['paths']['cfx_bin_dir'], 'D:\\ANSYS Inc\\v251\\CFX\\bin')
        self.assertEqual(config['paths']['output_dir'], 'C:\\explicit')
        self.assertEqual(config['paths']['base_cft'], '')
        self.assertEqual(config['paths']['extra_key'], 'E:\\x')
        meta = config[local.META_KEY]
        self.assertTrue(meta['found'])
        self.assertEqual(sorted(meta['keys']), ['base_cft', 'cfx_bin_dir', 'extra_key'])

    def test_missing_ini_and_env_override(self):
        config = local.apply_local_paths({'paths': {'cfx_bin_dir': ''}}, self.config_path)
        self.assertFalse(config[local.META_KEY]['found'])
        self.assertEqual(config['paths']['cfx_bin_dir'], '')
        other = self.root / 'elsewhere' / 'machine.ini'
        other.parent.mkdir()
        other.write_text('[paths]\ncfx_bin_dir = /opt/cfx\n')
        os.environ[local.LOCAL_INI_ENV] = str(other)
        config = local.apply_local_paths({'paths': {'cfx_bin_dir': ''}}, self.config_path)
        self.assertEqual(config['paths']['cfx_bin_dir'], '/opt/cfx')

    def test_ansys_tools_derived_from_awp_root(self):
        cfx, tg = os.path.join('/ansys/v251', 'CFX', 'bin'), os.path.join('/ansys/v251', 'TurboGrid', 'bin', 'cfxtg.exe')
        empty = lambda: {'paths': {'cfx_bin_dir': '', 'turbogrid_exe': '', 'base_cft': ''}}
        os.environ['AWP_ROOT251'] = '/ansys/v251'
        # no INI, a single install: used without a version
        config = local.apply_local_paths(empty(), self.config_path)
        self.assertEqual((config['paths']['cfx_bin_dir'], config['paths']['turbogrid_exe']), (cfx, tg))
        self.assertEqual(config[local.META_KEY]['sources'], {'cfx_bin_dir': 'AWP_ROOT251', 'turbogrid_exe': 'AWP_ROOT251'})
        # an explicit INI path wins; a non-empty JSON path is never replaced
        self.ini.write_text('[paths]\nturbogrid_exe = D:\\tg.exe\n')
        config = local.apply_local_paths({'paths': {'cfx_bin_dir': 'C:\\json', 'turbogrid_exe': ''}}, self.config_path)
        self.assertEqual(config['paths'], {'cfx_bin_dir': 'C:\\json', 'turbogrid_exe': 'D:\\tg.exe'})
        # several installs: only an explicit version selects one
        os.environ['AWP_ROOT242'] = '/ansys/v242'
        config = local.apply_local_paths(empty(), self.config_path)
        self.assertEqual(config['paths']['cfx_bin_dir'], '')
        self.assertIn('242、251', config[local.META_KEY]['ansys_problem'])
        self.ini.write_text('[ansys]\nversion = v251\n')
        config = local.apply_local_paths(empty(), self.config_path)
        self.assertEqual(config['paths']['cfx_bin_dir'], cfx)
        self.assertEqual(config[local.META_KEY]['ansys_problem'], '')
        self.ini.write_text('[ansys]\nversion = 252\n')
        config = local.apply_local_paths(empty(), self.config_path)
        self.assertEqual(config['paths']['cfx_bin_dir'], '')
        self.assertIn('AWP_ROOT252', config[local.META_KEY]['ansys_problem'])

    def test_diagnostics_explain_why_ini_is_not_used(self):
        misnamed = self.root / (local.LOCAL_INI_NAME + '.txt')
        misnamed.write_text('[paths]\ncfx_bin_dir = D:\\cfx\n')
        config = local.apply_local_paths({'paths': {'cfx_bin_dir': ''}}, self.config_path)
        self.assertEqual(config['paths']['cfx_bin_dir'], '')
        self.assertIn('改名', local.describe_local_paths(config)[0][1])
        misnamed.rename(self.ini)
        config = local.apply_local_paths({'paths': {'cfx_bin_dir': 'C:\\json'}}, self.config_path)
        levels = dict((message.split('：')[0], level) for level, message in local.describe_local_paths(config))
        self.assertEqual(levels['本机路径文件'], 'info')
        self.assertEqual(config[local.META_KEY]['ignored'], ['cfx_bin_dir'])
        self.assertTrue(any('未生效' in m for _, m in local.describe_local_paths(config)))
        from blade_gui.project import validate_config
        self.assertTrue(any('未生效' in item.message and item.level == 'warning' for item in validate_config(config)))

    def test_save_writes_only_edited_paths_and_never_derived_ones(self):
        from blade_gui.project import read_config_file, save_config
        os.environ['AWP_ROOT251'] = '/ansys/v251'
        self.config_path.write_text(json.dumps({'paths': {'cfx_bin_dir': '', 'turbogrid_exe': '', 'base_cft': '',
                                                          'output_dir': ''}, 'runtime': {}}))
        self.ini.write_text('# mine\n[paths]\nbase_cft = D:\\b.cft\noutput_dir = D:\\runs\n\n[ansys]\nversion = 251\n')
        before = self.ini.read_bytes()
        config = read_config_file(self.config_path)
        save_config(self.config_path, config, keep_backup=False)
        self.assertEqual(self.ini.read_bytes(), before)
        config['paths']['output_dir'] = 'E:\\runs'
        save_config(self.config_path, config, keep_backup=False)
        self.assertEqual(local.read_local_paths(self.ini), {'base_cft': 'D:\\b.cft', 'output_dir': 'E:\\runs'})
        self.assertIn('version = 251', self.ini.read_text())
        # "save as" copies the INI values but leaves the derived ANSYS paths to the new location
        target = self.root / 'copy' / 'cfg.json'
        target.parent.mkdir()
        save_config(target, config, keep_backup=False)
        self.assertEqual(local.read_local_paths(target.parent / local.LOCAL_INI_NAME),
                         {'base_cft': 'D:\\b.cft', 'output_dir': 'E:\\runs'})

    def test_report_command_prints_sources(self):
        import contextlib
        import io
        os.environ['AWP_ROOT251'] = '/ansys/v251'
        self.config_path.write_text(json.dumps({'paths': {'cfx_bin_dir': '', 'output_dir': 'shared'}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(local.main([str(self.config_path)]), 0)
        text = out.getvalue()
        self.assertIn('AWP_ROOT251', text)
        self.assertRegex(text, r'output_dir\s+json\s+shared')
        self.assertFalse(self.ini.exists())

    def test_write_keeps_comments_and_other_sections(self):
        self.ini.write_text('; keep me\n[other]\na = 1\n\n[paths]\n# note\ncfx_bin_dir = old\n\n[tail]\nb = 2\n')
        local.write_local_paths(self.ini, {'cfx_bin_dir': 'new', 'base_cft': 'D:\\b.cft'})
        text = self.ini.read_text()
        for kept in ('; keep me', '[other]', 'a = 1', '# note', '[tail]', 'b = 2'):
            self.assertIn(kept, text)
        self.assertEqual(local.read_local_paths(self.ini), {'cfx_bin_dir': 'new', 'base_cft': 'D:\\b.cft'})
        self.assertLess(text.index('base_cft'), text.index('[tail]'))
        fresh = self.root / 'new' / 'fresh.ini'
        local.write_local_paths(fresh, {'output_dir': 'D:\\runs'})
        self.assertEqual(local.read_local_paths(fresh), {'output_dir': 'D:\\runs'})

    def test_gui_save_round_trip_keeps_machine_paths_out_of_json(self):
        from blade_gui.project import read_config_file, save_config
        self.config_path.write_text(json.dumps({'paths': {'cfx_bin_dir': '', 'output_dir': 'shared'},
                                                'runtime': {}}))
        self.ini.write_text('[paths]\ncfx_bin_dir = D:\\cfx\n')
        config = read_config_file(self.config_path)
        self.assertEqual(config['paths']['cfx_bin_dir'], 'D:\\cfx')
        config['paths']['cfx_bin_dir'] = 'E:\\cfx'
        save_config(self.config_path, config, keep_backup=False)
        saved = json.loads(self.config_path.read_text())
        self.assertEqual(saved['paths'], {'cfx_bin_dir': '', 'output_dir': 'shared'})
        self.assertNotIn(local.META_KEY, saved)
        self.assertEqual(local.read_local_paths(self.ini), {'cfx_bin_dir': 'E:\\cfx'})
        # "save as" elsewhere carries the machine paths into an INI beside the new file
        target = self.root / 'copy' / 'cfg.json'
        target.parent.mkdir()
        save_config(target, config, keep_backup=False)
        self.assertEqual(local.read_local_paths(target.parent / local.LOCAL_INI_NAME), {'cfx_bin_dir': 'E:\\cfx'})

    def test_failed_json_save_rolls_back_ini(self):
        from blade_gui.project import read_config_file, save_config
        self.config_path.write_text(json.dumps({'paths': {'output_dir': ''}, 'runtime': {}}))
        self.ini.write_text('# mine\n[paths]\noutput_dir = D:\\old\n')
        before = self.ini.read_bytes()
        config = read_config_file(self.config_path)
        config['paths']['output_dir'] = 'D:\\new'
        with patch('blade_gui.project.shutil.copy2', side_effect=PermissionError('backup')), \
                self.assertRaises(PermissionError):
            save_config(self.config_path, config)
        self.assertEqual(self.ini.read_bytes(), before)
        self.ini.unlink()
        with patch('blade_gui.project.shutil.copy2', side_effect=PermissionError('backup')), \
                self.assertRaises(PermissionError):
            save_config(self.config_path, config)
        self.assertFalse(self.ini.exists())

    def test_empty_output_dir_is_rejected_not_cwd(self):
        import blade_shape_active_learning as al
        with self.assertRaisesRegex(ValueError, 'output_dir is empty'):
            al.output_dir({'paths': {'output_dir': ''}})

    def test_cli_loader_uses_ini(self):
        import blade_shape_active_learning as al
        config = json.loads((ROOT / 'blade_shape_config.json').read_text())
        self.config_path.write_text(json.dumps(config))
        self.ini.write_text((ROOT / 'blade_shape_local.ini.example').read_text(encoding='utf-8'), encoding='utf-8')
        loaded = al.load_config(self.config_path)
        self.assertEqual(loaded['paths']['cfx_bin_dir'], 'D:\\ANSYS Inc\\v251\\CFX\\bin')

    def test_tracked_config_has_no_machine_paths_and_example_covers_them(self):
        paths = json.loads((ROOT / 'blade_shape_config.json').read_text())['paths']
        self.assertTrue(all(value == '' for value in paths.values()), paths)
        example = local.read_local_paths(ROOT / 'blade_shape_local.ini.example')
        self.assertEqual(set(example), set(paths))


if __name__ == '__main__':
    unittest.main()
