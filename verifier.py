"""Core verification logic for exhibition-box sealing checks.

The module is intentionally independent of Flask and SQLite so the decision
procedure can be tested directly.  It uses a partial order:

* different minutes are ordered by minute;
* records in the same table and same minute are ordered by CSV row;
* cross-table records in the same minute are incomparable unless a supplement
  supplies their order.

Because every accepted action is one of pass/fail/relabel/shipment, a compliant
sequence is exactly one in which the record immediately before shipment is a
seal pass.  The final pass is then also the last relevant event before
shipment.  Possibility is tested with bitset reachability rather than by
enumerating permutations.
"""

from __future__ import annotations

import csv
import heapq
import io
import re
from typing import Any, Iterable

TABLE_WAREHOUSE = "warehouse"
TABLE_QC = "qc"

KIND_RELABEL = "relabel"
KIND_SHIPMENT = "shipment"
KIND_PASS = "pass"
KIND_FAIL = "fail"

VERDICT_COMPLIANT = "always_compliant"
VERDICT_NONCOMPLIANT = "always_noncompliant"
VERDICT_AMBIGUOUS = "ambiguous"

VERDICT_TEXT = {
    VERDICT_COMPLIANT: "必然合格",
    VERDICT_NONCOMPLIANT: "必然不合格",
    VERDICT_AMBIGUOUS: "两者都有可能",
}


class VerificationError(Exception):
    """Base class for user-fixable input/verification errors."""


class CsvParseError(VerificationError):
    pass


class ConflictError(VerificationError):
    pass


def make_uid(table: str, record_no: str) -> str:
    prefix = "W" if table == TABLE_WAREHOUSE else "Q"
    return f"{prefix}::{record_no}"


def _normalise_header(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"[\s_\-—–/\\()（）【】\[\]:：.。,，'\"“”‘’、]+", "", text)
    return text.strip().lower()


_HEADER_ALIASES = {
    "record": {
        "记录号",
        "记录编号",
        "记录号码",
        "编号",
        "流水号",
        "单号",
        "recordid",
        "recordno",
        "recordnumber",
        "id",
        "no",
    },
    "minute": {
        "分钟时间",
        "分钟",
        "时间",
        "日期时间",
        "记录时间",
        "datetime",
        "timestamp",
        "time",
        "minute",
        "minutetime",
    },
    "action": {
        "动作",
        "操作",
        "事件",
        "动作类型",
        "结果",
        "action",
        "event",
        "type",
        "actiontype",
    },
}

_ACTION_ALIASES = {
    "换签": KIND_RELABEL,
    "换标签": KIND_RELABEL,
    "更换标签": KIND_RELABEL,
    "改签": KIND_RELABEL,
    "relabel": KIND_RELABEL,
    "labelchange": KIND_RELABEL,
    "changelabel": KIND_RELABEL,
    "交运": KIND_SHIPMENT,
    "交运出库": KIND_SHIPMENT,
    "装运": KIND_SHIPMENT,
    "发运": KIND_SHIPMENT,
    "发货": KIND_SHIPMENT,
    "ship": KIND_SHIPMENT,
    "shipped": KIND_SHIPMENT,
    "shipment": KIND_SHIPMENT,
    "验封通过": KIND_PASS,
    "封验通过": KIND_PASS,
    "核验通过": KIND_PASS,
    "检查通过": KIND_PASS,
    "通过": KIND_PASS,
    "合格": KIND_PASS,
    "pass": KIND_PASS,
    "passed": KIND_PASS,
    "success": KIND_PASS,
    "验封失败": KIND_FAIL,
    "封验失败": KIND_FAIL,
    "核验失败": KIND_FAIL,
    "检查失败": KIND_FAIL,
    "失败": KIND_FAIL,
    "不合格": KIND_FAIL,
    "fail": KIND_FAIL,
    "failed": KIND_FAIL,
    "failure": KIND_FAIL,
}

_ALLOWED_KINDS_BY_TABLE = {
    TABLE_WAREHOUSE: {KIND_RELABEL, KIND_SHIPMENT},
    TABLE_QC: {KIND_PASS, KIND_FAIL},
}

_TABLE_LABEL = {
    TABLE_WAREHOUSE: "库房",
    TABLE_QC: "质检",
}

_KIND_LABEL = {
    KIND_RELABEL: "换签",
    KIND_SHIPMENT: "交运",
    KIND_PASS: "验封通过",
    KIND_FAIL: "验封失败",
}


def action_label(kind: str) -> str:
    return _KIND_LABEL.get(kind, kind)


