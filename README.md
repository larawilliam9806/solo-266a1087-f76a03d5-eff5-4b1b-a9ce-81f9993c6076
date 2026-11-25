# 借展箱交运验封核查（Flask + SQLite + Docker Compose）

把同一只借展箱的两份分钟级 CSV 流水合并成一个偏序，判断交运环节：

- **必然合格**（`always_compliant`）
- **必然不合格**（`always_noncompliant`）
- **两者都有可能**（`ambiguous`），并分别给出一条可行的合格/不合格记录号顺序

支持在页面上补记一条**跨表记录先后关系**后重新判定。补记与分钟、行序或已有补记冲突时返回 `409`/页面错误，不写入冲突补记，原有补记和当前结论保持不变。

## 启动

```bash
docker compose up --build
```

打开 <http://localhost:5000>。健康检查：

```bash
curl http://localhost:5000/health
```

SQLite 数据库保存在 Compose 命名卷 `box-seal-data` 中，容器内路径为 `/app/data/verification.db`。

也可以同时使用 `docker-compose.yml`；`compose.yaml` 内容相同，兼容不同 Compose 调用习惯。

## 本地直接运行

```bash
pip install -r requirements.txt
python app.py
# 默认 http://127.0.0.1:5000
```

可通过 `DATABASE_PATH` 和 `PORT` 环境变量修改数据库路径与端口。

## CSV 格式

两份文件都应包含三列，推荐使用中文表头：

```csv
记录号,分钟时间,动作
W-001,2026-11-24 10:00,换签
W-002,2026-11-24 10:05,交运
```

质检：

```csv
记录号,分钟时间,动作
Q-001,2026-11-24 10:05,验封通过
```

支持的动作及常见别名：

| 表 | 标准动作 | 内部类型 |
|---|---|---|
| 库房 | `换签` | `relabel` |
| 库房 | `交运`（恰好一笔） | `shipment` |
| 质检 | `验封通过` | `pass` |
| 质检 | `验封失败` | `fail` |

时间支持 `YYYY-MM-DD HH:MM`、`YYYY/MM/DD HH:MM`、中文日期、`MM-DD HH:MM`、`HH:MM`、12 位紧凑时间；秒会被截到分钟。编码支持 UTF-8/UTF-8 BOM/GB18030，分隔符支持逗号、分号和 Tab。

## 判定规则

已知顺序：

1. 不同分钟：按分钟时间；
2. 同表同分钟：按 CSV 行序；
3. 跨表同分钟：默认未知，可由补记增加先后约束；
4. 补记还可以明确任意两条跨表记录的先后；若与偏序形成环，则视为冲突并退回。

合格条件是：交运前有验封通过，且**最后一次通过到交运之间没有换签或验封失败**。

由于所有动作只属于 `通过 / 失败 / 换签 / 交运`，在一条完整可行顺序中：

- 交运的前一条是“验封通过” ⇒ 合格；
- 交运的前一条是“换签/验封失败”，或交运是第一条 ⇒ 不合格。

系统不会枚举同分钟的排列数，而是构造偏序图并用位集计算可达关系。对候选记录 `x` 和交运 `s`，`x` 能在某条线性扩展中紧邻 `s` 当且仅当：

- `s` 不会必然在 `x` 之前；
- 不存在记录 `y` 被强制满足 `x < y < s`。

随后通过收缩 `(x, s)` 节点并进行拓扑排序，构造一条真实可行的完整记录号顺序。对每个“通过”和“坏事件”最多构造一次见证顺序，因此几百条同分钟记录也能快速完成。

## HTTP API

### 创建核查案例

```bash
CID=$(curl -sS -X POST http://localhost:5000/api/cases \
  -F box_code=BOX-017 \
  -F warehouse_file=@samples/warehouse.csv \
  -F qc_file=@samples/qc.csv \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["case"]["id"])')

echo "$CID"
curl -sS "http://localhost:5000/api/cases/$CID"
```

返回的 `conclusion` 包含：

```json
{
  "verdict": "ambiguous",
  "verdict_text": "两者都有可能",
  "compliant_possible": true,
  "noncompliant_possible": true,
  "compliant_order": ["W::W-001", "Q::Q-001", "W::W-002"],
  "noncompliant_order": ["W::W-001", "W::W-002", "Q::Q-001"],
  "compliant_order_record_numbers": ["W-001", "Q-001", "W-002"],
  "noncompliant_order_record_numbers": ["W-001", "W-002", "Q-001"]
}
```

UID 使用 `W::记录号` 和 `Q::记录号`，即使两份 CSV 中存在相同记录号也能区分。

### 补记跨表先后

让交运 `W-002` 早于质检通过 `Q-001`：

```bash
curl -i -X POST "http://localhost:5000/api/cases/$CID/supplements" \
  -H 'Content-Type: application/json' \
  -d '{"before_uid":"W::W-002","after_uid":"Q::Q-001"}'
```

再提交反方向会得到 `409 Conflict`，响应中的 `state` 是未被修改的旧状态；重新查询也会看到补记和结论历史均未增加冲突项。

页面表单使用：

- `warehouse_uid`
- `qc_uid`
- `direction=w_before_q|q_before_w`

普通 HTML 提交地址为 `POST /cases/<case_id>/supplements`。

## SQLite 表

- `cases`：箱号、原始 CSV 文本、当前结论指针；
- `records`：解析后的流水、表别、记录号、分钟键、行序、动作类型；
- `supplements`：已接受的跨表先后补记；
- `conclusions`：初次上传及每次成功补记后的结论 JSON 历史。

## 测试

```bash
python tests/test_algorithm_exhaustive.py   # 与全排列暴力判定随机对照
python tests/test_performance.py            # 320 条同分钟记录性能检查
python tests/test_app.py                    # Flask/SQLite/冲突退回测试（需安装 Flask）
```

## 目录

```text
app.py          Flask 路由、上传、持久化与页面/API
verifier.py     CSV 解析、偏序、可达性、三态判定和见证顺序
database.py     SQLite schema/连接辅助
templates/      服务端渲染页面
static/style.css
samples/        一组可直接上传的歧义样例
tests/          算法、性能和接口测试
Dockerfile
compose.yaml
docker-compose.yml
```
