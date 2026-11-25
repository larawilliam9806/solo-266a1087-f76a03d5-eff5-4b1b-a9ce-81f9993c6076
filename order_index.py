"""Lazy, point-query index for witness orders.

A conclusion payload stores two complete witness orders.  Loading an entire
long order just to locate one paper record number is wasteful, so every
conclusion also gets an ``order_indexes`` row per (order, position).  Lookups
then read only the target slot and at most three slots on either side.

New conclusions are indexed eagerly while they are still in memory.
Conclusions written by older versions have ``index_status = 'pending'`` and
are materialised at most once, on the first lookup.  A failure is recorded so
the operator gets a retry prompt instead of a silent fallback to full scans.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from verifier import VerificationError

ORDER_COMPLIANT = "compliant"
ORDER_NONCOMPLIANT = "noncompliant"

ORDER_KINDS = (ORDER_COMPLIANT, ORDER_NONCOMPLIANT)

STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

WINDOW_RADIUS = 3


class OrderIndexError(Exception):
    """Raised when a conclusion cannot be indexed or located."""


def _insert_rows(
    db: sqlite3.Connection,
    conclusion_id: int,
    payload: dict[str, Any],
) -> None:
    rows: list[tuple[int, str, int, str]] = []
    for order_kind in ORDER_KINDS:
        uids = payload.get(f"{order_kind}_order")
        if not uids:
            continue
        rows.extend(
            (conclusion_id, order_kind, position, uid)
            for position, uid in enumerate(uids, start=1)
        )
    if rows:
        db.executemany(
            """
            INSERT OR IGNORE INTO order_indexes
                (conclusion_id, order_kind, position, uid)
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )


def index_new_conclusion(
    db: sqlite3.Connection,
    conclusion_id: int,
    payload: dict[str, Any],
) -> None:
    """Index a conclusion created inside the current transaction.

    The payload is already in memory, so this never re-reads the full order
    from disk.  Runs in the caller's transaction; a rollback undoes it.
    """
    _insert_rows(db, conclusion_id, payload)
    db.execute(
        "UPDATE conclusions SET index_status = ?, index_error = NULL WHERE id = ?",
        (STATUS_READY, conclusion_id),
    )


def get_index_status(db: sqlite3.Connection, conclusion_id: int) -> str | None:
    row = db.execute(
        "SELECT index_status FROM conclusions WHERE id = ?",
        (conclusion_id,),
    ).fetchone()
    return row["index_status"] if row is not None else None


def materialize_legacy_conclusion(
    db: sqlite3.Connection,
    conclusion_id: int,
) -> str:
    """Materialise a pre-indexing conclusion exactly once.

    Returns the resulting status (``ready``/``failed``/``pending``).  Only the
    first lookup triggers this work; a failed attempt keeps its error message
    until the explicit retry endpoint runs this again.
    """
    row = db.execute(
        "SELECT index_status, payload_json FROM conclusions WHERE id = ?",
        (conclusion_id,),
    ).fetchone()
    if row is None:
        raise OrderIndexError("结论不存在")
    status = row["index_status"] or STATUS_PENDING
    if status == STATUS_READY:
        return STATUS_READY
    if status == STATUS_FAILED:
        return STATUS_FAILED

    try:
        payload = json.loads(row["payload_json"])
        _insert_rows(db, conclusion_id, payload)
        db.execute(
            "UPDATE conclusions SET index_status = ?, index_error = NULL WHERE id = ?",
            (STATUS_READY, conclusion_id),
        )
        db.commit()
        return STATUS_READY
    except Exception as exc:  # pragma: no cover - defensive persistence guard
        db.rollback()
        try:
            db.execute(
                "UPDATE conclusions SET index_status = ?, index_error = ? WHERE id = ?",
                (STATUS_FAILED, str(exc)[:500], conclusion_id),
            )
            db.commit()
        except Exception:
            db.rollback()
        return STATUS_FAILED


def retry_materialization(
    db: sqlite3.Connection,
    conclusion_id: int,
) -> str:
    """Force another materialisation attempt after a previous failure."""
    row = db.execute(
        "SELECT index_status FROM conclusions WHERE id = ?",
        (conclusion_id,),
    ).fetchone()
    if row is None:
        raise OrderIndexError("结论不存在")
    if row["index_status"] == STATUS_READY:
        return STATUS_READY
    db.execute(
        "UPDATE conclusions SET index_status = ?, index_error = NULL WHERE id = ?",
        (STATUS_PENDING, conclusion_id),
    )
    db.commit()
    return materialize_legacy_conclusion(db, conclusion_id)


def ensure_indexed(db: sqlite3.Connection, conclusion_id: int) -> str:
    """Make sure a conclusion is indexed; never silently scans full orders."""
    status = get_index_status(db, conclusion_id)
    if status is None:
        raise OrderIndexError("结论不存在")
    if status == STATUS_PENDING:
        return materialize_legacy_conclusion(db, conclusion_id)
    return status


def order_exists(
    db: sqlite3.Connection, conclusion_id: int, order_kind: str
) -> bool:
    row = db.execute(
        """
        SELECT 1 FROM order_indexes
        WHERE conclusion_id = ? AND order_kind = ?
        LIMIT 1
        """,
        (conclusion_id, order_kind),
    ).fetchone()
    return row is not None


def order_length(
    db: sqlite3.Connection, conclusion_id: int, order_kind: str
) -> int:
    row = db.execute(
        """
        SELECT COUNT(*) AS n FROM order_indexes
        WHERE conclusion_id = ? AND order_kind = ?
        """,
        (conclusion_id, order_kind),
    ).fetchone()
    return int(row["n"])


