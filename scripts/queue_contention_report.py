"""Read-only offline instance-administrator queue diagnostic and baseline."""

import argparse
import json
from pathlib import Path
import sqlite3
from hub.queue_contention import baseline_window_report, queue_contention_report


class Reader:
    def __init__(self, connection):
        self.connection = connection

    def all(self, sql, args=()):
        return [dict(row) for row in self.connection.execute(sql, args)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--baseline", action="store_true", help="Report the rolling 24h baseline")
    parser.add_argument("--device", help="Restrict the baseline to one device ID")
    parser.add_argument("--project", help="Restrict the baseline to one project ID")
    args = parser.parse_args()
    if not args.baseline and (args.device is not None or args.project is not None):
        parser.error("--device and --project require --baseline")
    path = Path(args.database).resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        reader = Reader(connection)
        report = (
            baseline_window_report(reader, device_id=args.device, project_id=args.project)
            if args.baseline
            else queue_contention_report(reader, top_n=args.top)
        )
        print(json.dumps(report, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
