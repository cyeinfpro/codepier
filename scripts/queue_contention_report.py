"""Read-only offline instance-administrator queue diagnostic."""

import argparse
import json
from pathlib import Path
import sqlite3
from hub.queue_contention import queue_contention_report


class Reader:
    def __init__(self, connection):
        self.connection = connection

    def all(self, sql, args=()):
        return [dict(row) for row in self.connection.execute(sql, args)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    path = Path(args.database).resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        print(json.dumps(queue_contention_report(Reader(connection), top_n=args.top), indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