def table_label(table: str) -> str:
    return _TABLE_LABEL.get(table, table)


def decode_raw(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise CsvParseError(f"无法识别 CSV 编码：{last_error}")


def parse_minute(value: Any) -> tuple[str, str]:
    """Return a lexicographically sortable minute key and canonical label."""
    raw = str(value or "").strip()
    if not raw:
        raise CsvParseError("分钟时间为空")

    year: int | None = None
    month = day = 1
    compact = re.fullmatch(r"\s*(\d{12})(?:\d{2})?\s*", raw)
    full = None if compact else re.search(
        r"(\d{4})\D{1,4}(\d{1,2})\D{1,4}(\d{1,2})\D{1,4}(\d{1,2}):(\d{2})(?::\d{2})?",
        raw,
    )
    no_year = None
    clock = None

    if compact:
        digits = compact.group(1)
        year, month, day, hour, minute = (
            int(digits[0:4]),
            int(digits[4:6]),
            int(digits[6:8]),
            int(digits[8:10]),
            int(digits[10:12]),
        )
    elif full:
        year, month, day, hour, minute = map(int, full.groups())
    else:
        no_year = re.search(
            r"(\d{1,2})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2}):(\d{2})(?::\d{2})?",
            raw,
        )
        if no_year:
            month, day, hour, minute = map(int, no_year.groups())
        else:
            clock = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*", raw)
            if not clock:
                raise CsvParseError(
                    f"无法解析分钟时间 {raw!r}，应为 YYYY-MM-DD HH:MM、MM-DD HH:MM 或 HH:MM"
                )
            hour, minute = map(int, clock.groups())

    try:
        if not (1 <= month <= 12 and 1 <= day <= 31):
            raise ValueError
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        # Validate calendar date.  Year 2000 handles Feb 29 for year-less data.
        import datetime as _dt

        validation_year = year if year is not None else 2000
        _dt.datetime(validation_year, month, day, hour, minute)
        if year is None and no_year:
            key = f"0000{month:02d}{day:02d}{hour:02d}{minute:02d}"
            label = f"{month:02d}-{day:02d} {hour:02d}:{minute:02d}"
        elif year is None:
            key = f"1900{month:02d}{day:02d}{hour:02d}{minute:02d}"
            label = f"{hour:02d}:{minute:02d}"
        else:
            key = f"{year:04d}{month:02d}{day:02d}{hour:02d}{minute:02d}"
            label = f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"
    except ValueError as exc:
        raise CsvParseError(f"分钟时间 {raw!r} 不是有效时间") from exc

    return key, label


def normalise_action(raw: Any, table: str) -> str:
    text = str(raw or "").strip()
    compact = _normalise_header(text)
    kind = _ACTION_ALIASES.get(compact)
    if kind is None:
        # Also try the uncompact Chinese text after trimming only spaces.
        kind = _ACTION_ALIASES.get(text.replace(" ", ""))
    if kind is None:
        allowed = "、".join(_KIND_LABEL[k] for k in sorted(_ALLOWED_KINDS_BY_TABLE[table]))
        raise CsvParseError(
            f"{_TABLE_LABEL[table]} CSV 中存在无法识别的动作 {text!r}；本表只允许：{allowed}"
        )
    if kind not in _ALLOWED_KINDS_BY_TABLE[table]:
        allowed = "、".join(_KIND_LABEL[k] for k in sorted(_ALLOWED_KINDS_BY_TABLE[table]))
        raise CsvParseError(
            f"动作 {text!r} 不属于{_TABLE_LABEL[table]}流水；该表只允许：{allowed}"
        )
    return kind


def parse_csv(raw: bytes | str, table: str) -> list[dict[str, Any]]:
    """Parse one ledger CSV into record dictionaries."""
    if table not in _TABLE_LABEL:
        raise VerificationError(f"未知表类型：{table}")

    text = decode_raw(raw)
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = ","
    if first_line.count("\t") > first_line.count(","):
        delimiter = "\t"
    elif first_line.count(";") > first_line.count(","):
        delimiter = ";"
    elif first_line.count("，") > first_line.count(","):
        delimiter = "，"
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(str(cell).strip() for cell in row)]
    if not rows:
        raise CsvParseError(f"{_TABLE_LABEL[table]} CSV 没有任何内容")

    header_index: dict[str, int] = {}
    for idx, cell in enumerate(rows[0]):
        header_index[_normalise_header(cell)] = idx

    field_indexes: dict[str, int | None] = {}
    for field, aliases in _HEADER_ALIASES.items():
        found = None
        for alias in aliases:
            candidate = _normalise_header(alias)
            if candidate in header_index:
                found = header_index[candidate]
                break
        field_indexes[field] = found

    if all(v is not None for v in field_indexes.values()):
        data_rows = rows[1:]
    else:
        # Headerless CSV: record number, minute, action in positional order.
        data_rows = rows
        field_indexes = {"record": 0, "minute": 1, "action": 2}

    records: list[dict[str, Any]] = []
    seen_numbers: set[str] = set()

    for data_pos, row in enumerate(data_rows, start=1):
        def cell(field: str) -> str:
            idx = field_indexes[field]
            return str(row[idx]).strip() if idx is not None and idx < len(row) else ""

        record_no = cell("record")
        if not record_no:
            raise CsvParseError(f"{_TABLE_LABEL[table]} CSV 第 {data_pos} 条数据缺少记录号")
        if record_no in seen_numbers:
            raise CsvParseError(
                f"{_TABLE_LABEL[table]} CSV 中记录号 {record_no!r} 重复；记录号在同一份流水中须唯一"
            )
        seen_numbers.add(record_no)

        minute_raw = cell("minute")
        minute_key, minute_label = parse_minute(minute_raw)
        action_raw = cell("action")
        if not action_raw:
            raise CsvParseError(
                f"{_TABLE_LABEL[table]} CSV 记录 {record_no!r} 缺少动作"
            )
        kind = normalise_action(action_raw, table)

        records.append(
            {
                "uid": make_uid(table, record_no),
                "table": table,
                "record_no": record_no,
                "minute_key": minute_key,
                "minute_text": minute_label,
                "row_no": data_pos,
                "action_raw": action_raw,
                "kind": kind,
            }
        )

    if not records:
        raise CsvParseError(f"{_TABLE_LABEL[table]} CSV 缺少数据行")
    return records


