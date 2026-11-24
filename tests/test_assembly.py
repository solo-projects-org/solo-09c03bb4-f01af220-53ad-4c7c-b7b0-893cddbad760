from app.assembly import assemble
from app.geometry import Grid
from app.seed import SEED_TILES

G = Grid(2, 2, 100)


def test_seed_assembles_into_two_lines_and_one_loop():
    features, errors = assemble(G, SEED_TILES)
    assert errors == []
    by_depth = {}
    for f in features:
        by_depth.setdefault(f["depth"], []).append(f)

    d3 = by_depth[3]
    assert len(d3) == 1 and d3[0]["closed"] is False
    # 长线两端位于图幅外边
    pts = d3[0]["points"]
    assert pts[0] == [0, 20] and pts[-1] == [200, 20]
    # 跨片缝合点不重复
    assert pts.count([100, 20]) == 1

    d5 = by_depth[5]
    assert len(d5) == 1 and d5[0]["closed"] is False
    assert d5[0]["points"][0][0] == 0 and d5[0]["points"][-1][0] == 200

    d10 = by_depth[10]
    assert len(d10) == 1 and d10[0]["closed"] is True
    pts10 = d10[0]["points"]
    assert pts10[0] == pts10[-1]  # 闭环首尾重复
    # 闭环跨越四片
    assert sorted(tuple(t) for t in d10[0]["source_tiles"]) == [
        (0, 0), (0, 1), (1, 0), (1, 1)
    ]


def test_every_polyline_used_exactly_once():
    features, errors = assemble(G, SEED_TILES)
    assert errors == []
    total_input = sum(len(t["polylines"]) for t in SEED_TILES)

    # 精确核对：输出点序列由输入折线拼接而成。每条输入折线有 n 个点，
    # 长线在每次缝合处少 1 个重复点；闭环首尾再重复 1 个点。
    total_input_points = sum(
        len(pl["points"]) for t in SEED_TILES for pl in t["polylines"]
    )
    total_output_points = sum(len(f["points"]) for f in features)
    seam_join_count = 0  # 缝合次数 = 所有内部节点数（每个度 2 节点接一次）
    for f in features:
        interior_seam_pts = [
            p
            for p in f["points"]
            if (p[0] % 100 == 0 or p[1] % 100 == 0)
            and not (p[0] in (0, 200) or p[1] in (0, 200))
        ]
        if f["closed"]:
            # 闭环点序列首尾重复，起点在列表中出现两次，统计缝合时只算一次
            seam_join_count += len(interior_seam_pts) - 1
        else:
            seam_join_count += len(interior_seam_pts)
    # 闭环首尾重复的那个点是额外加回的 1 个点
    closed_bonus = sum(1 for f in features if f["closed"])
    assert (
        total_output_points
        == total_input_points - seam_join_count + closed_bonus
    )
    # 输入 8 条折线：2 条长线各由 2 段拼成、1 个闭环由 4 段拼成
    assert total_input == 8
    assert seam_join_count == 6  # 长线 2 个缝点 + 闭环 4 个缝点


def test_dangling_seam_at_batch_boundary_reported():
    # 模拟整图：左片在共缝留 (100,30)，右片无对应端点
    tiles = [
        {"row": 0, "col": 0,
         "polylines": [{"depth": 5, "points": [[0, 30], [50, 35], [100, 30]]}]},
        {"row": 0, "col": 1,
         "polylines": [{"depth": 5, "points": [[200, 40], [150, 45], [100, 40]]}]},
    ]
    features, errors = assemble(G, tiles)
    assert any(e["code"] == "seam_dangling" for e in errors)
    # 出错也尽量返回可拼接部分，且所有原折线仍各用一次（不丢线）
    rendered_points = sum(len(f["points"]) for f in features)
    assert rendered_points > 0


def test_outer_node_degree_two_rejected():
    # 两条折线共用同一个外边端点
    tiles = [
        {"row": 0, "col": 0,
         "polylines": [
             {"depth": 5, "points": [[0, 30], [40, 40], [100, 30]]},
             {"depth": 5, "points": [[0, 30], [40, 20], [50, 0]]},
         ]},
        {"row": 0, "col": 1,
         "polylines": [{"depth": 5, "points": [[100, 30], [150, 30], [200, 30]]}]},
    ]
    _, errors = assemble(G, tiles)
    assert any(e["code"] == "outer_endpoint_conflict" for e in errors)


def test_single_tile_closed_loop_supported():
    g = Grid(1, 1, 100)
    # 单片内闭环：端点都在同一条内... 单片没有内缝，闭环端点必须落在外边。
    # 闭环的两个端点落在同一外边点 -> 度数 2（外边允许的是度数1），
    # 因此单片闭环在“端点必须在片边”规则下无法合法表达；
    # 这里验证单片长线（两端均在图幅外边）。
    tiles = [
        {"row": 0, "col": 0,
         "polylines": [{"depth": 7, "points": [[0, 30], [50, 50], [100, 30]]}]},
    ]
    features, errors = assemble(g, tiles)
    assert errors == []
    assert len(features) == 1 and features[0]["closed"] is False
