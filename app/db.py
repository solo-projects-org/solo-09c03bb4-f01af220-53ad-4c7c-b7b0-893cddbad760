"""SQLite 持久层。

设计要点
--------
* WAL 模式 + 每次请求独立连接：读不阻塞写、写不阻塞读；
* 折线按“版本”保存（``tile_revisions`` 只增不删），``current_tiles`` 指向当前版本，
  换片失败时事务整体回滚，旧图原封不动；
* ``tile_seam_endpoints`` 是**当前各片内缝端点的派生索引**（只存端点摘要，
  不含折线明细）：换片验缝只查它，不读未换片的折线明细；
  它在换片事务内随批原子更新，旧库升级时由 ``user_version`` 迁移回填，
  图幅数据与换片历史全部保留，不必重交旧片；
* 出图在单个读事务内取齐全部当前版本，得到一致快照；
  换片写入不会等待已经开始的出图，正在出图的请求继续读到原版。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from .geometry import Grid, SeamEndpoint, validate_tile_polylines

# 库结构版本：1 = 初始结构；2 = 增加 tile_seam_endpoints 接缝端点索引
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS grid_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rows INTEGER NOT NULL,
    cols INTEGER NOT NULL,
    tile_size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL CHECK (status IN ('committed', 'rejected')),
    reason TEXT,
    tile_count INTEGER NOT NULL,
    errors TEXT
);
CREATE TABLE IF NOT EXISTS tile_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tile_row INTEGER NOT NULL,
    tile_col INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    points TEXT NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES batches(id)
);
CREATE INDEX IF NOT EXISTS idx_rev_tile ON tile_revisions(tile_row, tile_col, id);
CREATE TABLE IF NOT EXISTS current_tiles (
    tile_row INTEGER NOT NULL,
    tile_col INTEGER NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tile_row, tile_col)
);
CREATE TABLE IF NOT EXISTS tile_seam_endpoints (
    tile_row INTEGER NOT NULL,
    tile_col INTEGER NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    edge TEXT NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    PRIMARY KEY (tile_row, tile_col, edge, x, y, depth)
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.commit()

    # ------------------------------------------------------------------
    def _migrate(self, conn: sqlite3.Connection) -> None:
        """把旧库升到当前 SCHEMA_VERSION，保留全部图幅数据与换片历史。

        v1 -> v2：补建接缝端点索引。只依据 ``current_tiles`` 指向的当前版本
        回填（历史版本不参与验缝，无需索引），旧片不必重交。
        """
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        meta = self.get_grid(conn)
        if meta is not None:
            grid = Grid(meta["rows"], meta["cols"], meta["tile_size"])
            snapshot = self.current_snapshot(conn)
            batch_of = {
                (t["tile_row"], t["tile_col"]): t["batch_id"]
                for t in conn.execute(
                    "SELECT tile_row, tile_col, batch_id FROM current_tiles"
                ).fetchall()
            }
            for tile in snapshot["tiles"]:
                row, col = tile["row"], tile["col"]
                endpoints, errors = validate_tile_polylines(
                    grid, row, col, tile["polylines"]
                )
                if errors:  # 旧库数据理论上已合格；有错则跳过该片索引，留待换片时重验
                    continue
                self._replace_seam_index(
                    conn, grid, row, col, batch_of[(row, col)], endpoints
                )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def get_grid(self, conn: sqlite3.Connection) -> dict[str, int] | None:
        row = conn.execute(
            "SELECT rows, cols, tile_size FROM grid_meta WHERE id = 1"
        ).fetchone()
        return dict(row) if row else None

    def set_grid(self, conn: sqlite3.Connection, rows: int, cols: int,
                 tile_size: int) -> dict[str, int]:
        existing = self.get_grid(conn)
        if existing is not None:
            if (existing["rows"], existing["cols"], existing["tile_size"]) != (
                rows,
                cols,
                tile_size,
            ):
                raise ValueError("网格已存在且参数不同；不允许在有数据时改网格")
            return existing
        conn.execute(
            "INSERT INTO grid_meta (id, rows, cols, tile_size) VALUES (1, ?, ?, ?)",
            (rows, cols, tile_size),
        )
        return {"rows": rows, "cols": cols, "tile_size": tile_size}

    def has_tiles(self, conn: sqlite3.Connection) -> bool:
        return conn.execute(
            "SELECT 1 FROM current_tiles LIMIT 1"
        ).fetchone() is not None

    # ------------------------------------------------------------------
    def commit_batch(
        self,
        conn: sqlite3.Connection,
        tiles: list[dict],
        reason: str | None,
    ) -> int:
        """在调用方事务内写入一整批新片，返回批次号。

        同时刷新本批各片的接缝端点索引（与折线版本同一事务，同生同灭）。
        """
        cur = conn.execute(
            "INSERT INTO batches (status, reason, tile_count, errors) "
            "VALUES ('committed', ?, ?, NULL)",
            (reason, len(tiles)),
        )
        batch_id = cur.lastrowid
        meta = self.get_grid(conn)
        grid = (
            Grid(meta["rows"], meta["cols"], meta["tile_size"])
            if meta is not None
            else None
        )
        for t in tiles:
            row, col = t["row"], t["col"]
            for seq, pl in enumerate(t["polylines"]):
                conn.execute(
                    "INSERT INTO tile_revisions "
                    "(tile_row, tile_col, depth, seq, points, batch_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row,
                        col,
                        pl["depth"],
                        seq,
                        json.dumps(pl["points"], separators=(",", ":")),
                        batch_id,
                    ),
                )
            conn.execute(
                "INSERT INTO current_tiles (tile_row, tile_col, batch_id) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(tile_row, tile_col) DO UPDATE SET "
                "batch_id=excluded.batch_id, updated_at=datetime('now')",
                (row, col, batch_id),
            )
            if grid is not None:
                endpoints, errors = validate_tile_polylines(
                    grid, row, col, t["polylines"]
                )
                if errors:
                    raise ValueError(
                        f"片 ({row},{col}) 单片几何校验未通过: "
                        f"{errors[0]['message']}"
                    )
                self._replace_seam_index(conn, grid, row, col, batch_id, endpoints)
        return batch_id

    def _replace_seam_index(
        self,
        conn: sqlite3.Connection,
        grid: Grid,
        row: int,
        col: int,
        batch_id: int,
        endpoints: Iterable[SeamEndpoint],
    ) -> None:
        """重建某片的接缝端点索引（只索引内缝端点，外边端点不参与验缝）。"""
        conn.execute(
            "DELETE FROM tile_seam_endpoints WHERE tile_row = ? AND tile_col = ?",
            (row, col),
        )
        for ep in endpoints:
            if grid.seam_of_edge(row, col, ep.edge) is None:
                continue
            conn.execute(
                "INSERT INTO tile_seam_endpoints "
                "(tile_row, tile_col, batch_id, edge, x, y, depth) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row, col, batch_id, ep.edge, ep.x, ep.y, ep.depth),
            )

    def load_seam_endpoints(
        self,
        conn: sqlite3.Connection,
        tile_ids: Iterable[tuple[int, int]],
    ) -> dict[tuple[int, int], list[SeamEndpoint]]:
        """读取指定**当前**片的内缝端点索引（不读折线明细）。

        返回 ``{片号: [SeamEndpoint, ...]}``：已交片但没有内缝端点的片
        也会出现在键中（空列表），以便调用方区分“对侧未交片”；
        未交片的片号不出现在键中。端点的 ``polyline_index``/``end``
        对验缝无意义，置为 -1 / ""。
        """
        ids = sorted(set(tile_ids))
        if not ids:
            return {}
        where = " OR ".join("(c.tile_row = ? AND c.tile_col = ?)" for _ in ids)
        params = [v for tid in ids for v in tid]
        rows = conn.execute(
            f"""
            SELECT c.tile_row AS row, c.tile_col AS col,
                   e.edge, e.x, e.y, e.depth
            FROM current_tiles c
            LEFT JOIN tile_seam_endpoints e
              ON e.tile_row = c.tile_row AND e.tile_col = c.tile_col
            WHERE {where}
            ORDER BY c.tile_row, c.tile_col
            """,
            params,
        ).fetchall()
        out: dict[tuple[int, int], list[SeamEndpoint]] = {}
        for r in rows:
            tile = (r["row"], r["col"])
            bucket = out.setdefault(tile, [])
            if r["edge"] is None:
                continue  # 已交片但内缝上没有端点
            bucket.append(
                SeamEndpoint(
                    x=r["x"],
                    y=r["y"],
                    depth=r["depth"],
                    tile=tile,
                    edge=r["edge"],
                    polyline_index=-1,
                    end="",
                )
            )
        return out

    def record_rejected_batch(
        self, conn: sqlite3.Connection, tile_count: int, reason: str | None,
        errors: list[dict],
    ) -> int:
        cur = conn.execute(
            "INSERT INTO batches (status, reason, tile_count, errors) "
            "VALUES ('rejected', ?, ?, ?)",
            (reason, tile_count, json.dumps(errors, ensure_ascii=False)),
        )
        return cur.lastrowid

    # ------------------------------------------------------------------
    def current_snapshot(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """单事务内读取整图当前版本的一致快照。"""
        grid = self.get_grid(conn)
        rows = conn.execute(
            """
            SELECT c.tile_row AS row, c.tile_col AS col, c.batch_id,
                   r.depth, r.seq, r.points
            FROM current_tiles c
            JOIN tile_revisions r
              ON r.tile_row = c.tile_row
             AND r.tile_col = c.tile_col
             AND r.batch_id = c.batch_id
            ORDER BY c.tile_row, c.tile_col, r.seq
            """
        ).fetchall()
        tiles: dict[tuple[int, int], dict] = {}
        for r in rows:
            tile = tiles.setdefault(
                (r["row"], r["col"]),
                {"row": r["row"], "col": r["col"], "polylines": []},
            )
            tile["polylines"].append(
                {"depth": r["depth"], "points": json.loads(r["points"])}
            )
        return {"grid": grid, "tiles": list(tiles.values())}

    def list_batches(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT id, created_at, status, reason, tile_count, errors "
            "FROM batches ORDER BY id"
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["errors"] = json.loads(item["errors"]) if item["errors"] else []
            out.append(item)
        return out

    def list_current_tiles(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            """
            SELECT c.tile_row AS row, c.tile_col AS col,
                   c.batch_id, c.updated_at,
                   (SELECT COUNT(*) FROM tile_revisions r
                     WHERE r.tile_row = c.tile_row
                       AND r.tile_col = c.tile_col
                       AND r.batch_id = c.batch_id) AS polyline_count
            FROM current_tiles c
            ORDER BY c.tile_row, c.tile_col
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def reset(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            "DELETE FROM tile_seam_endpoints; DELETE FROM current_tiles; "
            "DELETE FROM tile_revisions; "
            "DELETE FROM batches; DELETE FROM grid_meta;"
        )
