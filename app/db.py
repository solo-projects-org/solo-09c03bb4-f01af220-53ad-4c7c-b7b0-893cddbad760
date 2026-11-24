"""SQLite 持久层。

设计要点
--------
* WAL 模式 + 每次请求独立连接：读不阻塞写、写不阻塞读；
* 折线按“版本”保存（``tile_revisions`` 只增不删），``current_tiles`` 指向当前版本，
  换片失败时事务整体回滚，旧图原封不动；
* 出图在单个读事务内取齐全部当前版本，得到一致快照；
  换片写入不会等待已经开始的出图，正在出图的请求继续读到原版。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

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
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

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
        """在调用方事务内写入一整批新片，返回批次号。"""
        cur = conn.execute(
            "INSERT INTO batches (status, reason, tile_count, errors) "
            "VALUES ('committed', ?, ?, NULL)",
            (reason, len(tiles)),
        )
        batch_id = cur.lastrowid
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
        return batch_id

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
            "DELETE FROM current_tiles; DELETE FROM tile_revisions; "
            "DELETE FROM batches; DELETE FROM grid_meta;"
        )
