#!/usr/bin/env python3
"""Export a review store as legacy inline JSON for inspection or rollback.

Never modifies live manifests or blobs. The output must be a new private
directory; stop all Agent/native writers before using an export for rollback.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.coding_reviews import ReviewStore, fingerprint, MAX_DOCUMENT_BYTES
from shared.util import atomic_json
import json


def export(state_directory, destination):
    state = Path(state_directory)
    destination = Path(destination)
    source = state/'coding-reviews'
    if not source.is_dir() or source.is_symlink():
        raise ValueError('Expected an existing private coding-reviews directory')
    if destination.resolve().is_relative_to(source.resolve()):
        raise ValueError('Export must be outside the live review store')
    store = ReviewStore(SimpleNamespace(config={}, journal=SimpleNamespace(directory=state)))
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    count = 0
    try:
        with store.lock():
            for path in source.glob('*.json'):
                document, _ = store._read(path)
                wrapped = {'sha256': fingerprint(document), 'payload': document}
                if len(json.dumps(wrapped, ensure_ascii=False, indent=2).encode()) > MAX_DOCUMENT_BYTES:
                    raise ValueError('Snapshot exceeds the legacy inline size limit')
                atomic_json(destination/path.name, wrapped)
                count += 1
    except BaseException:
        shutil.rmtree(destination)
        raise
    return {'records': count, 'format': 'inline-v1', 'source_unchanged': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.state_dir, args.output)))


if __name__ == '__main__':
    main()
