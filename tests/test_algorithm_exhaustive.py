import itertools
import random
import sys

sys.path.insert(0, "/workspace")

from verifier import (  # noqa: E402
    KIND_FAIL,
    KIND_PASS,
    KIND_RELABEL,
    KIND_SHIPMENT,
    analyze,
    build_graph,
    compute_closure,
    kahn_toposort,
)


def brute_force(records, supplements):
    adj, index = build_graph(records, supplements)
    n = len(records)

    def follows(order):
        pos = {node: i for i, node in enumerate(order)}
        return all(pos[u] < pos[v] for u in range(n) for v in adj[u])

    ship = next(i for i, r in enumerate(records) if r["kind"] == KIND_SHIPMENT)
    c = False
    bad_n = False
    any_ext = False
    for order in itertools.permutations(range(n)):
        if not follows(order):
            continue
        any_ext = True
        p = order.index(ship)
        if p == 0:
            bad_n = True
            continue
        prev = order[p - 1]
        if records[prev]["kind"] == KIND_PASS:
            c = True
        elif records[prev]["kind"] in (KIND_RELABEL, KIND_FAIL):
            bad_n = True
    assert any_ext
    return c, bad_n


def make_records(spec):
    # spec entries: table, minute, row, kind
    out = []
    counters = {}
    for table, minute, kind in spec:
        counters[(table, minute)] = counters.get((table, minute), 0) + 1
        row = counters[(table, minute)]
        rid = f"{table[0].upper()}{minute}-{row}"
        out.append(
            {
                "uid": ("W::" if table == "W" else "Q::") + rid,
                "table": "warehouse" if table == "W" else "qc",
                "record_no": rid,
                "minute_key": f"20260101{minute:02d}00",
                "minute_text": f"m{minute}",
                "row_no": row,
                "action_raw": kind,
                "kind": kind,
            }
        )
    return out


def test_fixed_examples():
    always_good = make_records(
        [
            ("W", 0, KIND_RELABEL),
            ("Q", 1, KIND_FAIL),
            ("Q", 2, KIND_PASS),
            ("W", 3, KIND_SHIPMENT),
        ]
    )
    result = analyze(always_good, [])
    assert result["verdict"] == "always_compliant", result

    always_bad = make_records(
        [
            ("Q", 0, KIND_PASS),
            ("Q", 1, KIND_FAIL),
            ("W", 2, KIND_RELABEL),
            ("W", 3, KIND_SHIPMENT),
        ]
    )
    result = analyze(always_bad, [])
    assert result["verdict"] == "always_noncompliant", result

    ambiguous = make_records(
        [
            ("W", 5, KIND_RELABEL),
            ("W", 5, KIND_SHIPMENT),
            ("Q", 5, KIND_PASS),
            ("Q", 5, KIND_FAIL),
        ]
    )
    result = analyze(ambiguous, [])
    assert result["verdict"] == "ambiguous", result
    assert result["compliant_order"] and result["noncompliant_order"]


def test_exhaustive_random():
    rng = random.Random(12345)
    kinds_w = [KIND_RELABEL] * 3 + [KIND_SHIPMENT]
    kinds_q = [KIND_PASS, KIND_FAIL]
    checked = 0
    for case in range(3000):
        nw = rng.randint(1, 3)
        nq = rng.randint(0, 3)
        w_kinds = [KIND_RELABEL] * max(0, nw - 1) + [KIND_SHIPMENT]
        rng.shuffle(w_kinds[:-1])
        spec = []
        for k in w_kinds:
            spec.append(("W", rng.randint(0, 2), k))
        for _ in range(nq):
            spec.append(("Q", rng.randint(0, 2), rng.choice(kinds_q)))
        records = make_records(spec)
        n = len(records)

        # Generate DAG supplements by choosing edges that follow a random total.
        adj, index = build_graph(records, [])
        topo = kahn_toposort(adj)
        rank = {node: pos for pos, node in enumerate(rng.sample(topo, len(topo)))}
        # Random rank may violate base; instead use a random linear extension.
        extension = topo
        rank = {node: pos for pos, node in enumerate(extension)}
        supplements = []
        uids = [r["uid"] for r in records]
        for _ in range(rng.randint(0, 3)):
            if n < 2:
                continue
            a, b = rng.sample(range(n), 2)
            if records[a]["table"] == records[b]["table"]:
                continue
            if rank[a] > rank[b]:
                a, b = b, a
            supplements.append({"before_uid": uids[a], "after_uid": uids[b]})
        try:
            result = analyze(records, supplements)
            algo_c = result["compliant_possible"]
            algo_n = result["noncompliant_possible"]
            brute_c, brute_n = brute_force(records, supplements)
            assert (algo_c, algo_n) == (brute_c, brute_n), (
                case,
                [(r["uid"], r["minute_key"], r["row_no"], r["kind"]) for r in records],
                supplements,
                (algo_c, algo_n),
                (brute_c, brute_n),
            )
            # Witness validity.
            for key in ("compliant_order", "noncompliant_order"):
                uid_order = result[key]
                if uid_order is None:
                    continue
                pos = {uid: i for i, uid in enumerate(uid_order)}
                for supp in supplements:
                    assert pos[supp["before_uid"]] < pos[supp["after_uid"]]
                adj2, idx2 = build_graph(records, supplements)
                for u, vs in enumerate(adj2):
                    for v in vs:
                        assert pos[records[u]["uid"]] < pos[records[v]["uid"]]
                ship_pos = pos[result["shipment_uid"]]
                if key == "compliant_order":
                    assert ship_pos > 0
                    prev_uid = uid_order[ship_pos - 1]
                    prev = next(r for r in records if r["uid"] == prev_uid)
                    assert prev["kind"] == KIND_PASS
                else:
                    if ship_pos > 0:
                        prev_uid = uid_order[ship_pos - 1]
                        prev = next(r for r in records if r["uid"] == prev_uid)
                        assert prev["kind"] in (KIND_RELABEL, KIND_FAIL)
            checked += 1
        except Exception:
            print("FAILED CASE", case)
            raise
    print(f"exhaustive random checks passed: {checked}")


if __name__ == "__main__":
    test_fixed_examples()
    test_exhaustive_random()
    print("all verifier tests passed")
