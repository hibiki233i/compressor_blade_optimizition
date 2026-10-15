"""Compare portable numerical/selection outputs with the checked-in synthetic reference."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_REFERENCE = Path(__file__).resolve().parent / 'offline_baseline/reference.json'


def compare_outputs(output_dir: Path, reference_path: Path = DEFAULT_REFERENCE) -> None:
    reference = json.loads(reference_path.read_text(encoding='utf-8'))
    tolerance = reference['tolerances']
    for filename, expected in reference['tables'].items():
        actual = pd.read_csv(Path(output_dir) / filename, keep_default_na=False)
        if len(actual) != len(expected['rows']):
            raise ValueError(f'{filename}: expected {len(expected["rows"])} rows, got {len(actual)}')
        columns = expected['exact_columns'] + expected['numeric_columns']
        missing = set(columns) - set(actual.columns)
        if missing:
            raise ValueError(f'{filename}: missing columns {sorted(missing)}')
        wanted = pd.DataFrame(expected['rows'], columns=columns)
        for column in expected['exact_columns']:
            if actual[column].astype(str).tolist() != wanted[column].astype(str).tolist():
                raise ValueError(f'{filename}: {column} differs from reference')
        for column in expected['numeric_columns']:
            values = pd.to_numeric(actual[column], errors='coerce').to_numpy(float)
            target = wanted[column].to_numpy(float)
            matches = np.isfinite(values) & np.isclose(
                values, target, atol=tolerance['absolute'], rtol=tolerance['relative'])
            if not matches.all():
                row = int(np.flatnonzero(~matches)[0])
                raise ValueError(f'{filename}: row {row}, {column}: expected {target[row]}, got {values[row]}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output_dir', type=Path)
    parser.add_argument('--reference', type=Path, default=DEFAULT_REFERENCE)
    args = parser.parse_args()
    try:
        compare_outputs(args.output_dir, args.reference)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Offline baseline FAILED: {exc}\n')
    print('Offline baseline matches reference.')


if __name__ == '__main__':
    main()
