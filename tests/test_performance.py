import sys
import time

sys.path.insert(0, "/workspace")

from verifier import (  # noqa: E402
    KIND_FAIL,
    KIND_PASS,
    KIND_RELABEL,
    KIND_SHIPMENT,
    analyze,
)


def test_hundreds_same_minute_are_fast():
    records = []
    minute = "202611241005"

    for row in range(1, 161):
        records.append(
            {
                "uid": f"W::W{row:04d}",
                "table": "warehouse",
                "record_no": f"W{row:04d}",
                "minute_key": minute,
                "minute_text": "2026-11-24 10:05",
                "row_no": row,
                "action_raw": "换签",
                "kind": KIND_RELABEL,
            }
        )
    # Shipment is the last warehouse row.
    records[-1]["kind"] = KIND_SHIPMENT
    records[-1]["action_raw"] = "交运"

    for row in range(1, 161):
        kind = KIND_PASS if row % 3 else KIND_FAIL
        records.append(
            {
                "uid": f"Q::Q{row:04d}",
                "table": "qc",
                "record_no": f"Q{row:04d}",
                "minute_key": minute,
                "minute_text": "2026-11-24 10:05",
                "row_no": row,
                "action_raw": "验封通过" if kind == KIND_PASS else "验封失败",
                "kind": kind,
            }
        )

    start = time.perf_counter()
    result = analyze(records, [])
    elapsed = time.perf_counter() - start
    assert result["verdict"] in {
        "ambiguous",
        "always_compliant",
        "always_noncompliant",
    }
    assert elapsed < 2.0, f"analysis too slow: {elapsed:.3f}s"
    print(f"320 same-minute records analyzed in {elapsed * 1000:.1f} ms")
    print("verdict:", result["verdict"])


if __name__ == "__main__":
    test_hundreds_same_minute_are_fast()
