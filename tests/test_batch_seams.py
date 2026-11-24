"""整批换片的批界缝校验：按整批换完后的图，验本批触及的内缝。

覆盖：
* 只换一片且与未换旧片错缝 -> 整批拒绝，旧图不变（本服务修复的核心缺陷）；
* 两侧一起换 -> 入库，出图正常；
* 删掉端点也要验；
* 对侧还没交片可先留端点，等对侧交片再核对；
* 换片不读未换片的折线明细。
"""

import json
import sqlite3
from contextlib import contextmanager

from app.db import Database
from app.main import execute_batch_submission
from app.seed import PAIR_GOOD_BATCH, SEED_TILES, SINGLE_SIDE_BAD_BATCH


def test_single_side_mismatch_rejected_and_old_map_kept(seeded_client):
    before = seeded_client.get("/api/render").json()
    r = seeded_client.post("/api/batches", json=SINGLE_SIDE_BAD_BATCH)
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "rejected"
    # 报错指出两侧片号与端点坐标
    assert body["errors"]
    for e in body["errors"]:
        assert e["code"] == "seam_count_mismatch"
        assert e["seam"] == ["v", 0, 0]
        assert e["tiles"] == [[0, 0], [0, 1]]
    assert {tuple(e["point"]) for e in body["errors"]} == {(100, 20), (100, 25)}
    assert all(e["depth"] == 3 for e in body["errors"])
    # 整批不生效：旧图不变，批次留 rejected 审计
    assert seeded_client.get("/api/render").json() == before
    batches = seeded_client.get("/api/batches").json()["batches"]
    assert batches[-1]["status"] == "rejected"
    assert batches[-1]["errors"]


def test_pair_replacement_commits_and_renders(seeded_client):
    r = seeded_client.post("/api/batches", json=PAIR_GOOD_BATCH)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "committed"
    rendered = seeded_client.get("/api/render").json()
    assert rendered["status"] == "ok"
    d3 = next(f for f in rendered["features"] if f["depth"] == 3)
    # 新缝点两侧各一个端点，接成一条横贯长线
    assert d3["points"].count([100, 25]) == 1
    assert d3["points"][0] == [0, 20] and d3["points"][-1] == [200, 20]


def test_deleted_endpoint_also_validated(seeded_client):
    # 只换 (1,0) 并删掉水深 10 折线：旧端点 (30,100) 没了，
    # 但未换的 (0,0) 顶边仍留有该端点 -> 拒绝
    r = seeded_client.post(
        "/api/batches",
        json={
            "tiles": [
                {"row": 1, "col": 0, "polylines": [
                    {"depth": 5, "points": [[0, 130], [50, 135], [100, 130]]},
                ]},
            ]
        },
    )
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert any(
        e["point"] == [30, 100] and e["depth"] == 10
        and e["tiles"] == [[0, 0], [1, 0]]
        for e in errors
    )
    # 旧图不变
    assert seeded_client.get("/api/render").json()["status"] == "ok"


def test_opposite_side_not_submitted_allows_dangling_then_checked(client):
    client.post("/api/grid", json={"rows": 2, "cols": 2, "tile_size": 100})
    # 对侧还没交片：端点可先留，批被接受
    r = client.post(
        "/api/batches",
        json={"tiles": [{"row": 0, "col": 0, "polylines": [
            {"depth": 3, "points": [[0, 20], [50, 25], [100, 20]]},
        ]}]},
    )
    assert r.status_code == 200, r.text
    # 对侧交片但错缝 -> 这一批被拒
    r = client.post(
        "/api/batches",
        json={"tiles": [{"row": 0, "col": 1, "polylines": [
            {"depth": 3, "points": [[100, 25], [150, 20], [200, 20]]},
        ]}]},
    )
    assert r.status_code == 422
    assert r.json()["errors"][0]["tiles"] == [[0, 0], [0, 1]]
    # 对侧交片且对缝 -> 入库，出图接通
    r = client.post(
        "/api/batches",
        json={"tiles": [{"row": 0, "col": 1, "polylines": [
            {"depth": 3, "points": [[100, 20], [150, 18], [200, 20]]},
        ]}]},
    )
    assert r.status_code == 200, r.text
    rendered = client.get("/api/render").json()
    d3 = next(f for f in rendered["features"] if f["depth"] == 3)
    assert d3["points"][0] == [0, 20] and d3["points"][-1] == [200, 20]


