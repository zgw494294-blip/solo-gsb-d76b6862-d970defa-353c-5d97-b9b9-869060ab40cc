# sample-fragment-service

保留触发前后样本的 HTTP 服务（Python + FastAPI，状态仅存于单进程内存）。

- 创建会话时指定历史长度 `H` 与样本容量 `C`（`1 ≤ H ≤ C ≤ 1000`）。
- 逐条写入整数样本，序号从 1 开始连续编号。
- 触发以**当前末条**为中心生成片段：前 `before` 条 + 中心 + 后 `after` 条
  （`before`、`after` 为非负整数，`before < H`，且 `before + 1 + after ≤ C`）。
  历史不足返回 `409`；`after > 0` 时片段处于 `pending`，后续样本到齐后变为 `completed`。
- 片段可重叠。查询片段返回 `pending` / `completed` 状态，以及按序号升序排列的序号和值。
- **容量核算**：最近 `H` 条历史与所有未删除片段引用的样本共同占用容量 `C`，
  同一序号只计一次（并集）；仅回收两者都不再需要的样本。
  新样本无处容纳时写入返回 `409`，**不消耗序号、不改变历史或片段**。
- 删除片段只解除该片段的引用，回收后写入可重试；已完成片段在删除前内容不变。
- **运行中调整 H/C**：`PUT /sessions/{sid}/limits` 不停采样地同时调整 `history` 与 `capacity`
  （必须是整数，`1 ≤ H ≤ C ≤ 1000`；布尔值、字符串、浮点等非整数返回 `422`）。
  新历史窗口要求的最近 `min(H, 已写入条数)` 条必须全部仍存活，且所有未删除片段的总长、
  新历史与片段已写入部分的序号并集均不得超过新 `C`，否则返回 `409` 且**不改变任何状态**。
  成功则立即按新参数回收不再需要的样本；片段及其内容、样本与片段编号、模式配置、
  匹配进度与命中记录均不变，**扩容不会恢复已回收的样本**。旧片段的 `before` 即使
  不小于新 `H` 也原样保留，仅后续触发按新参数校验。
- 会话内所有操作按会话级锁串行执行，等效于某个串行顺序。
- **整数模式识别（可选）**：会话可在**首条样本写入前**配置一次长度 `1～32` 的整数
  pattern，此后按成功写入的连续样本值精确匹配，**允许重叠**（pattern 可以长于 `H` 和 `C`）。
  识别只保存流式自动机的标量状态，**不保存原始样本、不占用样本容量**；片段触发、片段删除、
  样本回收都不改变识别进度，命中记录在回收后依然保留。容量拒绝不推进匹配状态，重试成功只识别一次。

## 启动

单条命令（Docker Compose）：

```bash
docker compose up --build
```

- 访问地址：http://localhost:8000
- 交互式接口文档（Swagger UI）：http://localhost:8000/docs
- 端口：`8000`（宿主机 `8000` → 容器 `8000`，可在 `docker-compose.yml` 中调整）

也可以只用 Docker：

```bash
docker build -t sample-fragment-service .
docker run --rm -p 8000:8000 sample-fragment-service
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sessions` | 创建会话，指定 `history`(H)、`capacity`(C) |
| GET | `/sessions/{sid}` | 查看会话状态（已用容量、存活序号、片段列表） |
| PUT | `/sessions/{sid}/limits` | 不停采样地调整 `history`(H) 与 `capacity`(C)，返回会话状态 |
| POST | `/sessions/{sid}/samples` | 写入一条整数样本 |
| PUT | `/sessions/{sid}/pattern` | 在首条样本写入前配置识别 pattern（1～32 个整数，仅一次） |
| GET | `/sessions/{sid}/pattern/hits` | 查询累计命中数与最近 100 次命中的 `start_seq`/`end_seq` |
| POST | `/sessions/{sid}/triggers` | 以当前末条为中心触发片段 |
| GET | `/sessions/{sid}/fragments` | 列出所有未删除片段 |
| GET | `/sessions/{sid}/fragments/{fid}` | 查询片段状态与样本内容 |
| DELETE | `/sessions/{sid}/fragments/{fid}` | 删除片段（解除引用并回收样本） |

错误码：`404` 会话/片段不存在；`409` 历史不足、容量不足、pattern 重复配置或已有样本后配置、
查询未配置 pattern 的会话、调整参数与存活样本或片段冲突（新历史窗口含已回收样本、
未删除片段总长或所需序号并集超过新 `C`）；`400` 触发参数违反 `before < H` 或
`before+1+after ≤ C`；`422` 请求体不合法（含 pattern 长度非法、元素为布尔值或字符串，
以及调整参数为布尔值/非整数或不满足 `1 ≤ H ≤ C ≤ 1000`）。

## 调用示例

### 1. 创建会话（H=2，C=3）

```bash
curl -s -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"history": 2, "capacity": 3}'
# {"session_id":1,"history":2,"capacity":3}
```

### 2. 逐条写入样本

```bash
for v in 10 20 30; do
  curl -s -X POST http://localhost:8000/sessions/1/samples \
    -H 'Content-Type: application/json' -d "{\"value\": $v}"; echo
done
# {"seq":1,"value":10}
# {"seq":2,"value":20}
# {"seq":3,"value":30}
```

