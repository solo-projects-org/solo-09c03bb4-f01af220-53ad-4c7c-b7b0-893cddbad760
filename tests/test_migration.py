"""旧版 SQLite 库升级：保留图幅数据与换片历史，不必重交旧片。"""

import json
import sqlite3

from app.db import Database
from app.main import execute_batch_submission
from app.seed import SEED_TILES, SINGLE_SIDE_BAD_BATCH

# 旧版（v1）库结构：没有 tile_seam_endpoints，user_version = 0
OLD_SCHEMA = """
CREATE TABLE grid_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rows INTEGER NOT NULL, cols INTEGER NOT NULL, tile_size INTEGER NOT NULL
);
CREATE TABLE batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL CHECK (status IN ('committed', 'rejected')),
    reason TEXT, tile_count INTEGER NOT NULL, errors TEXT
);
CREATE TABLE tile_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tile_row INTEGER NOT NULL, tile_col INTEGER NOT NULL,
    depth INTEGER NOT NULL, seq INTEGER NOT NULL,
    points TEXT NOT NULL, batch_id INTEGER NOT NULL REFERENCES batches(id)
);
CREATE TABLE current_tiles (
    tile_row INTEGER NOT NULL, tile_col INTEGER NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tile_row, tile_col)
);
"""


def _build_old_db(path) -> None:
    """按旧结构手工建库并写入一批历史数据。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO grid_meta VALUES (1, 2, 2, 100)")
    conn.execute(
        "INSERT INTO batches (status, reason, tile_count) "
        "VALUES ('committed', '历史换片', ?)",
        (len(SEED_TILES),),
    )
    batch_id = conn.execute("SELECT max(id) FROM batches").fetchone()[0]
    for t in SEED_TILES:
        for seq, pl in enumerate(t["polylines"]):
            conn.execute(
                "INSERT INTO tile_revisions VALUES (NULL,?,?,?,?,?,?)",
                (t["row"], t["col"], pl["depth"], seq,
                 json.dumps(pl["points"]), batch_id),
            )
        conn.execute(
            "INSERT INTO current_tiles VALUES (?,?,?,datetime('now'))",
            (t["row"], t["col"], batch_id),
        )
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()


def test_old_db_upgrade_keeps_data_and_history(tmp_path):
    path = tmp_path / "old.db"
    _build_old_db(path)

    db = Database(str(path))  # 打开即触发迁移
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        # 图幅数据保留
        tiles = db.list_current_tiles(conn)
        assert len(tiles) == 4
        # 换片历史保留
        batches = db.list_batches(conn)
        assert [(b["status"], b["reason"]) for b in batches] == [
            ("committed", "历史换片")
        ]
        # 旧折线明细原样保留
        snapshot = db.current_snapshot(conn)
        t00 = next(t for t in snapshot["tiles"] if (t["row"], t["col"]) == (0, 0))
        assert any([40, 25] in pl["points"] for pl in t00["polylines"])
        # 接缝端点索引已回填：旧片不必重交即可参与验缝
        eps = db.load_seam_endpoints(conn, [(0, 0)])
        assert ("right", 100, 20, 3) in {
            (e.edge, e.x, e.y, e.depth) for e in eps[(0, 0)]
        }

    # 迁移后的库上，单侧错缝换片被拒；两侧一起换可入库
    result = execute_batch_submission(
        db, SINGLE_SIDE_BAD_BATCH["tiles"], "迁移后单侧错缝"
    )
    assert result.status_code == 422
    from app.seed import PAIR_GOOD_BATCH

    result = execute_batch_submission(db, PAIR_GOOD_BATCH["tiles"], "迁移后两侧一起换")
    assert result["status"] == "committed"
    with db.connect() as conn:
        assert len(db.list_batches(conn)) == 3  # 历史 + 拒绝审计 + 新批


def test_fresh_db_starts_at_current_version(tmp_path):
    db = Database(str(tmp_path / "fresh.db"))
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
