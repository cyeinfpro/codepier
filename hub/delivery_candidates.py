"""Candidate lookup from the durable operation journal."""

import json

ROUND_LIMIT = 128
PENDING = "state IN ('queued','running','reconnecting','cancelling')"


def migrate(db):
    # Candidate ranking needs only the narrow index until the final rowid lookup.
    db.execute(
        f"CREATE INDEX IF NOT EXISTS op_delivery_due "
        f"ON operations(next_attempt,device_id,created) WHERE {PENDING}"
    )
    # SQLite's implicit rowid suffix supplies the admission-order tie break.
    db.execute(
        f"CREATE INDEX IF NOT EXISTS op_delivery_device "
        f"ON operations(device_id,created) WHERE {PENDING}"
    )


def candidates(store, now, busy_devices, config):
    """Return one fair window, limiting each opted-in device to its chunk size."""
    parameters = {"now": now, "round_limit": ROUND_LIMIT}
    available = ""
    if busy_devices:
        available = (
            "AND device_id IS NOT NULL "
            "AND device_id NOT IN (SELECT value FROM json_each(:busy_devices))"
        )
        parameters["busy_devices"] = json.dumps(
            [device for device in busy_devices if device is not None]
        )
    chunk = ""
    if config.delivery_rolling_devices:
        parameters["chunk"] = config.delivery_device_chunk
        if config.delivery_rolling_devices == ("*",):
            chunk = "WHERE turn<=:chunk OR device_id IS NULL"
        else:
            chunk = (
                "WHERE turn<=:chunk OR device_id IS NULL "
                "OR device_id NOT IN (SELECT value FROM json_each(:rolling))"
            )
            parameters["rolling"] = json.dumps(config.delivery_rolling_devices)
    return store.all(
        f"""WITH ranked AS (
            SELECT rowid AS row_order, created, device_id,
                   ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY created,rowid) AS turn
            FROM operations INDEXED BY op_delivery_due WHERE {PENDING} AND next_attempt<=:now {available}
        ), picked AS (
            SELECT row_order, created, turn FROM ranked {chunk}
            ORDER BY turn,created,row_order LIMIT :round_limit
        )
        SELECT op.id,op.device_id,MAX(op.created,op.next_attempt) AS due,op.attempts
        FROM picked CROSS JOIN operations AS op ON op.rowid=picked.row_order
        ORDER BY picked.turn,picked.created,picked.row_order""",
        parameters,
    )


def refill(store, now, device_id, seen, limit):
    """Fetch the next earliest due rows, leaving already handled rows for another round."""
    seen_ids = json.dumps(seen)
    if seen and store.one(
        f"""SELECT 1 FROM json_each(?) AS seen CROSS JOIN operations AS op ON op.id=seen.value
        WHERE device_id=? AND {PENDING} AND cancel_requested=1 AND next_attempt<=? LIMIT 1""",
        (seen_ids, device_id, now),
    ):
        # A new cancel for a previously sent operation is eligible again.
        # Yield at this chunk boundary and let normal round order pick it up.
        return None
    return store.all(
        f"""SELECT id,MAX(created,next_attempt) AS due,attempts
        FROM operations INDEXED BY op_delivery_device
        WHERE device_id=? AND {PENDING} AND next_attempt<=?
        AND id NOT IN (SELECT value FROM json_each(?))
        ORDER BY created,rowid LIMIT ?""",
        (device_id, now, seen_ids, limit),
    )
