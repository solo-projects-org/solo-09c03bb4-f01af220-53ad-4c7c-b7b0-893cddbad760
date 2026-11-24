"""整图接图：把各片折线拼接成端点位于图幅外边的长线或闭环。

同时承担“按整批换完的图验缝”中对批界缝（新片 ↔ 未换旧片）的核对：
* 图幅外边的节点度数必须为 1（一条长线的端点）；
* 图幅内部节点（含内缝、批界缝）度数必须为 2，且同位置同水深；
  度数不为 2 即“接不上”，返回指明接缝的错误。
"""

from __future__ import annotations

from .geometry import Grid

# 半边：(片行, 片列, 片内折线序号, 端名)
HalfEdge = tuple[int, int, int, str]


def _node_kind(grid: Grid, x: int, y: int) -> str:
    s = grid.tile_size
    width = grid.cols * s
    height = grid.rows * s
    if x in (0, width) or y in (0, height):
        return "outer"
    return "inner"


def assemble(grid: Grid, tiles: list[dict]) -> tuple[list[dict], list[dict]]:
    """接图。

    参数 ``tiles`` 为当前图幅版本下各片的折线：
    ``[{"row", "col", "polylines": [{"depth", "points": [[x, y], ...]}]}]``。

    返回 ``(features, errors)``：
      * ``features``：``{"depth", "closed", "points", "source_tiles"}``，
        闭环的 ``points`` 首尾重复表示闭合；
      * ``errors``：接不上的接缝错误；即便有错也会尽量返回可拼接部分。
    """
    # 折线库：edge_id -> (深度, 点序列, 片号)
    edges: dict[tuple[int, int, int], tuple[int, list[tuple[int, int]], tuple[int, int]]] = {}
    nodes: dict[tuple[int, int, int], list[HalfEdge]] = {}

    for t in tiles:
        row, col = t["row"], t["col"]
        for i, pl in enumerate(t["polylines"]):
            pts = [tuple(p) for p in pl["points"]]
            depth = int(pl["depth"])
            eid = (row, col, i)
            edges[eid] = (depth, pts, (row, col))
            for end_name, pt in (("start", pts[0]), ("end", pts[-1])):
                nodes.setdefault((*pt, depth), []).append((row, col, i, end_name))

    errors: list[dict] = []
    for (x, y, depth), halves in nodes.items():
        kind = _node_kind(grid, x, y)
        expected = 1 if kind == "outer" else 2
        if len(halves) != expected:
            tile_sides = sorted({(h[0], h[1]) for h in halves})
            errors.append(
                {
                    "code": (
                        "outer_endpoint_conflict"
                        if kind == "outer"
                        else "seam_dangling"
                        if len(halves) < 2
                        else "seam_collision"
                    ),
                    "message": (
                        f"图幅{('外边' if kind == 'outer' else '内部')}节点 "
                        f"({x},{y}) 水深 {depth} 连接了 {len(halves)} 条折线，"
                        f"应为 {expected} 条"
                    ),
                    "point": [x, y],
                    "depth": depth,
                    "tiles": [list(t) for t in tile_sides],
                }
            )

    # 半边邻接表：半边 -> 对端半边（按节点配对）
    partner: dict[HalfEdge, HalfEdge] = {}
    for halves in nodes.values():
        if len(halves) == 2:
            partner[halves[0]] = halves[1]
            partner[halves[1]] = halves[0]

    used: set[tuple[int, int, int]] = set()
    features: list[dict] = []

    def other_end(he: HalfEdge) -> str:
        return "end" if he[3] == "start" else "start"

    def edge_points(he: HalfEdge) -> list[tuple[int, int]]:
        row, col, i, end_name = he
        _, pts, _ = edges[(row, col, i)]
        return pts if end_name == "start" else list(reversed(pts))

    def walk(start_he: HalfEdge) -> tuple[list[tuple[int, int]], int, set, list]:
        """从半边 ``start_he`` 出发沿 partner 行走，返回(点序列, 深度, 用到的边, 片)。"""
        out_points: list[tuple[int, int]] = []
        depth: int | None = None
        walked: set[tuple[int, int, int]] = set()
        source_tiles: list[tuple[int, int]] = []
        cur = start_he
        while True:
            eid = (cur[0], cur[1], cur[2])
            if eid in walked:
                break
            d, _, tile = edges[eid]
            depth = d
            walked.add(eid)
            pts = edge_points(cur)
            if not out_points:
                out_points.extend(pts)
            else:
                out_points.extend(pts[1:])  # 跳过重复的缝合点
            source_tiles.append(tile)
            exit_he = (cur[0], cur[1], cur[2], other_end(cur))
            nxt = partner.get(exit_he)
            if nxt is None or (nxt[0], nxt[1], nxt[2]) in walked:
                break
            cur = nxt
        assert depth is not None
        return out_points, depth, walked, source_tiles

    # 1) 从图幅外边节点出发，拼长线
    for (x, y, depth), halves in nodes.items():
        if _node_kind(grid, x, y) != "outer":
            continue
        for he in halves:
            eid = (he[0], he[1], he[2])
            if eid in used:
                continue
            pts, d, walked, src = walk(he)
            used.update(walked)
            features.append(
                {
                    "depth": d,
                    "closed": False,
                    "points": [list(p) for p in pts],
                    "source_tiles": [list(t) for t in dict.fromkeys(src)],
                }
            )

    # 2) 剩余的边构成闭环
    for eid in list(edges.keys()):
        if eid in used:
            continue
        start_he = (eid[0], eid[1], eid[2], "start")
        pts, d, walked, src = walk(start_he)
        used.update(walked)
        if pts[0] != pts[-1]:
            pts.append(pts[0])  # 闭环首尾重复
        features.append(
            {
                "depth": d,
                "closed": True,
                "points": [list(p) for p in pts],
                "source_tiles": [list(t) for t in dict.fromkeys(src)],
            }
        )

    # 自检：每条原折线恰好使用一次
    assert used == set(edges.keys()), "接图未覆盖全部折线"
    features.sort(key=lambda f: (f["depth"], not f["closed"], f["points"][0]))
    return features, errors
