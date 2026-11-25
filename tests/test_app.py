import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "/workspace")

try:
    from app import create_app
except ImportError:  # Flask is only required for these web-layer tests.
    create_app = None


WAREHOUSE_CSV = """记录号,分钟时间,动作
W-001,2026-11-24 10:00,换签
W-002,2026-11-24 10:05,交运
"""

QC_CSV = """记录号,分钟时间,动作
Q-001,2026-11-24 10:05,验封通过
"""


@unittest.skipIf(create_app is None, "Flask is not installed")
class FlaskAppTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="boxcheck-", suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.app = create_app(
            {"TESTING": True, "DATABASE_PATH": self.db_path}
        )
        self.client = self.app.test_client()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def upload_case(self):
        return self.client.post(
            "/api/cases",
            data={
                "box_code": "TEST-BOX",
                "warehouse_file": (io.BytesIO(WAREHOUSE_CSV.encode()), "w.csv"),
                "qc_file": (io.BytesIO(QC_CSV.encode()), "q.csv"),
            },
            content_type="multipart/form-data",
        )

    def test_health_upload_ambiguous_and_persistence(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.get_json()["status"], "ok")
        self.assertEqual(self.client.get("/").status_code, 200)

        response = self.upload_case()
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        detail = response.get_json()
        self.assertEqual(detail["case"]["box_code"], "TEST-BOX")
        conclusion = detail["conclusion"]
        self.assertEqual(conclusion["verdict"], "ambiguous")
        self.assertTrue(conclusion["compliant_order"])
        self.assertTrue(conclusion["noncompliant_order"])
        case_id = detail["case"]["id"]

        fetched = self.client.get(f"/api/cases/{case_id}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.get_json()["conclusion"]["verdict"], "ambiguous")

        page = self.client.get(f"/cases/{case_id}")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("两者都有可能", html)
        self.assertIn('id="compliant-order"', html)
        self.assertIn('id="noncompliant-order"', html)
        self.assertIn("W-002", html)

    def test_supplement_resolves_and_conflict_keeps_state(self):
        detail = self.upload_case().get_json()
        case_id = detail["case"]["id"]

        # Shipment before the same-minute pass => pass is post-shipment.
        ok = self.client.post(
            f"/api/cases/{case_id}/supplements",
            json={
                "before_uid": "W::W-002",
                "after_uid": "Q::Q-001",
            },
        )
        self.assertEqual(ok.status_code, 200, ok.get_data(as_text=True))
        ok_detail = ok.get_json()
        self.assertEqual(
            ok_detail["conclusion"]["verdict"], "always_noncompliant"
        )
        self.assertEqual(len(ok_detail["supplements"]), 1)

        # Opposite relation conflicts and must not replace saved state.
        conflict = self.client.post(
            f"/api/cases/{case_id}/supplements",
            json={
                "before_uid": "Q::Q-001",
                "after_uid": "W::W-002",
            },
        )
        self.assertEqual(conflict.status_code, 409)
        body = conflict.get_json()
        self.assertTrue(body["conflict"])
        self.assertEqual(len(body["state"]["supplements"]), 1)
        self.assertEqual(
            body["state"]["conclusion"]["verdict"], "always_noncompliant"
        )

        persisted = self.client.get(f"/api/cases/{case_id}").get_json()
        self.assertEqual(len(persisted["supplements"]), 1)
        self.assertEqual(
            persisted["conclusion"]["verdict"], "always_noncompliant"
        )
        # Initial + accepted supplement; rejected attempt adds no conclusion.
        self.assertEqual(len(persisted["history"]), 2)

    def test_compliant_supplement_and_html_conflict(self):
        detail = self.upload_case().get_json()
        case_id = detail["case"]["id"]

        ok = self.client.post(
            f"/cases/{case_id}/supplements",
            data={
                "warehouse_uid": "W::W-002",
                "qc_uid": "Q::Q-001",
                "direction": "q_before_w",
            },
        )
        self.assertEqual(ok.status_code, 303)
        page = self.client.get(ok.headers["Location"])
        self.assertIn("必然合格", page.get_data(as_text=True))

        conflict = self.client.post(
            f"/cases/{case_id}/supplements",
            data={
                "warehouse_uid": "W::W-002",
                "qc_uid": "Q::Q-001",
                "direction": "w_before_q",
            },
        )
        self.assertEqual(conflict.status_code, 409)
        html = conflict.get_data(as_text=True)
        self.assertIn("补记被退回", html)
        self.assertIn("必然合格", html)
        self.assertIn('data-conflict="true"', html)

    def test_invalid_csv_returns_400(self):
        response = self.client.post(
            "/api/cases",
            data={
                "warehouse_file": (
                    io.BytesIO("记录号,分钟时间,动作\nW1,bad-time,换签".encode()),
                    "w.csv",
                ),
                "qc_file": (io.BytesIO(QC_CSV.encode()), "q.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("无法解析", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
