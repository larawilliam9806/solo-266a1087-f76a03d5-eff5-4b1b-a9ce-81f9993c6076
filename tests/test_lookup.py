import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "/workspace")

try:
    from app import create_app
except ImportError:  # Flask is only required for the web-layer tests.
    create_app = None


def warehouse_csv(rows):
    lines = ["记录号,分钟时间,动作"]
    lines.extend(f"{no},2026-11-24 {minute},{action}" for no, minute, action in rows)
    return "\n".join(lines) + "\n"


def qc_csv(rows):
    return warehouse_csv(rows)


# 8 warehouse + 6 qc = 14 records, all in the same minute so the initial
# verdict is ambiguous and both witness orders are long enough to exercise
# the +/-3 window truncation.
WAREHOUSE_ROWS = [(f"W-{i:03d}", "10:05", "换签") for i in range(1, 8)] + [
    ("W-008", "10:05", "交运")
]
QC_ROWS = [
    ("Q-001", "10:05", "验封通过"),
    ("Q-002", "10:05", "验封失败"),
    ("Q-003", "10:05", "验封通过"),
    ("Q-004", "10:05", "验封失败"),
    ("Q-005", "10:05", "验封通过"),
    ("Q-006", "10:05", "验封失败"),
]


@unittest.skipIf(create_app is None, "Flask is not installed")
class LookupFeatureTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="boxlookup-", suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.app = create_app(
            {"TESTING": True, "DATABASE_PATH": self.db_path}
        )
        self.client = self.app.test_client()
        self.traced_statements = []
        self._trace_conn = None

    def tearDown(self):
        self.detach_tracer()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def upload_case(self):
        response = self.client.post(
            "/api/cases",
            data={
                "box_code": "LOOKUP-BOX",
                "warehouse_file": (
                    io.BytesIO(warehouse_csv(WAREHOUSE_ROWS).encode()), "w.csv"
                ),
                "qc_file": (io.BytesIO(qc_csv(QC_ROWS).encode()), "q.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        detail = response.get_json()
        return detail["case"]["id"], detail["conclusion"]

    def attach_tracer(self):
        """Trace every connection the Flask app opens, across request contexts."""
        import sqlite3

        original_connect = sqlite3.connect
        statements = self.traced_statements

        def traced_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        self._original_connect = original_connect
        sqlite3.connect = traced_connect
        return sqlite3

    def detach_tracer(self):
        import sqlite3

        original = getattr(self, "_original_connect", None)
        if original is not None:
            sqlite3.connect = original
            self._original_connect = None

    def assert_no_full_table_reads(self):
        """The lookup flow may only use bounded, explicitly-listed columns."""
        normalized = [" ".join(s.lower().split()) for s in self.traced_statements]
        joined = "\n".join(normalized)
        # Full-order payload and raw ledger columns must never be named.
        for forbidden in (
            "payload_json",
            "raw_warehouse_csv",
            "raw_qc_csv",
        ):
            self.assertNotIn(forbidden, joined)
        # Star selects over the wide tables would silently pull them in.
        for stmt in normalized:
            if "from cases" in stmt or "from conclusions" in stmt:
                self.assertNotIn("select *", stmt)
            if "from order_indexes" in stmt:
                self.assertIn(" where ", stmt)
                self.assertNotIn("select * from order_indexes", stmt)
            if "from records" in stmt:
                self.assertIn(" where ", stmt)
                self.assertNotIn("select * from records", stmt)

    def test_lookup_page_link_and_open_without_full_orders(self):
        case_id, conclusion = self.upload_case()
        index_page = self.client.get("/").get_data(as_text=True)
        self.assertIn(f"/cases/{case_id}/lookup", index_page)

        # Opening the page must not read any order slot or full payload.
        self.attach_tracer()
        page = self.client.get(f"/cases/{case_id}/lookup")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("按纸质凭据记录号查阅", html)
        # Hidden field pins the page to this conclusion snapshot.
        self.assertIn(
            f'name="conclusion_id" value="{conclusion["id"]}"', html
        )
        self.assert_no_full_table_reads()
        # No order index slots are touched until a record number is submitted.
        self.assertNotIn("from order_indexes", "\n".join(self.traced_statements).lower())
        # The whole ledger is not preloaded onto the lookup page either.
        self.assertNotIn("W-008", html)

    def test_locate_returns_bounded_window_and_shipment_relation(self):
        case_id, conclusion = self.upload_case()
        cid = conclusion["id"]

        self.attach_tracer()
        response = self.client.get(
            f"/api/cases/{case_id}/lookup"
            f"?table=warehouse&record_no=W-001&conclusion_id={cid}"
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertEqual(result["conclusion_id"], cid)

        for kind in ("compliant", "noncompliant"):
            order = result["orders"][kind]
            if not order["exists"]:
                continue
            self.assertEqual(order["position"], 1)
            self.assertTrue(order["has_after_gap"])
            self.assertFalse(order["has_before_gap"])
            # At most target + three following slots.
            self.assertLessEqual(len(order["window"]), 4)
            self.assertTrue(
                all(item["position"] <= 4 for item in order["window"])
            )
            self.assertTrue(
                all(item["is_target"] is False or item["record_no"] == "W-001"
                    for item in order["window"])
            )

        # Shipment itself: relation must say "this record is the shipment".
        ship = self.client.get(
            f"/api/cases/{case_id}/lookup?table=warehouse&record_no=W-008"
        ).get_json()
        for kind in ("compliant", "noncompliant"):
            order = ship["orders"][kind]
            if order["exists"]:
                self.assertEqual(order["position"], order["shipment_position"])

        # A middle record gets up to three slots on each side.
        middle = self.client.get(
            f"/api/cases/{case_id}/lookup?table=qc&record_no=Q-003"
        ).get_json()
        for kind in ("compliant", "noncompliant"):
            order = middle["orders"][kind]
            if not order["exists"]:
                continue
            self.assertLessEqual(len(order["window"]), 7)
            positions = [item["position"] for item in order["window"]]
            self.assertEqual(positions, sorted(positions))
            self.assertIn(order["position"], positions)

    def test_locate_never_reads_full_order_or_payload(self):
        case_id, conclusion = self.upload_case()
        cid = conclusion["id"]
        self.attach_tracer()
        response = self.client.get(
            f"/api/cases/{case_id}/lookup?table=qc&record_no=Q-002"
            f"&conclusion_id={cid}"
        )
        self.assertEqual(response.status_code, 200)
        self.assert_no_full_table_reads()
        # No window may contain more than seven slots.
        result = response.get_json()
        for order in result["orders"].values():
            if order.get("exists"):
                self.assertLessEqual(len(order["window"]), 7)

    def test_missing_order_marked_absent(self):
        # Force an always-compliant verdict: the pass is in a strictly later
        # minute than the relabel and strictly before the shipment.
        w_rows = [
            ("W-001", "10:00", "换签"),
            ("W-002", "10:05", "交运"),
        ]
        q_rows = [("Q-001", "10:03", "验封通过")]
        response = self.client.post(
            "/api/cases",
            data={
                "warehouse_file": (
                    io.BytesIO(warehouse_csv(w_rows).encode()), "w.csv"
                ),
                "qc_file": (io.BytesIO(qc_csv(q_rows).encode()), "q.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        case_id = response.get_json()["case"]["id"]
        self.assertEqual(
            response.get_json()["conclusion"]["verdict"], "always_compliant"
        )
        result = self.client.get(
            f"/api/cases/{case_id}/lookup?table=warehouse&record_no=W-002"
        ).get_json()
        self.assertTrue(result["orders"]["compliant"]["exists"])
        self.assertFalse(result["orders"]["noncompliant"]["exists"])

        page = self.client.get(
            f"/cases/{case_id}/lookup?table=warehouse&record_no=W-002"
        ).get_data(as_text=True)
        self.assertIn("不存在可行的不合格顺序", page)
        self.assertIn('id="noncompliant-lookup-panel"', page)

    def test_input_errors(self):
        case_id, _ = self.upload_case()
        missing = self.client.get(
            f"/api/cases/{case_id}/lookup?table=warehouse&record_no=GHOST"
        )
        self.assertEqual(missing.status_code, 400)
        self.assertIn("找不到记录号", missing.get_json()["error"])

        bad_table = self.client.get(
            f"/api/cases/{case_id}/lookup?table=other&record_no=W-001"
        )
        self.assertEqual(bad_table.status_code, 400)

        no_number = self.client.get(
            f"/api/cases/{case_id}/lookup?table=warehouse&record_no="
        )
        self.assertEqual(no_number.status_code, 400)

        page = self.client.get(
            f"/cases/{case_id}/lookup?table=warehouse&record_no=GHOST"
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn("data-testid=\"lookup-error\"", page.get_data(as_text=True))

    def test_snapshot_pinned_after_supplement(self):
        case_id, initial = self.upload_case()
        old_id = initial["id"]

        # Open a lookup page pinned to the initial (ambiguous) conclusion.
        old_page = self.client.get(
            f"/cases/{case_id}/lookup?conclusion_id={old_id}"
        ).get_data(as_text=True)
        self.assertIn(f"结论 #{old_id}", old_page)

        # Supplement forces a new verdict and creates a new conclusion.
        supplement = self.client.post(
            f"/api/cases/{case_id}/supplements",
            json={"before_uid": "W::W-008", "after_uid": "Q::Q-005"},
        )
        self.assertEqual(supplement.status_code, 200, supplement.get_data(as_text=True))
        new_conclusion = supplement.get_json()["conclusion"]
        new_id = new_conclusion["id"]
        self.assertNotEqual(old_id, new_id)

        # Old pinned page still locates with the original conclusion.
        old_lookup = self.client.get(
            f"/api/cases/{case_id}/lookup"
            f"?table=warehouse&record_no=W-008&conclusion_id={old_id}"
        ).get_json()
        self.assertEqual(old_lookup["conclusion_id"], old_id)

        # Newly opened page uses the latest conclusion.
        fresh_page = self.client.get(f"/cases/{case_id}/lookup")
        self.assertEqual(fresh_page.status_code, 200)
        fresh_html = fresh_page.get_data(as_text=True)
        self.assertIn(f"结论 #{new_id}", fresh_html)
        self.assertIn("当前为最新结论", fresh_html)

        # The old snapshot page explicitly offers the latest-conclusion page.
        old_page2 = self.client.get(
            f"/cases/{case_id}/lookup?conclusion_id={old_id}"
        ).get_data(as_text=True)
        self.assertIn("打开最新结论的查阅页", old_page2)
        self.assertIn("此结论生成于补记重判之前", old_page2)

        # Full orders remain available on the case detail page.
        detail = self.client.get(f"/cases/{case_id}").get_data(as_text=True)
        self.assertIn('id="compliant-order"', detail)

    def test_legacy_conclusion_materialized_once_on_first_lookup(self):
        case_id, conclusion = self.upload_case()
        cid = conclusion["id"]

        # Simulate a conclusion written before this feature: no index rows,
        # status pending.
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM order_indexes WHERE conclusion_id = ?", (cid,))
        conn.execute(
            "UPDATE conclusions SET index_status = 'pending' WHERE id = ?", (cid,)
        )
        conn.commit()
        conn.close()

        page = self.client.get(f"/cases/{case_id}/lookup")
        self.assertEqual(page.status_code, 200)

        # First locate triggers the one-time materialisation by reading the
        # stored payload; afterwards lookups never touch the payload again.
        first = self.client.get(
            f"/api/cases/{case_id}/lookup?table=qc&record_no=Q-001"
        )
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        first_json = first.get_json()
        self.assertEqual(first_json["index_status"], "ready")
        self.assertTrue(first_json["orders"]["compliant"]["exists"])

        self.attach_tracer()
        second = self.client.get(
            f"/api/cases/{case_id}/lookup?table=warehouse&record_no=W-004"
        )
        self.assertEqual(second.status_code, 200)
        statements = "\n".join(self.traced_statements).lower()
        self.assertNotIn("payload_json", statements)

        # Index rows now cover every slot of both orders.
        import sqlite3 as _sq

        conn = _sq.connect(self.db_path)
        compliant_total = conn.execute(
            "SELECT COUNT(*) FROM order_indexes WHERE conclusion_id=? AND order_kind='compliant'",
            (cid,),
        ).fetchone()[0]
        self.assertEqual(compliant_total, conclusion["record_count"])
        conn.close()

    def test_failed_materialization_prompts_retry_then_succeeds(self):
        case_id, conclusion = self.upload_case()
        cid = conclusion["id"]
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM order_indexes WHERE conclusion_id = ?", (cid,))
        conn.execute(
            "UPDATE conclusions SET index_status='failed', index_error=? WHERE id=?",
            ("模拟整理失败", cid),
        )
        conn.commit()
        conn.close()

        page = self.client.get(f"/cases/{case_id}/lookup")
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("data-testid=\"index-failed\"", html)
        self.assertIn("模拟整理失败", html)
        self.assertIn("重试整理", html)

        # Locating while failed is refused with a retryable error.
        blocked = self.client.get(
            f"/api/cases/{case_id}/lookup?table=warehouse&record_no=W-001"
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertTrue(blocked.get_json()["retryable"])

        # Retry endpoint re-attempts materialisation and restores service.
        retry = self.client.post(
            f"/cases/{case_id}/lookup/reindex",
            data={"conclusion_id": cid},
        )
        self.assertEqual(retry.status_code, 303)
        ok = self.client.get(
            f"/api/cases/{case_id}/lookup?table=warehouse&record_no=W-001"
        )
        self.assertEqual(ok.status_code, 200, ok.get_data(as_text=True))
        self.assertEqual(ok.get_json()["index_status"], "ready")

        api_retry = self.client.post(
            f"/api/cases/{case_id}/lookup/reindex",
            json={"conclusion_id": cid},
        )
        self.assertEqual(api_retry.status_code, 200)

    def test_windows_match_full_witness_orders(self):
        case_id, conclusion = self.upload_case()
        full_orders = {
            "compliant": conclusion["compliant_order"],
            "noncompliant": conclusion["noncompliant_order"],
        }
        for record_no in ("W-003", "Q-004", "W-008", "Q-006"):
            table = "warehouse" if record_no.startswith("W") else "qc"
            result = self.client.get(
                f"/api/cases/{case_id}/lookup"
                f"?table={table}&record_no={record_no}"
            ).get_json()
            for kind, full_uids in full_orders.items():
                if full_uids is None:
                    self.assertFalse(result["orders"][kind]["exists"])
                    continue
                order = result["orders"][kind]
                expected_pos = full_uids.index(
                    ("W::" if table == "warehouse" else "Q::") + record_no
                ) + 1
                self.assertEqual(order["position"], expected_pos)
                expected_uids = set(
                    full_uids[
                        max(0, expected_pos - 4):
                        min(len(full_uids), expected_pos + 3)
                    ]
                )
                self.assertEqual(
                    {item["uid"] for item in order["window"]}, expected_uids
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
