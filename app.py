"""Flask application for combined warehouse/QC sealing verification."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from database import close_db, get_db, init_db, loads_conclusion, now_iso
from verifier import (
    TABLE_QC,
    TABLE_WAREHOUSE,
    ConflictError,
    VerificationError,
    action_label,
    analyze,
    decode_raw,
    parse_csv,
    table_label,
    validate_new_supplement,
)

WAREHOUSE_FIELD_NAMES = (
    "warehouse",
    "warehouse_file",
    "file_warehouse",
    "warehouse_csv",
    "kufang",
    "ku",
    "w",
    "库房",
    "库房csv",
    "库房CSV",
)
QC_FIELD_NAMES = (
    "qc",
    "qc_file",
    "file_qc",
    "qc_csv",
    "quality",
    "zhijian",
    "zhi",
    "q",
    "质检",
    "质检csv",
    "质检CSV",
)

WAREHOUSE_FILENAME_WORDS = (
    "warehouse",
    "kufang",
    "库房",
    "relabel",
    "shipment",
    "交运",
    "换签",
)
QC_FILENAME_WORDS = (
    "qc",
    "quality",
    "zhijian",
    "质检",
    "seal",
    "验封",
    "pass",
    "fail",
)


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    default_db = os.path.join(app.root_path, "data", "verification.db")
    app.config.from_mapping(
        DATABASE_PATH=os.environ.get("DATABASE_PATH", default_db),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        JSON_AS_ASCII=False,
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    app.json.ensure_ascii = False
    app.jinja_env.globals["action_label"] = action_label
    app.jinja_env.globals["table_label"] = table_label

    app.teardown_appcontext(close_db)

    with app.app_context():
        init_db()

    def _named_upload(names: tuple[str, ...]):
        for name in names:
            upload = request.files.get(name)
            if upload is not None and upload.filename:
                return upload
        return None

    def _filename_has(filename: str, words: tuple[str, ...]) -> bool:
        lowered = filename.lower()
        return any(word.lower() in lowered for word in words)

    def extract_upload_pair():
        warehouse = _named_upload(WAREHOUSE_FIELD_NAMES)
        qc = _named_upload(QC_FIELD_NAMES)

        # Accept conventional repeated "files" fields and arbitrary field names
        # such as file1/file2.
        all_uploads: list[Any] = []
        for field_name in request.files.keys():
            all_uploads.extend(request.files.getlist(field_name))
        valid_uploads = [
            upload for upload in all_uploads if upload is not None and upload.filename
        ]
        assigned = {id(upload) for upload in (warehouse, qc) if upload is not None}
        candidates = [upload for upload in valid_uploads if id(upload) not in assigned]

        for upload in candidates:
            name = upload.filename or ""
            if warehouse is None and _filename_has(name, WAREHOUSE_FILENAME_WORDS):
                warehouse = upload
            elif qc is None and _filename_has(name, QC_FILENAME_WORDS):
                qc = upload

        assigned = {id(upload) for upload in (warehouse, qc) if upload is not None}
        unused = [upload for upload in valid_uploads if id(upload) not in assigned]

        # Fall back to multipart order: first warehouse, second QC.
        if warehouse is None and unused:
            warehouse = unused.pop(0)
        if qc is None and unused:
            qc = unused.pop(0)
        if warehouse is None or qc is None:
            raise VerificationError("请同时上传库房 CSV 和质检 CSV 两份文件")
        return warehouse, qc

    def create_case_from_request() -> str:
        warehouse_file, qc_file = extract_upload_pair()
        warehouse_raw = warehouse_file.read()
        qc_raw = qc_file.read()

        warehouse_records = parse_csv(warehouse_raw, TABLE_WAREHOUSE)
        qc_records = parse_csv(qc_raw, TABLE_QC)
        records = warehouse_records + qc_records
        conclusion = analyze(records, [])

        case_id = uuid.uuid4().hex
        box_code = (request.form.get("box_code") if request.form else None) or ""
        box_code = box_code.strip() or f"BOX-{case_id[:8].upper()}"
        created_at = now_iso()
        db = get_db()

        try:
            cursor = db.execute(
                """
                INSERT INTO cases (
                    id, box_code, warehouse_filename, qc_filename,
                    raw_warehouse_csv, raw_qc_csv,
                    current_verdict, current_conclusion_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    case_id,
                    box_code,
                    warehouse_file.filename,
                    qc_file.filename,
                    decode_raw(warehouse_raw),
                    decode_raw(qc_raw),
                    conclusion["verdict"],
                    created_at,
                ),
            )

            for record in records:
                db.execute(
                    """
                    INSERT INTO records (
                        case_id, uid, table_name, record_no, minute_key,
                        minute_text, row_no, action_raw, kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        case_id,
                        record["uid"],
                        record["table"],
                        record["record_no"],
                        record["minute_key"],
                        record["minute_text"],
                        record["row_no"],
                        record["action_raw"],
                        record["kind"],
                    ),
                )

            payload = json.dumps(conclusion, ensure_ascii=False)
            conclusion_cursor = db.execute(
                """
                INSERT INTO conclusions (
                    case_id, verdict, compliant_possible,
                    noncompliant_possible, payload_json,
                    trigger_type, created_at
                ) VALUES (?, ?, ?, ?, ?, 'initial', ?)
                """,
                (
                    case_id,
                    conclusion["verdict"],
                    int(conclusion["compliant_possible"]),
                    int(conclusion["noncompliant_possible"]),
                    payload,
                    created_at,
                ),
            )
            db.execute(
                "UPDATE cases SET current_conclusion_id = ? WHERE id = ?",
                (conclusion_cursor.lastrowid, case_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return case_id

    def case_exists(case_id: str) -> bool:
        row = get_db().execute(
            "SELECT 1 FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        return row is not None

    def get_detail(case_id: str) -> dict[str, Any] | None:
        db = get_db()
        case_row = db.execute(
            "SELECT * FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        if case_row is None:
            return None
        case = dict(case_row)

        records = []
        for row in db.execute(
            "SELECT * FROM records WHERE case_id = ? ORDER BY id",
            (case_id,),
        ).fetchall():
            record = dict(row)
            # The verifier calls this field "table"; SQLite stores it as
            # table_name to avoid ambiguity in SQL.
            record["table"] = record["table_name"]
            records.append(record)
        supplements = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM supplements WHERE case_id = ? ORDER BY id",
                (case_id,),
            ).fetchall()
        ]

        conclusion_row = None
        if case.get("current_conclusion_id"):
            conclusion_row = db.execute(
                "SELECT * FROM conclusions WHERE id = ? AND case_id = ?",
                (case["current_conclusion_id"], case_id),
            ).fetchone()
        if conclusion_row is None:
            conclusion_row = db.execute(
                "SELECT * FROM conclusions WHERE case_id = ? ORDER BY id DESC LIMIT 1",
                (case_id,),
            ).fetchone()

        conclusion = loads_conclusion(conclusion_row)
        records_by_uid = {record["uid"]: record for record in records}
        if conclusion:
            for key in ("compliant_order", "noncompliant_order"):
                uids = conclusion.get(key)
                if uids is not None:
                    conclusion[f"{key}_items"] = [
                        records_by_uid[uid]
                        for uid in uids
                        if uid in records_by_uid
                    ]
                    conclusion[f"{key}_record_numbers"] = [
                        records_by_uid[uid]["record_no"]
                        for uid in uids
                        if uid in records_by_uid
                    ]

        history = [
            {
                "id": row["id"],
                "verdict": row["verdict"],
                "trigger_type": row["trigger_type"],
                "created_at": row["created_at"],
            }
            for row in db.execute(
                """
                SELECT id, verdict, trigger_type, created_at
                FROM conclusions
                WHERE case_id = ?
                ORDER BY id DESC
                LIMIT 30
                """,
                (case_id,),
            ).fetchall()
        ]

        return {
            "case": case,
            "records": records,
            "supplements": supplements,
            "conclusion": conclusion,
            "history": history,
        }

    def recent_cases(limit: int = 20):
        return [
            dict(row)
            for row in get_db()
            .execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM records r WHERE r.case_id = c.id)
                           AS record_count,
                       (SELECT COUNT(*) FROM supplements s WHERE s.case_id = c.id)
                           AS supplement_count
                FROM cases c
                ORDER BY c.created_at DESC, c.id DESC
                LIMIT ?
                """,
                (limit,),
            )
            .fetchall()
        ]

    def resolve_record_ref(case_id: str, ref: str | None, table: str | None = None) -> str:
        if not ref:
            raise VerificationError("请选择要补记先后的两条记录")
        ref = str(ref).strip()
        db = get_db()

        direct = db.execute(
            "SELECT uid FROM records WHERE case_id = ? AND uid = ?",
            (case_id, ref),
        ).fetchone()
        if direct:
            return direct["uid"]

        # Allow callers to pass the raw record number.  Prefixes W:/Q: are
        # also accepted even if the canonical prefix uses two colons.
        normalized_ref = ref
        for single, canonical in (("W:", "W::"), ("Q:", "Q::")):
            if normalized_ref.startswith(single) and not normalized_ref.startswith(canonical):
                normalized_ref = canonical + normalized_ref[len(single) :]
        direct = db.execute(
            "SELECT uid FROM records WHERE case_id = ? AND uid = ?",
            (case_id, normalized_ref),
        ).fetchone()
        if direct:
            return direct["uid"]

        rows = db.execute(
            "SELECT uid, table_name FROM records WHERE case_id = ? AND record_no = ?",
            (case_id, ref),
        ).fetchall()
        if table:
            rows = [row for row in rows if row["table_name"] == table]
        if len(rows) == 1:
            return rows[0]["uid"]
        if len(rows) > 1:
            raise VerificationError(
                f"记录号 {ref!r} 在两份流水中重复，请使用带表标识的 UID 或分别选择库房/质检记录"
            )
        raise VerificationError(f"补记引用了不存在的记录：{ref}")

    def supplement_input_from_request() -> tuple[str | None, str | None, str | None, str | None]:
        data = request.get_json(silent=True) if request.is_json else request.form
        data = data or {}

        before_ref = data.get("before_uid") or data.get("before") or data.get("before_record_no")
        after_ref = data.get("after_uid") or data.get("after") or data.get("after_record_no")
        before_table = data.get("before_table")
        after_table = data.get("after_table")

        if not before_ref or not after_ref:
            warehouse_uid = (
                data.get("warehouse_uid")
                or data.get("w_uid")
                or data.get("warehouse_record")
            )
            qc_uid = data.get("qc_uid") or data.get("q_uid") or data.get("qc_record")
            direction = (data.get("direction") or "w_before_q").strip()
            if warehouse_uid and qc_uid:
                if direction in (
                    "w_before_q",
                    "warehouse_before_qc",
                    "wq",
                    "warehouse_first",
                    "库房在前",
                ):
                    before_ref, after_ref = warehouse_uid, qc_uid
                    before_table, after_table = TABLE_WAREHOUSE, TABLE_QC
                else:
                    before_ref, after_ref = qc_uid, warehouse_uid
                    before_table, after_table = TABLE_QC, TABLE_WAREHOUSE
        return before_ref, after_ref, before_table, after_table

    def add_supplement_service(case_id: str):
        if not case_exists(case_id):
            abort(404)

        before_ref, after_ref, before_table, after_table = supplement_input_from_request()
        before_uid = resolve_record_ref(case_id, before_ref, before_table)
        after_uid = resolve_record_ref(case_id, after_ref, after_table)

        db = get_db()
        record_rows = db.execute(
            "SELECT * FROM records WHERE case_id = ? ORDER BY id",
            (case_id,),
        ).fetchall()
        records = []
        for row in record_rows:
            record = dict(row)
            record["table"] = record["table_name"]
            records.append(record)
        supplement_rows = db.execute(
            "SELECT * FROM supplements WHERE case_id = ? ORDER BY id",
            (case_id,),
        ).fetchall()
        existing = [
            {"before_uid": row["before_uid"], "after_uid": row["after_uid"]}
            for row in supplement_rows
        ]

        before_record = next(r for r in records if r["uid"] == before_uid)
        after_record = next(r for r in records if r["uid"] == after_uid)
        if before_record["table"] == after_record["table"]:
            raise VerificationError("补记仅支持跨表记录；同表顺序已按分钟和行序确定")

        duplicate = any(
            s["before_uid"] == before_uid and s["after_uid"] == after_uid
            for s in existing
        )
        reverse_exists = any(
            s["before_uid"] == after_uid and s["after_uid"] == before_uid
            for s in existing
        )
        if reverse_exists:
            raise ConflictError("与已有补记方向相反，补记冲突，已退回并沿用原结论")

        # Raises ConflictError/VerificationError without writing on failure.
        conclusion = validate_new_supplement(records, existing, before_uid, after_uid)

        if duplicate:
            detail = get_detail(case_id)
            return detail, True

        created_at = now_iso()
        try:
            db.execute(
                """
                INSERT INTO supplements (
                    case_id, before_uid, after_uid, before_table,
                    before_record_no, after_table, after_record_no, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    before_uid,
                    after_uid,
                    before_record["table"],
                    before_record["record_no"],
                    after_record["table"],
                    after_record["record_no"],
                    created_at,
                ),
            )
            payload = json.dumps(conclusion, ensure_ascii=False)
            conclusion_cursor = db.execute(
                """
                INSERT INTO conclusions (
                    case_id, verdict, compliant_possible,
                    noncompliant_possible, payload_json,
                    trigger_type, created_at
                ) VALUES (?, ?, ?, ?, ?, 'supplement', ?)
                """,
                (
                    case_id,
                    conclusion["verdict"],
                    int(conclusion["compliant_possible"]),
                    int(conclusion["noncompliant_possible"]),
                    payload,
                    created_at,
                ),
            )
            db.execute(
                "UPDATE cases SET current_verdict = ?, current_conclusion_id = ? WHERE id = ?",
                (
                    conclusion["verdict"],
                    conclusion_cursor.lastrowid,
                    case_id,
                ),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return get_detail(case_id), False

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/")
    def index():
        return render_template("index.html", cases=recent_cases(), error=None)

    @app.post("/cases")
    def create_case_html():
        try:
            case_id = create_case_from_request()
        except VerificationError as exc:
            return (
                render_template(
                    "index.html",
                    cases=recent_cases(),
                    error=str(exc),
                ),
                400,
            )
        return redirect(url_for("case_detail", case_id=case_id), code=303)

    @app.post("/api/cases")
    def create_case_api():
        try:
            case_id = create_case_from_request()
        except VerificationError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(get_detail(case_id)), 201

    @app.get("/cases/<case_id>")
    def case_detail(case_id: str):
        detail = get_detail(case_id)
        if detail is None:
            abort(404)
        added = request.args.get("added") == "1"
        return render_template(
            "case.html",
            **detail,
            error_message=None,
            conflict=False,
            added=added,
        )

    @app.post("/cases/<case_id>/supplements")
    def add_supplement_html(case_id: str):
        if not case_exists(case_id):
            abort(404)
        try:
            _detail, duplicate = add_supplement_service(case_id)
        except ConflictError as exc:
            detail = get_detail(case_id)
            return (
                render_template(
                    "case.html",
                    **detail,
                    error_message=str(exc),
                    conflict=True,
                    added=False,
                ),
                409,
            )
        except VerificationError as exc:
            detail = get_detail(case_id)
            return (
                render_template(
                    "case.html",
                    **detail,
                    error_message=str(exc),
                    conflict=False,
                    added=False,
                ),
                400,
            )
        target = url_for("case_detail", case_id=case_id)
        if not duplicate:
            target += "?added=1"
        return redirect(target, code=303)

    @app.get("/api/cases")
    def list_cases_api():
        return jsonify(cases=recent_cases(100))

    @app.get("/api/cases/<case_id>")
    def case_detail_api(case_id: str):
        detail = get_detail(case_id)
        if detail is None:
            return jsonify(error="案例不存在"), 404
        return jsonify(detail)

    @app.post("/api/cases/<case_id>/supplements")
    def add_supplement_api(case_id: str):
        if not case_exists(case_id):
            return jsonify(error="案例不存在"), 404
        try:
            detail, duplicate = add_supplement_service(case_id)
        except ConflictError as exc:
            return (
                jsonify(
                    error=str(exc),
                    conflict=True,
                    rejected=True,
                    state=get_detail(case_id),
                ),
                409,
            )
        except VerificationError as exc:
            return (
                jsonify(
                    error=str(exc),
                    rejected=True,
                    state=get_detail(case_id),
                ),
                400,
            )
        return jsonify({**detail, "duplicate": duplicate}), 200

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