def validate_case_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    records = list(records)
    shipments = [r for r in records if r["kind"] == KIND_SHIPMENT]
    if len(shipments) != 1:
        raise VerificationError(
            f"库房流水中必须恰好有一笔交运，当前有 {len(shipments)} 笔"
        )
    uids = [r["uid"] for r in records]
    if len(uids) != len(set(uids)):
        raise VerificationError("两份 CSV 组合后的内部记录标识重复")
    return shipments[0]


def build_graph(
    records: list[dict[str, Any]],
    supplements: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[set[int]], dict[str, int]]:
    """Build the sparse precedence graph.

    O(n) base edges are used.  Each table is a chain; at every minute boundary
    the last record of each table in the earlier minute is connected to the
    first record of each table in the next minute.  Transitivity then gives all
    required earlier-minute/later-minute precedences.
    """
    n = len(records)
    index = {r["uid"]: i for i, r in enumerate(records)}
    adj: list[set[int]] = [set() for _ in range(n)]

    def add_edge(u: int, v: int) -> None:
        if u == v:
            raise ConflictError("同一条记录不能补记为自身在前")
        adj[u].add(v)

    by_table: dict[str, list[tuple[str, int, int]]] = {
        TABLE_WAREHOUSE: [],
        TABLE_QC: [],
    }
    minute_tables: dict[str, dict[str, list[int]]] = {}

    for i, record in enumerate(records):
        table = record["table"]
        if table not in by_table:
            raise VerificationError(f"未知表类型：{table}")
        by_table[table].append((record["minute_key"], record["row_no"], i))
        minute_tables.setdefault(record["minute_key"], {}).setdefault(table, []).append(i)

    # Same-table total order: minute first, CSV row as same-minute tie breaker.
    for table_rows in by_table.values():
        table_rows.sort(key=lambda item: (item[0], item[1]))
        for (_, _, u), (_, _, v) in zip(table_rows, table_rows[1:]):
            add_edge(u, v)

    minute_keys = sorted(minute_tables)
    for prev_key, next_key in zip(minute_keys, minute_keys[1:]):
        prev_groups = minute_tables[prev_key]
        next_groups = minute_tables[next_key]
        tails: list[int] = []
        heads: list[int] = []
        for table in (TABLE_WAREHOUSE, TABLE_QC):
            prev_indices = prev_groups.get(table)
            if prev_indices:
                ordered = sorted(
                    prev_indices,
                    key=lambda i: records[i]["row_no"],
                )
                tails.append(ordered[-1])
            next_indices = next_groups.get(table)
            if next_indices:
                ordered = sorted(
                    next_indices,
                    key=lambda i: records[i]["row_no"],
                )
                heads.append(ordered[0])
        for u in tails:
            for v in heads:
                add_edge(u, v)

    for supplement in supplements or []:
        before_uid = supplement.get("before_uid") or make_uid(
            supplement["before_table"], supplement["before_record_no"]
        )
        after_uid = supplement.get("after_uid") or make_uid(
            supplement["after_table"], supplement["after_record_no"]
        )
        if before_uid not in index or after_uid not in index:
            raise VerificationError("补记引用了不存在的记录")
        add_edge(index[before_uid], index[after_uid])

    return adj, index


