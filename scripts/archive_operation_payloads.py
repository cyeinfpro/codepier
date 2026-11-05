"""Offline cold-payload maintenance with retained DB/key backup; no VACUUM."""
from __future__ import annotations
import argparse
import json
import uuid

from hub.keyring import _backup
from hub.operation_archive import archive_payloads, restore_payloads
from hub.state_layout import state_directory
from hub.store import Store
from shared.instance_lock import InstanceLock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    parser.add_argument('--restore', action='store_true')
    parser.add_argument('--apply', action='store_true', help='Requires stopped Hub; retains a full matching backup')
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--batch', type=int, default=100)
    args = parser.parse_args()
    directory = state_directory(args.data)
    if not (directory / 'hub.sqlite3').is_file():
        parser.error('Select an existing Hub database')
    with InstanceLock(directory / '.hub.lock'), InstanceLock(directory / '.keys.lock'):
        store = Store(args.data)
        try:
            backup = None
            if args.apply:
                parent = directory / '.payload-backups'
                if parent.is_symlink():
                    raise RuntimeError('Backup directory cannot be a symlink')
                parent.mkdir(mode=0o700, exist_ok=True)
                backup = parent / uuid.uuid4().hex
                _backup(store, backup)
            options = {'batch': args.batch, 'dry_run': not args.apply}
            result = restore_payloads(store, **options) if args.restore else archive_payloads(store, days=args.days, **options)
            print(json.dumps({**result, 'backup': str(backup) if backup else None}))
        finally:
            store.close()


if __name__ == '__main__':
    main()