def locate_in_order(
    db: sqlite3.Connection,
    case_id: str,
    conclusion_id: int,
    order_kind: str,
    uid: str,
) -> dict[str, Any] | None:
    """Locate one record inside one indexed order.

    Reads at most ``2 * WINDOW_RADIUS + 1`` order slots plus the window's
    record rows.  Returns ``None`` when the order does not exist; the target
    slot itself cannot be missing for a valid persisted conclusion.
    """
    target = db.execute(
        """
        SELECT position FROM order_indexes
        WHERE conclusion_id = ? AND order_kind = ? AND uid = ?
        """,
        (conclusion_id, order_kind, uid),
    ).fetchone()
    if target is None:
        # Distinguish "order does not exist" from "uid not in this order".
        if not order_exists(db, conclusion_id, order_kind):
            return None
        raise OrderIndexError(
            f"索引缺少记录 {uid} 在 {order_kind} 顺序中的位置，请重试整理旧结论"
        )

    target_position = int(target["position"])
    total = order_length(db, conclusion_id, order_kind)
    lo = max(1, target_position - WINDOW_RADIUS)
    hi = min(total, target_position + WINDOW_RADIUS)

    slot_rows = db.execute(
        """
        SELECT o.position, o.uid,
               r.table_name, r.record_no, r.minute_text, r.row_no, r.kind
        FROM order_indexes o
        LEFT JOIN records r
               ON r.case_id = ? AND r.uid = o.uid
        WHERE o.conclusion_id = ? AND o.order_kind = ?
          AND o.position BETWEEN ? AND ?
        ORDER BY o.position
        """,
        (case_id, conclusion_id, order_kind, lo, hi),
    ).fetchall()

    return {
        "order_kind": order_kind,
        "exists": True,
        "total": total,
        "position": target_position,
        "window_start": lo,
        "window_end": hi,
        "has_before_gap": lo > 1,
        "has_after_gap": hi < total,
        "window": [
            {
                "position": row["position"],
                "is_target": row["position"] == target_position,
                "uid": row["uid"],
                "table_name": row["table_name"],
                "record_no": row["record_no"],
                "minute_text": row["minute_text"],
                "row_no": row["row_no"],
                "kind": row["kind"],
            }
            for row in slot_rows
        ],
    }


def resolve_record(
    db: sqlite3.Connection,
    case_id: str,
    table_name: str,
    record_no: str,
) -> dict[str, Any] | None:
    """Point lookup of one record by table + paper record number."""
    row = db.execute(
        """
        SELECT uid, table_name, record_no, minute_text, row_no, kind
        FROM records
        WHERE case_id = ? AND table_name = ? AND record_no = ?
        """,
        (case_id, table_name, record_no.strip()),
    ).fetchone()
    return dict(row) if row is not None else None


def shipment_position(
    db: sqlite3.Connection, conclusion_id: int, order_kind: str, shipment_uid: str
) -> int | None:
    row = db.execute(
        """
        SELECT position FROM order_indexes
        WHERE conclusion_id = ? AND order_kind = ? AND uid = ?
        """,
        (conclusion_id, order_kind, shipment_uid),
    ).fetchone()
    return int(row["position"]) if row is not None else None


def build_lookup(
    db: sqlite3.Connection,
    case_id: str,
    conclusion_id: int,
    table_name: str,
    record_no: str,
) -> dict[str, Any]:
    """Assemble the full bounded lookup response for one paper record."""
    status = ensure_indexed(db, conclusion_id)
    if status != STATUS_READY:
        raise OrderIndexError("旧结论顺序索引尚未整理完成，请稍后重试")

    record = resolve_record(db, case_id, table_name, record_no)
    if record is None:
        raise VerificationError(
            f"在{'库房' if table_name == 'warehouse' else '质检'}流水中找不到记录号 {record_no!r}"
        )

    shipment = db.execute(
        "SELECT uid FROM records WHERE case_id = ? AND kind = 'shipment'",
        (case_id,),
    ).fetchone()
    shipment_uid = shipment["uid"] if shipment else None

    orders: dict[str, dict[str, Any]] = {}
    for order_kind in ORDER_KINDS:
        located = locate_in_order(
            db, case_id, conclusion_id, order_kind, record["uid"]
        )
        if located is None:
            orders[order_kind] = {"order_kind": order_kind, "exists": False}
            continue
        located["shipment_position"] = (
            shipment_position(
                db, conclusion_id, order_kind, shipment_uid
            )
            if shipment_uid
            else None
        )
        orders[order_kind] = located

    return {
        "case_id": case_id,
        "conclusion_id": conclusion_id,
        "index_status": STATUS_READY,
        "table_name": table_name,
        "record_no": record_no.strip(),
        "target": record,
        "shipment_uid": shipment_uid,
        "orders": orders,
    }


def indexed_conclusion_ids(db: sqlite3.Connection) -> Iterable[int]:
    return [
        row["id"]
        for row in db.execute(
            "SELECT id FROM conclusions WHERE index_status = 'ready'"
        ).fetchall()
    ]


# Re-exported for callers that only need the status vocabulary.
__all__ = [
    "ORDER_COMPLIANT",
    "ORDER_NONCOMPLIANT",
    "ORDER_KINDS",
    "STATUS_PENDING",
    "STATUS_READY",
    "STATUS_FAILED",
    "WINDOW_RADIUS",
    "OrderIndexError",
    "VerificationError",
    "index_new_conclusion",
    "get_index_status",
    "materialize_legacy_conclusion",
    "retry_materialization",
    "ensure_indexed",
    "order_exists",
    "order_length",
    "locate_in_order",
    "resolve_record",
    "shipment_position",
    "build_lookup",
    "indexed_conclusion_ids",
]
