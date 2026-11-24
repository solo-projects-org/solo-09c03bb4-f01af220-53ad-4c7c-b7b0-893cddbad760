"""港池等深线分片测图服务（FastAPI）。

工作流
------
1. ``POST /api/grid``  定义固定方片网格；
2. ``POST /api/batches``  整批换片：先做单片几何校验与批内缝校验，
   接不上直接 422 并指出接缝，事务回滚、旧图继续有效；
3. ``GET /api/render``  按当前整图快照接图，输出端点位于图幅外边的长线或闭环，
   同时核对新片与未换旧片之间的批界缝；
4. 出图只在单个只读事务内取快照，换片写入不等待出图，正在出图的请求继续用原版。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import seed
from .assembly import assemble
from .db import Database
from .geometry import Grid, validate_batch
from .schemas import BatchIn, GridIn, RenderOut


def get_db(request: Request) -> Database:
    return request.app.state.db


def build_grid(db: Database, conn) -> Grid:
    meta = db.get_grid(conn)
    if meta is None:
        raise HTTPException(status_code=409, detail="尚未定义网格，请先调用 POST /api/grid")
    return Grid(meta["rows"], meta["cols"], meta["tile_size"])


@asynccontextmanager
async def lifespan(app: FastAPI) -> None:
    db_path = os.environ.get("DB_PATH", "/data/soundings.db")
    app.state.db = Database(db_path)
    yield


app = FastAPI(
    title="港池等深线分片测图服务",
    version="1.0.0",
    description=(
        "矩形图幅按固定方片网格划分，测绘组按片提交带水深的整数坐标折线；"
        "整批换片、验缝，出图时接成外边长线或闭环。数据存 SQLite（WAL）。"
    ),
    lifespan=lifespan,
)


@app.get("/health", tags=["运维"])
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/grid", tags=["网格"], summary="定义（或确认）固定方片网格")
def define_grid(body: GridIn, db: Database = Depends(get_db)) -> dict:
    with db.connect() as conn:
        if body.reset:
            db.reset(conn)
        if not body.reset and db.has_tiles(conn):
            raise HTTPException(status_code=409, detail="图幅已有换片数据；如需重建请传 reset=true")
        meta = db.set_grid(conn, body.rows, body.cols, body.tile_size)
        conn.commit()
    return {"status": "ok", "grid": meta}


@app.post(
    "/api/batches",
    tags=["换片"],
    summary="整批换片（验缝失败则整批拒绝，旧图继续有效）",
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "合格样例-上排修测": {"value": seed.GOOD_BATCH},
                        "失败样例-接缝对不上": {"value": seed.BAD_BATCH},
                    }
                }
            }
        },
    },
)
def submit_batch(body: BatchIn, db: Database = Depends(get_db)) -> dict:
    payload = [t.model_dump() for t in body.tiles]
    with db.connect() as conn:
        grid = build_grid(db, conn)
        # 片号范围与重复校验
        for t in payload:
            if not (0 <= t["row"] < grid.rows and 0 <= t["col"] < grid.cols):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "tile_out_of_grid",
                        "message": f"片 ({t['row']},{t['col']}) 超出 {grid.rows}x{grid.cols} 网格",
                    },
                )
        # 整批验缝：只读本批提交数据，不读未换片折线明细
        _, errors = validate_batch(grid, payload)
        if errors:
            rejected_id = db.record_rejected_batch(
                conn, tile_count=len(payload), reason=body.reason, errors=errors
            )
            conn.commit()
            return JSONResponse(
                status_code=422,
                content={
                    "status": "rejected",
                    "batch_id": rejected_id,
                    "message": "验缝未通过，整批拒绝，当前图幅仍使用旧图",
                    "error_count": len(errors),
                    "errors": errors,
                },
            )
        batch_id = db.commit_batch(conn, payload, body.reason)
        conn.commit()
    return {
        "status": "committed",
        "batch_id": batch_id,
        "tile_count": len(payload),
        "message": "换片已生效，可调用 GET /api/render 出图",
    }


@app.get("/api/render", tags=["出图"], summary="接图：外边长线或闭环", response_model=RenderOut)
def render(db: Database = Depends(get_db)) -> dict[str, Any]:
    # 单个只读事务 = 一致快照；并发换片不会影响本次出图（正在出图继续用原版）
    with db.connect() as conn:
        snapshot = db.current_snapshot(conn)
    meta = snapshot["grid"]
    if meta is None:
        raise HTTPException(status_code=409, detail="尚未定义网格")
    grid = Grid(meta["rows"], meta["cols"], meta["tile_size"])
    tiles = snapshot["tiles"]
    features, seam_errors = assemble(grid, tiles)
    return {
        "status": "ok" if not seam_errors else "seam_errors",
        "grid": meta,
        "tile_count": len(tiles),
        "features": features,
        "seam_errors": seam_errors,
    }


@app.get("/api/tiles", tags=["查询"], summary="当前图幅各片状态（不含未换片折线明细）")
def list_tiles(db: Database = Depends(get_db)) -> dict:
    with db.connect() as conn:
        meta = db.get_grid(conn)
        tiles = db.list_current_tiles(conn)
    return {"grid": meta, "tiles": tiles}


@app.get("/api/batches", tags=["查询"], summary="换批记录（含被拒绝批次及其接缝错误）")
def list_batches(db: Database = Depends(get_db)) -> dict:
    with db.connect() as conn:
        return {"batches": db.list_batches(conn)}


@app.post(
    "/admin/seed",
    tags=["演示"],
    summary="一键载入 Swagger 演示数据（2x2 港池样例，会清空重建）",
)
def admin_seed(db: Database = Depends(get_db)) -> dict:
    with db.connect() as conn:
        db.reset(conn)
        db.set_grid(conn, seed.GRID["rows"], seed.GRID["cols"], seed.GRID["tile_size"])
        batch_id = db.commit_batch(conn, seed.SEED_TILES, "初始演示数据")
        conn.commit()
    return {
        "status": "ok",
        "batch_id": batch_id,
        "next_steps": [
            "GET /api/render 查看接好的整图（2 条长线 + 1 个闭环）",
            "POST /api/batches 用 Swagger 示例体验合格/失败换片",
        ],
    }