历史窗口只保留最近 H=2 条，序号 1 已被回收：

```bash
curl -s http://localhost:8000/sessions/1
# {"session_id":1,"history":2,"capacity":3,"next_seq":4,"used":2,"stored_seqs":[2,3],"fragments":[]}
```

### 3. 触发片段（中心=当前末条 3，前 1 条 + 中心 + 后 1 条）

```bash
curl -s -X POST http://localhost:8000/sessions/1/triggers \
  -H 'Content-Type: application/json' \
  -d '{"before": 1, "after": 1}'
# {"fragment_id":1,"center":3,"before":1,"after":1,"start":2,"end":4,"status":"pending"}
```

### 4. 查询片段：pending → completed

```bash
curl -s http://localhost:8000/sessions/1/fragments/1
# {"fragment_id":1,...,"status":"pending","samples":[{"seq":2,"value":20},{"seq":3,"value":30}]}

curl -s -X POST http://localhost:8000/sessions/1/samples \
  -H 'Content-Type: application/json' -d '{"value": 40}'
# {"seq":4,"value":40}   —— 片段所需的后续样本到齐

curl -s http://localhost:8000/sessions/1/fragments/1
# {"fragment_id":1,...,"status":"completed",
#  "samples":[{"seq":2,"value":20},{"seq":3,"value":30},{"seq":4,"value":40}]}
```

### 5. 容量不足 → 409，删除后重试成功

片段 1 仍引用序号 2–4，历史窗口需要 3–4，并集 {2,3,4} 已占满 C=3，
再写一条需要 {2,3,4,5} 共 4 个位置，超出容量：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/sessions/1/samples \
  -H 'Content-Type: application/json' -d '{"value": 50}'
# 409   —— 序号未消耗，next_seq 仍为 5

curl -s -X DELETE http://localhost:8000/sessions/1/fragments/1 -o /dev/null -w '%{http_code}\n'
# 204   —— 解除引用，序号 2 被回收

curl -s -X POST http://localhost:8000/sessions/1/samples \
  -H 'Content-Type: application/json' -d '{"value": 50}'
# {"seq":5,"value":50}   —— 回收后重试成功，序号连续
```

### 6. 历史不足 → 409

```bash
curl -s -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' -d '{"history": 2, "capacity": 3}'
# {"session_id":2,...}

curl -s -X POST http://localhost:8000/sessions/2/triggers \
  -H 'Content-Type: application/json' -d '{"before": 1, "after": 0}'
# 409 {"detail":"历史不足：已有样本不足以覆盖中心及其前 before 条"}
```

### 7. 配置识别 pattern（首条样本写入前，仅一次）

```bash
curl -s -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' -d '{"history": 2, "capacity": 3}'
# {"session_id":3,"history":2,"capacity":3}

curl -s -X PUT http://localhost:8000/sessions/3/pattern \
  -H 'Content-Type: application/json' -d '{"pattern": [1, 1]}'
# {"pattern":[1,1]}
```

非法配置返回 `422`（长度须为 1～32，元素必须是整数，布尔值/字符串均拒绝）；
重复配置、或已有样本后配置返回 `409`；未知会话返回 `404`：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X PUT http://localhost:8000/sessions/3/pattern \
  -H 'Content-Type: application/json' -d '{"pattern": []}'
# 422

curl -s -o /dev/null -w '%{http_code}\n' -X PUT http://localhost:8000/sessions/3/pattern \
  -H 'Content-Type: application/json' -d '{"pattern": [1, true, "x"]}'
# 422

curl -s -o /dev/null -w '%{http_code}\n' -X PUT http://localhost:8000/sessions/3/pattern \
  -H 'Content-Type: application/json' -d '{"pattern": [1, 1]}'
# 409   —— 重复配置
```

### 8. 重叠命中与查询

连续写入三个 `1`，pattern `[1,1]` 允许重叠，命中两次：`(1,2)` 与 `(2,3)`。
识别不依赖存活样本——序号 1 虽已被历史窗口回收，命中记录仍保留：

```bash
for v in 1 1 1; do
  curl -s -X POST http://localhost:8000/sessions/3/samples \
    -H 'Content-Type: application/json' -d "{\"value\": $v}" > /dev/null
done

curl -s http://localhost:8000/sessions/3/pattern/hits
# {"pattern":[1,1],"hit_count":2,
#  "recent_hits":[{"start_seq":1,"end_seq":2},{"start_seq":2,"end_seq":3}]}
```

返回 `hit_count`（累计命中总数，初始为 0）和 `recent_hits`（最近 100 次命中，
按 `start_seq` 升序，初始为空列表）。未配置 pattern 的会话查询返回 `409`：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/sessions/2/pattern/hits
# 409
```

### 9. 容量拒绝后重试：只识别一次

容量不足的写入返回 `409` 时匹配状态不推进、不产生命中；解除引用、回收后用相同值
重试成功，该样本只参与一次识别：

```bash
# 会话 3：片段引用序号 2–4，历史窗口需要 3–4，并集占满 C=3
curl -s -X POST http://localhost:8000/sessions/3/triggers \
  -H 'Content-Type: application/json' -d '{"before": 1, "after": 1}' > /dev/null
