#!/usr/bin/env python3
"""Merge a complete set of same-platform CI shards; missing evidence fails closed."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import stat

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.regression_plan import merge_summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--coverage', action='store_true', help='Also combine every shard coverage file, preserving the originals')
    args = parser.parse_args()
    try:
        if any(path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) for path in args.reports):
            raise ValueError('A shard report must be a regular, non-symlink file')
        paths = sorted({path.resolve() for path in args.reports})
        if len(paths) != len(args.reports):
            raise ValueError('The same report was supplied more than once')
        reports = []
        for path in paths:
            if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError('A shard report is not bounded regular input')
            reports.append(json.loads(path.read_text(encoding='utf-8')))
        result = merge_summaries(reports)
        if args.coverage:
            if not all(report.get('coverage_requested') and not report.get('coverage_error') for report in reports):
                raise ValueError('A shard lacks verified coverage measurements')
            from scripts.coverage_reports import combine
            combine([path.parent/'.coverage' for path in paths], args.output.parent)
            result['coverage_merged'] = True
    except Exception as error:
        result = {'verified': False, 'scope': 'incomplete', 'error': str(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0 if result['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
