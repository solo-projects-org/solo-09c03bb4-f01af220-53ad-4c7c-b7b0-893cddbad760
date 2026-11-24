"""并发/快照语义测试。

验证：出图在只读事务内取一致快照，换片写入不等待出图，
已经开始的出图请求始终读到它启动时的整图版本。
"""

import sqlite3
import threading

from app.db import Database
from app.geometry import Grid
from app.seed import GOOD_BATCH, SEED_TILES


def _fresh_db(path):
    db = Database(path)
    with db.connect() as conn:
        db.set_grid(conn, 2, 2, 100)
        db.commit_batch(conn, SEED_TILES, "seed")
        conn.commit()
    return db


def test_snapshot_is_stable_while_writer_commits(tmp_path):
    db = _fresh_db(str(tmp_path / "s.db"))

    snapshot_holder = {}
    barrier = threading.Event()

    def reader():
        with db.connect() as conn:
            # 在读事务内先取快照，再等待写入线程完成提交
            conn.execute("BEGIN")
            snapshot_holder["before"] = db.current_snapshot(conn)
            barrier.set()
            # 等待写入完成后仍在同一事务内再次读取
            done.wait(timeout=5)
            snapshot_holder["after"] = db.current_snapshot(conn)

    done = threading.Event()
    t = threading.Thread(target=reader)
    t.start()
    barrier.wait(timeout=5)

    with db.connect() as conn:  # 写入连接独立，不等待读事务
        batch_id = db.commit_batch(conn, GOOD_BATCH["tiles"], "replace")
        conn.commit()
    done.set()
    t.join(timeout=5)

    # 长读事务前后看到的是同一个旧版本
    before_tiles = {(t["row"], t["col"]): t["polylines"]
                    for t in snapshot_holder["before"]["tiles"]}
    after_tiles = {(t["row"], t["col"]): t["polylines"]
                   for t in snapshot_holder["after"]["tiles"]}
    assert before_tiles == after_tiles
    # 旧版本里水深 3 还是旧线型
    assert [40, 25] in [p for pl in before_tiles[(0, 0)] for p in pl["points"]]

    # 新连接立即看到新版本
    with db.connect() as conn:
        fresh = db.current_snapshot(conn)
    fresh_t0 = next(t for t in fresh["tiles"] if (t["row"], t["col"]) == (0, 0))
    assert [45, 30] in [p for pl in fresh_t0["polylines"] for p in pl["points"]]
    assert batch_id > 0


def test_wal_mode_enabled(tmp_path):
    db = _fresh_db(str(tmp_path / "w.db"))
    with db.connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


def test_rollback_keeps_old_revisions(tmp_path):
    path = str(tmp_path / "r.db")
    db = _fresh_db(path)
    try:
        with db.connect() as conn:
            db.commit_batch(conn, GOOD_BATCH["tiles"], "will-rollback")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with db.connect() as conn:
        snap = db.current_snapshot(conn)
    t00 = next(t for t in snap["tiles"] if (t["row"], t["col"]) == (0, 0))
    # 回滚后仍是旧线型
    assert any([40, 25] in pl["points"] for pl in t00["polylines"])
    # 直连数据库确认没有半成品行
    raw = sqlite3.connect(path)
    assert raw.execute(
        "SELECT COUNT(*) FROM tile_revisions WHERE batch_id IN "
        "(SELECT id FROM batches WHERE reason='will-rollback')"
    ).fetchone()[0] == 0
    raw.close()


def test_grid_construction_matches_seed():
    g = Grid(2, 2, 100)
    assert g.tile_bounds(1, 1) == (100, 100, 200, 200)
    assert g.seam_of_edge(0, 0, "right") == ("v", (0, 0))
    assert g.seam_of_edge(0, 1, "left") == ("v", (0, 0))
    assert g.seam_of_edge(0, 0, "top") == ("h", (0, 0))
    assert g.seam_of_edge(1, 0, "bottom") == ("h", (0, 0))
    assert g.seam_of_edge(0, 0, "left") is None  # 图幅外边