curl -s -X POST http://localhost:8000/sessions/3/samples \
  -H 'Content-Type: application/json' -d '{"value": 2}' > /dev/null   # seq=4

# seq=5 需要 4 个位置 -> 409；被拒绝的值不会进入识别自动机
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/sessions/3/samples \
  -H 'Content-Type: application/json' -d '{"value": 1}'
# 409

curl -s -X DELETE http://localhost:8000/sessions/3/fragments/1 -o /dev/null -w '%{http_code}\n'
# 204   —— 回收后重试成功，pattern [1,1] 会在后续样本流中继续匹配，且不会补算被拒绝的写入
curl -s -X POST http://localhost:8000/sessions/3/samples \
  -H 'Content-Type: application/json' -d '{"value": 1}'
# {"seq":5,"value":1}
```

### 10. 运行中调整 H/C：成功收缩

会话 4 以 `H=3, C=5` 创建并写入 5 条样本，历史窗口只保留最近 3 条：

```bash
curl -s -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' -d '{"history": 3, "capacity": 5}'
# {"session_id":4,"history":3,"capacity":5}

for v in 10 20 30 40 50; do
  curl -s -X POST http://localhost:8000/sessions/4/samples \
    -H 'Content-Type: application/json' -d "{\"value\": $v}" > /dev/null
done

curl -s http://localhost:8000/sessions/4
# {"session_id":4,"history":3,"capacity":5,"next_seq":6,"used":3,"stored_seqs":[3,4,5],"fragments":[]}
```

收缩为 `H=2, C=3`：新窗口 `{4,5}` 全部存活、无片段、并集大小 2 ≤ 3，校验通过，
立即按新参数回收序号 3，返回会话查询格式：

```bash
curl -s -X PUT http://localhost:8000/sessions/4/limits \
  -H 'Content-Type: application/json' -d '{"history": 2, "capacity": 3}'
# {"session_id":4,"history":2,"capacity":3,"next_seq":6,"used":2,"stored_seqs":[4,5],"fragments":[]}
```

### 11. 扩大历史失败 → 409，状态不变

会话 4 的序号 1–3 已被回收。把 `H` 扩大为 4 需要序号 2–5 全部存活，
其中 2、3 已不存在，返回 `409` 且会话参数与样本保持原样：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X PUT http://localhost:8000/sessions/4/limits \
  -H 'Content-Type: application/json' -d '{"history": 4, "capacity": 5}'
# 409   —— 新历史窗口包含已回收样本，拒绝且不改变任何状态

curl -s http://localhost:8000/sessions/4
# {"session_id":4,"history":2,"capacity":3,"next_seq":6,"used":2,"stored_seqs":[4,5],"fragments":[]}
```

布尔值、字符串等非整数参数返回 `422`，不存在的会话返回 `404`：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X PUT http://localhost:8000/sessions/4/limits \
  -H 'Content-Type: application/json' -d '{"history": true, "capacity": 3}'
# 422

curl -s -o /dev/null -w '%{http_code}\n' -X PUT http://localhost:8000/sessions/99/limits \
  -H 'Content-Type: application/json' -d '{"history": 1, "capacity": 1}'
# 404
```

### 12. 扩容后重试写入

会话 5（`H=2, C=3`）的片段 1 引用序号 2–4，与历史窗口的并集占满 `C=3`，
写入第 5 条被 `409` 拒绝（序号未消耗）。把 `C` 扩容为 4 后重试成功；
扩容只放宽上限，不会恢复已回收的序号 1：

```bash
curl -s -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' -d '{"history": 2, "capacity": 3}'
# {"session_id":5,"history":2,"capacity":3}

for v in 10 20 30; do
  curl -s -X POST http://localhost:8000/sessions/5/samples \
    -H 'Content-Type: application/json' -d "{\"value\": $v}" > /dev/null
done
curl -s -X POST http://localhost:8000/sessions/5/triggers \
  -H 'Content-Type: application/json' -d '{"before": 1, "after": 1}' > /dev/null
curl -s -X POST http://localhost:8000/sessions/5/samples \
  -H 'Content-Type: application/json' -d '{"value": 40}' > /dev/null   # seq=4，片段 completed

curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/sessions/5/samples \
  -H 'Content-Type: application/json' -d '{"value": 50}'
# 409   —— 并集 {2,3,4,5} 超出 C=3，next_seq 仍为 5

curl -s -X PUT http://localhost:8000/sessions/5/limits \
  -H 'Content-Type: application/json' -d '{"history": 2, "capacity": 4}'
# {"session_id":5,"history":2,"capacity":4,"next_seq":5,"used":3,"stored_seqs":[2,3,4],"fragments":[1]}

curl -s -X POST http://localhost:8000/sessions/5/samples \
  -H 'Content-Type: application/json' -d '{"value": 50}'
# {"seq":5,"value":50}   —— 扩容后重试成功，序号连续，片段内容与编号不变
```

## 本地开发（可选）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```
