"""The 项目设置 explanations name real config keys and convert normalised values correctly."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from blade_gui.field_help import HELP, help_document_html, live_hint, tooltip_html

ROOT = Path(__file__).resolve().parent.parent
MISSING = object()


def lookup(config: dict, key: str):
    node = config
    for part in key.split('.'):
        if not isinstance(node, dict) or part not in node:
            return MISSING
        node = node[part]
    return node


class FieldHelpTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / 'blade_shape_config.json').read_text(encoding='utf-8'))

    def test_every_entry_names_a_tracked_config_key(self):
        missing = [key for key in HELP if lookup(self.config, key) is MISSING]
        self.assertEqual(missing, [])
        for key, entry in HELP.items():
            self.assertTrue(entry.hint and entry.text, key)

    def test_normalised_values_are_shown_in_degrees(self):
        spans = [v['upper'] - v['lower'] for v in self.config['variables']
                 if v['name'] in self.config['search']['active_variables']]
        hint = live_hint('constraints.duplicate_distance_norm', self.config, 0.02)
        self.assertEqual(hint, f'单变量 ≈ {0.02 * min(spans):.3g}–{0.02 * max(spans):.3g}°')
        self.assertEqual(live_hint('refinement.local_search.initial_radius_norm', self.config, 0.15),
                         f'± {0.15 * min(spans):.3g}–{0.15 * max(spans):.3g}°')
        self.assertEqual(live_hint('search.slice_tolerance_norm', self.config, 1e-8), '精确匹配')
        self.assertTrue(live_hint('search.slice_tolerance_norm', self.config, 0.01).startswith('固定变量 ≈ '))

    def test_counts_and_inactive_envelopes(self):
        active = len(self.config['search']['active_variables'])
        self.assertEqual(live_hint('runtime.initial_samples', self.config, 48), f'推荐 ≈ 10×{active} = {10 * active}')
        self.assertEqual(live_hint('runtime.batch_size', self.config, 3), '角色数 3，已整除')
        self.assertEqual(live_hint('runtime.batch_size', self.config, 4), '角色数 3，未整除')
        bound = max(max(abs(v['lower']), abs(v['upper'])) for v in self.config['variables'] if 'beta' in v['name'])
        self.assertIn('不起作用', live_hint('constraints.max_beta_offset_deg', self.config, bound + 1))
        self.assertIn('比边界更严', live_hint('constraints.max_beta_offset_deg', self.config, bound - 1))
        # malformed forms never raise
        self.assertEqual(live_hint('constraints.duplicate_distance_norm', {'variables': 'bad'}, 'x'), '')

    def test_rendering_escapes_and_groups(self):
        self.assertIn('&lt;', tooltip_html('runtime.p_out_pa', '<压力>'))
        document = help_document_html([('几何约束', [('constraints.duplicate_distance_norm', '重复距离阈值'),
                                                    ('paths.output_dir', '输出目录')])])
        self.assertIn('<h3>几何约束</h3>', document)
        self.assertIn('重复距离阈值', document)
        self.assertNotIn('输出目录', document)
        self.assertIn('修改影响：物理签名', document)


if __name__ == '__main__':
    unittest.main()