def kahn_toposort(adj: list[set[int]]) -> list[int]:
    n = len(adj)
    indegree = [0] * n
    for u, neighbours in enumerate(adj):
        for v in neighbours:
            indegree[v] += 1

    ready = [u for u in range(n) if indegree[u] == 0]
    heapq.heapify(ready)
    order: list[int] = []

    while ready:
        u = heapq.heappop(ready)
        order.append(u)
        for v in adj[u]:
            indegree[v] -= 1
            if indegree[v] == 0:
                heapq.heappush(ready, v)

    if len(order) != n:
        raise ConflictError("补记的先后关系与已有时间、行序或其他补记形成了环，存在冲突")
    return order


def compute_closure(
    adj: list[set[int]], topo: list[int]
) -> tuple[list[int], list[int]]:
    """Return descendant and ancestor bitsets (bit i corresponds to node i)."""
    n = len(adj)
    reach = [0] * n
    for u in reversed(topo):
        bits = 0
        for v in adj[u]:
            bits |= (1 << v) | reach[v]
        reach[u] = bits

    ancestors = [0] * n
    for u in topo:
        u_and_ancestors = ancestors[u] | (1 << u)
        for v in adj[u]:
            ancestors[v] |= u_and_ancestors
    return reach, ancestors


def can_be_adjacent(
    first: int,
    second: int,
    reach: list[int],
    ancestors: list[int],
) -> bool:
    """Whether first can be immediately followed by second in some extension.

    Necessary and sufficient conditions in a poset: second must not already
    precede first, and no node may be forced strictly between them.
    """
    if first == second:
        return False
    if (reach[second] >> first) & 1:
        return False
    forced_between = reach[first] & ancestors[second]
    return forced_between == 0


