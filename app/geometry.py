"""网格几何、单片折线校验与整批验缝。

坐标约定
--------
* 直角坐标，单位为整数米（或任意整数单位）：``x`` 向右递增，``y`` 向上递增；
* 矩形图幅按固定方片（边长 ``tile_size``）划分，片号 ``(row, col)``，
  其中 ``row`` 自下而上、``col`` 自左而右，均从 0 开始；
* 片边端点必须落在片边上且避开四个角点；非端点必须严格位于片内。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# 边名：下、左、右、上
EDGES = ("bottom", "left", "right", "top")


@dataclass(frozen=True)
class Grid:
    """固定方片网格定义。"""

    rows: int
    cols: int
    tile_size: int

    def __post_init__(self) -> None:
        if self.rows <= 0 or self.cols <= 0 or self.tile_size <= 0:
            raise ValueError("rows、cols、tile_size 必须为正整数")

    def tile_bounds(self, row: int, col: int) -> tuple[int, int, int, int]:
        """返回片范围 ``(x_min, y_min, x_max, y_max)``。"""
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            raise ValueError(f"片号超出网格: row={row}, col={col}")
        s = self.tile_size
        return col * s, row * s, (col + 1) * s, (row + 1) * s

    def edge_of_point(
        self, x: int, y: int, row: int, col: int
    ) -> str | None:
        """判断点落在该片的哪条边；角点或不在边上时返回 ``None``。"""
        x_min, y_min, x_max, y_max = self.tile_bounds(row, col)
        if y == y_min and x_min < x < x_max:
            return "bottom"
        if y == y_max and x_min < x < x_max:
            return "top"
        if x == x_min and y_min < y < y_max:
            return "left"
        if x == x_max and y_min < y < y_max:
            return "right"
        return None

    # ------------------------------------------------------------------
    # 接缝定位
    # ------------------------------------------------------------------
    def seam_of_edge(
        self, row: int, col: int, edge: str
    ) -> tuple[str, tuple[int, int]] | None:
        """返回片边对应的图幅内缝键；图幅外边返回 ``None``。

        * 竖缝（左右相邻）键为 ``("v", col, y_row)``，其中 ``col`` 是缝左侧片的列号，
          ``y_row`` 是缝所在片行号；
        * 横缝（上下相邻）键为 ``("h", row, x_col)``，其中 ``row`` 是缝下方片的行号，
          ``x_col`` 是缝所在片列号。
        同一条缝从两侧计算得到的键完全相同。
        """
        if edge == "left" and col > 0:
            return ("v", (row, col - 1))
        if edge == "right" and col < self.cols - 1:
            return ("v", (row, col))
        if edge == "bottom" and row > 0:
            return ("h", (row - 1, col))
        if edge == "top" and row < self.rows - 1:
            return ("h", (row, col))
        return None

    def neighbor_across(
        self, row: int, col: int, edge: str
    ) -> tuple[int, int] | None:
        """返回边对面的相邻片号；图幅外边返回 ``None``。"""
        if edge == "left" and col > 0:
            return row, col - 1
        if edge == "right" and col < self.cols - 1:
            return row, col + 1
        if edge == "bottom" and row > 0:
            return row - 1, col
        if edge == "top" and row < self.rows - 1:
            return row + 1, col
        return None


@dataclass(frozen=True)
class SeamEndpoint:
    """片边上的一个端点。"""

    x: int
    y: int
    depth: int
    tile: tuple[int, int]
    edge: str
    polyline_index: int
    end: str  # "start" | "end"


def validate_tile_polylines(
    grid: Grid, row: int, col: int, polylines: Iterable[dict]
) -> tuple[list[SeamEndpoint], list[dict]]:
    """校验单片中的全部折线。

    返回 ``(端点列表, 错误列表)``。错误元素为
    ``{"code": ..., "message": ..., "tile": [r, c], "polyline_index": i}``。
    校验规则：
      1. 至少 2 个点，点坐标必须为整数且落在片范围（含边）内；
      2. 相邻点不得重合；
      3. 两个端点必须落在片边且避开角点；
      4. 中间点必须严格位于片内（不得压边）；
      5. 同一片同一条边上，同一 (位置, 水深) 至多出现一个端点
         （否则接图时无法唯一配对）。
    """
    x_min, y_min, x_max, y_max = grid.tile_bounds(row, col)
    endpoints: list[SeamEndpoint] = []
    errors: list[dict] = []
    seen_edge_keys: set[tuple] = set()

    def err(code: str, message: str, index: int) -> None:
        errors.append(
            {
                "code": code,
                "message": message,
                "tile": [row, col],
                "polyline_index": index,
            }
        )

    for i, pl in enumerate(polylines):
        pts = pl.get("points", [])
        depth = pl.get("depth")
        if not isinstance(depth, int) or isinstance(depth, bool):
            err("invalid_depth", "水深必须为整数", i)
            continue
        if not isinstance(pts, list) or len(pts) < 2:
            err("invalid_polyline", "折线至少包含 2 个点", i)
            continue

        valid_pts: list[tuple[int, int]] = []
        point_ok = True
        for p in pts:
            if (
                not isinstance(p, (list, tuple))
                or len(p) != 2
                or not all(isinstance(v, int) and not isinstance(v, bool) for v in p)
            ):
                err("invalid_point", "点坐标必须为两个整数", i)
                point_ok = False
                break
            x, y = p
            if not (x_min <= x <= x_max and y_min <= y <= y_max):
                err("point_outside_tile", f"点 ({x},{y}) 超出片范围", i)
                point_ok = False
                break
            valid_pts.append((x, y))
        if not point_ok:
            continue

        for a, b in zip(valid_pts, valid_pts[1:]):
            if a == b:
                err("duplicate_consecutive_point", f"相邻点重合: {a}", i)
                point_ok = False
                break
        if not point_ok:
            continue

        for j, (x, y) in enumerate(valid_pts):
            edge = grid.edge_of_point(x, y, row, col)
            if j in (0, len(valid_pts) - 1):
                if edge is None:
                    err(
                        "endpoint_not_on_edge",
                        f"端点 ({x},{y}) 未落在片边（角点不允许）",
                        i,
                    )
                    point_ok = False
            elif x in (x_min, x_max) or y in (y_min, y_max):
                err(
                    "interior_point_on_edge",
                    f"中间点 ({x},{y}) 不允许压在片边上",
                    i,
                )
                point_ok = False
        if not point_ok:
            continue

        for end_name, (x, y) in (
            ("start", valid_pts[0]),
            ("end", valid_pts[-1]),
        ):
            edge = grid.edge_of_point(x, y, row, col)
            assert edge is not None
            key = (edge, x, y, depth)
            if key in seen_edge_keys:
                err(
                    "duplicate_seam_endpoint",
                    f"{edge} 边上水深 {depth} 的端点 ({x},{y}) 在本片重复",
                    i,
                )
                continue
            seen_edge_keys.add(key)
            endpoints.append(
                SeamEndpoint(
                    x=x,
                    y=y,
                    depth=depth,
                    tile=(row, col),
                    edge=edge,
                    polyline_index=i,
                    end=end_name,
                )
            )

    return endpoints, errors


def validate_batch(
    grid: Grid, tiles: list[dict]
) -> tuple[list[SeamEndpoint], list[dict]]:
    """整批验缝（只依据本批提交数据，不读取未换片的任何折线明细）。

    规则：
      * 批内两片之间的内缝，同一 (位置, 水深) 的端点两侧必须各有且仅有一个；
      * 图幅外边、以及本批与未换片之间的批界缝上允许只留单侧端点
        （对侧情况由出图阶段结合当前整图版本核对）。
    """
    all_endpoints: list[SeamEndpoint] = []
    errors: list[dict] = []
    tile_ids = {(t["row"], t["col"]) for t in tiles}

    for t in tiles:
        eps, errs = validate_tile_polylines(grid, t["row"], t["col"], t["polylines"])
        all_endpoints.extend(eps)
        errors.extend(errs)

    # 按接缝 + 位置 + 水深归集端点
    buckets: dict[tuple, list[SeamEndpoint]] = {}
    for ep in all_endpoints:
        seam = grid.seam_of_edge(*ep.tile, ep.edge)
        if seam is None:
            continue  # 图幅外边，允许留端点
        kind, (a, b) = seam
        if kind == "v":
            seam_row, left_col = a, b
            key = ("v", seam_row, left_col, ep.x, ep.y, ep.depth)
        else:
            bottom_row, seam_col = a, b
            key = ("h", bottom_row, seam_col, ep.x, ep.y, ep.depth)
        buckets.setdefault(key, []).append(ep)

    # 枚举本批内部所有接缝签名（两侧片都在本批），逐一核对两侧端点。
    # 不查询未换片的任何数据；批界缝（一侧未换）只允许单侧端点，留到出图核对。
    tile_index = {(t["row"], t["col"]): t for t in tiles}
    internal_seam_keys: set[tuple] = set()
    for (row, col) in tile_ids:
        for edge, nbr in (
            ("right", (row, col + 1)),
            ("top", (row + 1, col)),
        ):
            if nbr not in tile_ids:
                continue
            seam = grid.seam_of_edge(row, col, edge)
            assert seam is not None
            kind, (a, b) = seam
            internal_seam_keys.add((kind, a, b))

    for kind, a, b in sorted(internal_seam_keys):
        # 该缝上本批出现过的全部 (位置, 水深) 组合
        related = [
            (k, eps)
            for k, eps in buckets.items()
            if k[0] == kind and k[1] == a and k[2] == b
        ]
        for key, eps in related:
            x, y, depth = key[3], key[4], key[5]
            sides = {ep.tile for ep in eps}
            if len(eps) == 2 and len(sides) == 2:
                e1, e2 = eps
                if (e1.x, e1.y) == (e2.x, e2.y) and e1.depth == e2.depth:
                    continue
            errors.append(
                {
                    "code": "seam_count_mismatch",
                    "message": (
                        f"内缝 {kind}({a},{b}) 上 ({x},{y}) 水深 {depth} "
                        f"的端点数量为 {len(eps)}（来自 {sorted(sides)} 片），两侧须各有一个"
                    ),
                    "seam": [kind, a, b],
                    "point": [x, y],
                    "depth": depth,
                    "tiles": [list(t) for t in sorted(sides)],
                }
            )

    return all_endpoints, errors
