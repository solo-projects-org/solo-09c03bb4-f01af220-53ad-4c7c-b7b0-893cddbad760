"""并发/快照语义测试。

验证：出图在只读事务内取一致快照，换片写入不等待出图，
已经开始的出图请求始终读到它启动时的整图版本；
并发换片时，批界缝校验一律在写事务内现读邻片端点索引，
不会按过期的邻片验缝结果放行；无关换片互不影响。
"""

import sqlite3
import threading

from app.db import Database
from app.geometry import Grid, validate_boundary_seams
from app.main import execute_batch_submission
from app.seed import GOOD_BATCH, PAIR_GOOD_BATCH, SEED_TILES, SINGLE_SIDE_BAD_BATCH


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


def test_neighbor_endpoints_read_is_never_stale(tmp_path):
    """批界缝校验在写事务内现读邻片索引：读到的必然是已提交的最新状态。"""
    db = _fresh_db(str(tmp_path / "ns.db"))
    a_locked = threading.Event()
    b_may_read = threading.Event()
    seen = {}

    def writer_a():
        # A 把 (0,0) 水深 3 的缝端点从 (100,20) 挪到 (100,25)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            a_locked.set()
            b_may_read.wait(timeout=5)
            db.commit_batch(conn, [PAIR_GOOD_BATCH["tiles"][0]], "A")
            conn.commit()

    def validator_b():
        a_locked.wait(timeout=5)
        with db.connect() as conn:
            # A 持有写锁期间，B 的 BEGIN IMMEDIATE 只能等 A 提交后再读
            conn.execute("BEGIN IMMEDIATE")
            seen["eps"] = db.load_seam_endpoints(conn, [(0, 0)])
            conn.commit()

    ta = threading.Thread(target=writer_a)
    tb = threading.Thread(target=validator_b)
    ta.start()
    tb.start()
    a_locked.wait(timeout=5)
    b_may_read.set()  # B 已在 BEGIN IMMEDIATE 上排队，放行 A 提交
    ta.join(timeout=10)
    tb.join(timeout=10)

    # B 读到的是 A 提交后的新端点，而不是过期旧端点 (100,20)
    keys = {(e.edge, e.x, e.y, e.depth) for e in seen["eps"][(0, 0)]}
    assert ("right", 100, 25, 3) in keys
    assert ("right", 100, 20, 3) not in keys
    # 因此 B 若仍按旧端点 (100,20) 换 (0,1)，验缝必然失败而非放行
    from app.geometry import SeamEndpoint

    batch_eps = {
        (0, 1): [
            SeamEndpoint(100, 20, 3, (0, 1), "left", 0, "start"),
            SeamEndpoint(100, 70, 10, (0, 1), "left", 1, "start"),
        ]
    }
    errors = validate_boundary_seams(Grid(2, 2, 100), batch_eps, seen["eps"])
    assert any(e["point"] == [100, 20] for e in errors)


def test_concurrent_pair_and_stale_single_side_batches(tmp_path):
    """并发：两侧一起换 与 按旧缝点单侧换。

    无论提交先后，最终整图必须一致（出图无错缝）；
    单侧那批只有在对方提交之后验缝才可能通过（此时它恰好与新缝点一致）。
    """
    db = _fresh_db(str(tmp_path / "cc.db"))
    results = {}

    def run(key, tiles):
        results[key] = execute_batch_submission(db, tiles, key)

    threads = [
        threading.Thread(target=run, args=("pair", PAIR_GOOD_BATCH["tiles"])),
        threading.Thread(target=run, args=("single", SINGLE_SIDE_BAD_BATCH["tiles"])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    # 两侧一起换的那批一定成功
    assert results["pair"]["status"] == "committed"
    # 最终整图一致：出图无错缝
    with db.connect() as conn:
        snapshot = db.current_snapshot(conn)
    from app.assembly import assemble

    _, seam_errors = assemble(Grid(2, 2, 100), snapshot["tiles"])
    assert seam_errors == []


def test_unrelated_batches_both_commit(tmp_path):
    """无关换片（触及的缝不相交）互不影响：并发提交两批都应入库。"""
    path = str(tmp_path / "unrelated.db")
    db = Database(path)
    with db.connect() as conn:
        db.set_grid(conn, 1, 3, 100)
        seed = [
            {"row": 0, "col": 0,
             "polylines": [{"depth": 3, "points": [[0, 20], [50, 25], [100, 20]]}]},
            {"row": 0, "col": 1,
             "polylines": [{"depth": 3, "points": [[100, 20], [150, 25], [200, 20]]}]},
            {"row": 0, "col": 2,
             "polylines": [{"depth": 3, "points": [[200, 20], [250, 25], [300, 20]]}]},
        ]
        db.commit_batch(conn, seed, "seed")
        conn.commit()

    batch_left = [{"row": 0, "col": 0,
                   "polylines": [{"depth": 3, "points": [[0, 20], [55, 30], [100, 20]]}]}]
    batch_right = [{"row": 0, "col": 2,
                    "polylines": [{"depth": 3, "points": [[200, 20], [245, 30], [300, 20]]}]}]
    results = {}

    def run(key, tiles):
        results[key] = execute_batch_submission(db, tiles, key)

    threads = [
        threading.Thread(target=run, args=("left", batch_left)),
        threading.Thread(target=run, args=("right", batch_right)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    # 两批都不因对方先入库而被拒
    assert results["left"]["status"] == "committed"
    assert results["right"]["status"] == "committed"
    with db.connect() as conn:
        snapshot = db.current_snapshot(conn)
    from app.assembly import assemble

    features, seam_errors = assemble(Grid(1, 3, 100), snapshot["tiles"])
    assert seam_errors == []
    d3 = next(f for f in features if f["depth"] == 3)
    assert d3["points"][0] == [0, 20] and d3["points"][-1] == [300, 20]
    assert [55, 30] in d3["points"] and [245, 30] in d3["points"]