def contracted_adjacent_order(
    adj: list[set[int]],
    first: int,
    second: int,
) -> list[int] | None:
    """Construct a deterministic linear extension where first,second are adjacent."""
    n = len(adj)
    if first == second:
        return None
    block = first

    def mapped(node: int) -> int:
        return block if node == second else node

    contracted: dict[int, set[int]] = {
        i: set() for i in range(n) if i != second
    }
    indegree = {i: 0 for i in contracted}

    for u in range(n):
        uu = mapped(u)
        for v in adj[u]:
            vv = mapped(v)
            if uu == vv:
                # Internal first -> second constraint, or a contradiction that
                # the reachability test has already rejected.
                continue
            if vv not in contracted[uu]:
                contracted[uu].add(vv)
                indegree[vv] += 1

    ready = [node for node, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    result: list[int] = []

    while ready:
        u = heapq.heappop(ready)
        if u == block:
            result.extend([first, second])
        else:
            result.append(u)
        for v in contracted[u]:
            indegree[v] -= 1
            if indegree[v] == 0:
                heapq.heappush(ready, v)

    if len(result) != n:
        return None
    return result


def toposort_with_forced_first(
    adj: list[set[int]], forced_first: int
) -> list[int] | None:
    n = len(adj)
    indegree = [0] * n
    for u, neighbours in enumerate(adj):
        for v in neighbours:
            indegree[v] += 1
    if indegree[forced_first] != 0:
        return None

    result = [forced_first]
    indegree[forced_first] = -1
    for v in adj[forced_first]:
        indegree[v] -= 1

    ready = [
        i
        for i in range(n)
        if i != forced_first and indegree[i] == 0
    ]
    heapq.heapify(ready)
    while ready:
        u = heapq.heappop(ready)
        result.append(u)
        for v in adj[u]:
            indegree[v] -= 1
            if indegree[v] == 0:
                heapq.heappush(ready, v)

    if len(result) != n:
        return None
    return result


def uid_order(records: list[dict[str, Any]], indexes: list[int]) -> list[str]:
    return [records[i]["uid"] for i in indexes]


def analyze(
    records: list[dict[str, Any]],
    supplements: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute the three-way verdict and witness orders."""
    records = list(records)
    supplement_list = list(supplements or [])
    shipment_record = validate_case_records(records)
    adj, index = build_graph(records, supplement_list)
    topo = kahn_toposort(adj)
    reach, ancestors = compute_closure(adj, topo)
    shipment = index[shipment_record["uid"]]

    passes = [i for i, r in enumerate(records) if r["kind"] == KIND_PASS]
    bads = [
        i
        for i, r in enumerate(records)
        if r["kind"] in (KIND_RELABEL, KIND_FAIL)
    ]

    compliant_indexes: list[int] | None = None
    compliant_witness: int | None = None
    for candidate in passes:
        if not can_be_adjacent(candidate, shipment, reach, ancestors):
            continue
        order = contracted_adjacent_order(adj, candidate, shipment)
        if order is not None:
            compliant_indexes = order
            compliant_witness = candidate
            break

    noncompliant_indexes: list[int] | None = None
    noncompliant_witness: int | None = None
    for candidate in bads:
        if not can_be_adjacent(candidate, shipment, reach, ancestors):
            continue
        order = contracted_adjacent_order(adj, candidate, shipment)
        if order is not None:
            noncompliant_indexes = order
            noncompliant_witness = candidate
            break

    shipment_first_witness = False
    if noncompliant_indexes is None and ancestors[shipment] == 0:
        order = toposort_with_forced_first(adj, shipment)
        if order is not None:
            noncompliant_indexes = order
            shipment_first_witness = True

    compliant_possible = compliant_indexes is not None
    noncompliant_possible = noncompliant_indexes is not None

    if compliant_possible and noncompliant_possible:
        verdict = VERDICT_AMBIGUOUS
    elif compliant_possible:
        verdict = VERDICT_COMPLIANT
    elif noncompliant_possible:
        verdict = VERDICT_NONCOMPLIANT
    else:
        # Every acyclic poset has a linear extension; with the constrained
        # action vocabulary one of the two witnesses must exist.
        raise VerificationError("无法构造核查结论：偏序图存在未预期的状态")

    return {
        "verdict": verdict,
        "verdict_text": VERDICT_TEXT[verdict],
        "compliant_possible": compliant_possible,
        "noncompliant_possible": noncompliant_possible,
        "compliant_order": uid_order(records, compliant_indexes)
        if compliant_indexes is not None
        else None,
        "noncompliant_order": uid_order(records, noncompliant_indexes)
        if noncompliant_indexes is not None
        else None,
        "shipment_uid": records[shipment]["uid"],
        "compliant_witness_uid": records[compliant_witness]["uid"]
        if compliant_witness is not None
        else None,
        "noncompliant_witness_uid": records[noncompliant_witness]["uid"]
        if noncompliant_witness is not None
        else (records[shipment]["uid"] if shipment_first_witness else None),
        "shipment_first_witness": shipment_first_witness,
        "record_count": len(records),
        "supplement_count": len(supplement_list),
    }


def validate_new_supplement(
    records: list[dict[str, Any]],
    existing: Iterable[dict[str, Any]],
    before_uid: str,
    after_uid: str,
) -> dict[str, Any]:
    """Validate one proposed cross-table supplement and return its analysis.

    Raises VerificationError/ConflictError.  The caller is responsible for not
    persisting anything when an exception is raised.
    """
    index = {r["uid"]: i for i, r in enumerate(records)}
    if before_uid not in index or after_uid not in index:
        raise VerificationError("请选择两条已上传的记录")
    if before_uid == after_uid:
        raise VerificationError("不能补记同一条记录的先后")

    before_record = next(r for r in records if r["uid"] == before_uid)
    after_record = next(r for r in records if r["uid"] == after_uid)
    if before_record["table"] == after_record["table"]:
        raise VerificationError("补记仅用于跨表记录；同表顺序已由分钟和 CSV 行序确定")

    existing_list = list(existing)
    for supp in existing_list:
        existing_before = supp.get("before_uid")
        existing_after = supp.get("after_uid")
        if existing_before == before_uid and existing_after == after_uid:
            # Idempotent redundant submission.
            return analyze(records, existing_list)
        if existing_before == after_uid and existing_after == before_uid:
            raise ConflictError("与已有补记方向相反，构成冲突")

    trial = existing_list + [
        {"before_uid": before_uid, "after_uid": after_uid}
    ]
    # analyze() builds the graph and runs Kahn; cycles raise ConflictError.
    return analyze(records, trial)