def test_neighbor_tile_with_no_internal_endpoints_counts_as_submitted(client):
    client.post("/api/grid", json={"rows": 1, "cols": 2, "tile_size": 100})
    # (0,0) 的折线两端都在图幅外边 -> 已交片但内缝上没有端点
    r = client.post(
        "/api/batches",
        json={"tiles": [{"row": 0, "col": 0, "polylines": [
            {"depth": 4, "points": [[0, 30], [40, 20], [30, 0]]},
        ]}]},
    )
    assert r.status_code == 200, r.text
    # (0,1) 在共缝上留端点：对侧已交片（只是该缝无端点），单侧端点不允许
    r = client.post(
        "/api/batches",
        json={"tiles": [{"row": 0, "col": 1, "polylines": [
            {"depth": 4, "points": [[100, 30], [150, 20], [200, 30]]},
        ]}]},
    )
    assert r.status_code == 422
    assert r.json()["errors"][0]["tiles"] == [[0, 0], [0, 1]]


def test_untouched_seams_not_revalidated(seeded_client):
    # 下排 (1,1) 换成全新线型但四周缝端点保持不变：
    # 只验本批触及的缝，其它缝即使与己无关也不影响本批
    r = seeded_client.post(
        "/api/batches",
        json={"tiles": [{"row": 1, "col": 1, "polylines": [
            {"depth": 5, "points": [[100, 130], [150, 140], [200, 130]]},
            {"depth": 10, "points": [[100, 130], [130, 155], [170, 100]]},
        ]}]},
    )
    assert r.status_code == 200, r.text
    assert seeded_client.get("/api/render").json()["status"] == "ok"


class _TracingDatabase(Database):
    """记录换片过程中执行过的全部 SQL。"""

    def __init__(self, path):
        self.statements: list[str] = []
        super().__init__(path)

    @contextmanager
    def connect(self):
        with super().connect() as conn:
            conn.set_trace_callback(self.statements.append)
            yield conn


def test_batch_submission_does_not_read_unreplaced_polyline_details(tmp_path):
    db = _TracingDatabase(str(tmp_path / "t.db"))
    with db.connect() as conn:
        db.set_grid(conn, 2, 2, 100)
        db.commit_batch(conn, SEED_TILES, "seed")
        conn.commit()
    db.statements.clear()

    result = execute_batch_submission(db, PAIR_GOOD_BATCH["tiles"], "tracing")
    assert result["status"] == "committed"

    reads_of_revisions = [
        s for s in db.statements
        if "tile_revisions" in s.lower() and s.lstrip().upper().startswith("SELECT")
    ]
    assert reads_of_revisions == [], "换片流程不得 SELECT 未换片的折线明细"
    # 确实走了端点索引表
    assert any("tile_seam_endpoints" in s for s in db.statements)


def test_rejected_batch_leaves_no_partial_writes(seeded_client):
    r = seeded_client.post("/api/batches", json=SINGLE_SIDE_BAD_BATCH)
    assert r.status_code == 422
    tiles = {t["row"] + t["col"] * 10: t for t in
             seeded_client.get("/api/tiles").json()["tiles"]}
    # (0,0) 仍指向种子批次
    seed_batch = seeded_client.get("/api/batches").json()["batches"][0]["id"]
    assert tiles[0]["batch_id"] == seed_batch
    # 直连库确认没有写入新折线版本
    db_path = seeded_client.app.state.db.path
    raw = sqlite3.connect(db_path)
    rejected = raw.execute(
        "SELECT id FROM batches WHERE status='rejected'"
    ).fetchall()
    assert rejected
    for (bid,) in rejected:
        n = raw.execute(
            "SELECT COUNT(*) FROM tile_revisions WHERE batch_id=?", (bid,)
        ).fetchone()[0]
        assert n == 0
    raw.close()
