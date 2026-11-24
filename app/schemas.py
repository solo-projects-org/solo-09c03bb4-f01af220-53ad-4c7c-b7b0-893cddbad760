"""请求/响应模型，含 Swagger UI 可直接使用的样例。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PolylineIn(BaseModel):
    depth: int = Field(..., description="等深线水深（整数）", examples=[5])
    points: list[list[int]] = Field(
        ...,
        description=(
            "整数坐标折线；首尾端点落在片边且避开角点，"
            "中间点严格位于片内，相邻点不得重合"
        ),
        examples=[[[0, 30], [40, 35], [100, 30]]],
    )

    @model_validator(mode="after")
    def _check_points(self) -> "PolylineIn":
        if len(self.points) < 2:
            raise ValueError("折线至少包含 2 个点")
        for p in self.points:
            if len(p) != 2:
                raise ValueError("每个点必须是 [x, y] 两个整数")
        return self


class TileIn(BaseModel):
    row: int = Field(..., ge=0, description="片行号，自下而上从 0 开始")
    col: int = Field(..., ge=0, description="片列号，自左而右从 0 开始")
    polylines: list[PolylineIn] = Field(..., min_length=1, description="本片提交的带水深折线（至少 1 条）")


class GridIn(BaseModel):
    rows: int = Field(..., gt=0, examples=[2])
    cols: int = Field(..., gt=0, examples=[2])
    tile_size: int = Field(..., gt=0, description="方片边长（整数坐标单位）", examples=[100])
    reset: bool = Field(
        False, description="true 时清空已有数据后重建（仅 /admin 演示接口使用）"
    )


class BatchIn(BaseModel):
    reason: str | None = Field(None, description="换批说明（可选）")
    tiles: list[TileIn] = Field(..., min_length=1, description="本批一起换的片")

    @model_validator(mode="after")
    def _unique_tiles(self) -> "BatchIn":
        ids = [(t.row, t.col) for t in self.tiles]
        if len(ids) != len(set(ids)):
            raise ValueError("同一批中片号不得重复")
        return self


class FeatureOut(BaseModel):
    depth: int
    closed: bool
    points: list[list[int]]
    source_tiles: list[list[int]]


class RenderOut(BaseModel):
    status: Literal["ok", "seam_errors"]
    grid: dict | None
    tile_count: int
    features: list[FeatureOut]
    seam_errors: list[dict]
