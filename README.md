# 港池等深线分片测图服务

测绘组按**固定方片网格**分片绘制港池等深线，制图员通过本服务取回接好的整图。
FastAPI + SQLite（WAL）实现，支持整批换片、验缝、出图拼接与并发快照。

## 1. 领域规则与实现对照

| 业务要求 | 实现位置 |
| --- | --- |
| 矩形图幅按固定方片网格划分，整数坐标 | `app/geometry.py` `Grid`；`POST /api/grid` 一次性定义，有数据后不可改 |
| 每片提交带水深的整数坐标折线 | `app/schemas.py` `PolylineIn`（非整数坐标在 Pydantic 层拒绝） |
| 折线两端落在片边、避开角点，其余点严格在片内 | `validate_tile_polylines`：`endpoint_not_on_edge` / `interior_point_on_edge` |
| 内缝同一水深、同一位置端点两侧各一个；外边可留端点 | `validate_batch`：只枚举本批两侧都换的内缝，按 `(缝, 位置, 水深)` 配对，错误码 `seam_count_mismatch` |
| 按整批换完的图验缝，**不读未换片折线明细** | 验缝只入参本批 `tiles` + 静态网格；批界缝单侧端点放行，留到出图核对 |
| 接不上指出接缝，换片失败继续用旧图 | 失败返回 HTTP 422 + 接缝坐标/片号/水深，事务回滚；`batches` 表留 `rejected` 审计记录 |
| 出图接成两端位于图幅外边的长线或闭环，每条原折线恰用一次 | `app/assembly.py` 半边图行走；`used == edges` 自检断言保证不重不漏 |
| 换片不等旧图出完，正在出图的请求继续用原版 | SQLite WAL + 单读事务快照，出图期间提交的换片不影响本次结果（见 `tests/test_concurrency.py`） |

坐标约定：`x` 向右、`y` 向上；片号 `(row, col)`，`row` 自下而上、`col` 自左而右，均从 0 开始。

## 2. 目录结构

```
app/
  main.py       FastAPI 路由与事务边界
  geometry.py   网格、单片几何校验、整批验缝
  assembly.py   整图接图（长线/闭环）与批界缝核对
  db.py         SQLite 持久层（WAL、版本化、快照读）
  schemas.py    Pydantic 模型
  seed.py       Swagger 演示数据（2x2：2 条长线 + 1 个菱形闭环）
tests/          32 个 pytest 用例
Dockerfile, docker-compose.yml, requirements*.txt
```

## 3. Docker Compose 启动

```bash
docker compose up --build
# 服务：http://localhost:8000
# Swagger：http://localhost:8000/docs   （可直接 Try it out）
# 数据库：命名卷 soundings-data 内的 /data/soundings.db
```

## 4. Swagger 操作样例（也可用 curl 复现）

1. `POST /admin/seed` —— 一键载入 2x2 演示图幅（清空重建，仅演示用）。
2. `GET /api/render` —— 出图：水深 3、5 各一条横贯图幅的长线，水深 10 一个跨四片闭环。
3. `POST /api/batches` —— 请求体下拉选择内置示例：
   * **合格样例-上排修测**：只换上排两片，批内缝匹配、批界缝沿用旧端点，返回 200；
   * **失败样例-接缝对不上**：竖缝上水深 3 端点两侧位置不一致，返回 422 并指出接缝，旧图不变。
4. `GET /api/tiles` —— 各片当前版本（只给条数与批次号，**不返回折线明细**）。
5. `GET /api/batches` —— 全部换批记录，含被拒绝批次的接缝错误。

curl 速览：

```bash
curl -X POST localhost:8000/admin/seed
curl localhost:8000/api/render | jq
curl -X POST localhost:8000/api/batches -H 'Content-Type: application/json' \
  -d '{"tiles":[{"row":0,"col":0,"polylines":[{"depth":3,"points":[[0,20],[50,30],[100,20]]}]},
               {"row":0,"col":1,"polylines":[{"depth":3,"points":[[100,25],[150,20],[200,20]]}]}]}'
# -> 422，errors 中给出 seam=["v",0,0]、point、depth、两侧片号
```

## 5. 出图结果说明

`GET /api/render` 返回：

* `status`：`ok` 或 `seam_errors`（新片与未换旧片在批界缝接不上时）；
* `features[]`：`depth`、`closed`、`points`、`source_tiles`；
  * 长线 `closed=false`，首尾点必在图幅外边；
  * 闭环 `closed=true`，点序列首尾重复表示闭合；
* `seam_errors[]`：接不上的节点，含坐标、水深、涉及片号（`seam_dangling` / `seam_collision` / `outer_endpoint_conflict`）。
  即便有错也会返回可拼接部分，且不丢任何原折线。

## 6. 本地开发与自测

```bash
python3 -m venv .venv && source .venv/bin/activate   # 或自行准备解释器
pip install -r requirements.txt -r requirements-dev.txt
PYTHONPATH=. pytest -q
uvicorn app.main:app --reload
```

## 7. 数据模型（SQLite）

* `grid_meta`：唯一网格定义；
* `batches`：换批记录，`status` 为 `committed` / `rejected`，拒绝时存错误 JSON；
* `tile_revisions`：折线版本表，**只增不删**；
* `current_tiles`：每片当前指向的批次版本（UPSERT）。

换片在单事务内完成：任一片写入失败或进程中断，整批回滚，当前版本指针不变。
