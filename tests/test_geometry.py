from app.geometry import Grid, validate_batch, validate_tile_polylines

G = Grid(rows=2, cols=2, tile_size=100)


def test_valid_tile():
    eps, errors = validate_tile_polylines(
        G, 0, 0, [{"depth": 5, "points": [[0, 30], [40, 35], [100, 30]]}]
    )
    assert errors == []
    assert {(e.edge, e.x, e.y) for e in eps} == {
        ("left", 0, 30),
        ("right", 100, 30),
    }


def test_endpoint_at_corner_rejected():
    _, errors = validate_tile_polylines(
        G, 0, 0, [{"depth": 5, "points": [[0, 0], [50, 50], [100, 100]]}]
    )
    assert any(e["code"] == "endpoint_not_on_edge" for e in errors)


def test_interior_point_on_edge_rejected():
    _, errors = validate_tile_polylines(
        G, 0, 0, [{"depth": 5, "points": [[0, 30], [100, 30], [50, 60]]}]
    )
    # (100,30) 作为中间点压边
    assert any(e["code"] == "interior_point_on_edge" for e in errors)


def test_point_outside_tile_rejected():
    _, errors = validate_tile_polylines(
        G, 0, 0, [{"depth": 5, "points": [[0, 30], [101, 30], [100, 20]]}]
    )
    assert any(e["code"] == "point_outside_tile" for e in errors)


def test_duplicate_consecutive_point_rejected():
    _, errors = validate_tile_polylines(
        G, 0, 0, [{"depth": 5, "points": [[0, 30], [40, 30], [40, 30], [100, 20]]}]
    )
    assert any(e["code"] == "duplicate_consecutive_point" for e in errors)


def test_duplicate_endpoint_same_edge_rejected():
    _, errors = validate_tile_polylines(
        G,
        0,
        0,
        [
            {"depth": 5, "points": [[0, 30], [50, 40], [100, 30]]},
            {"depth": 5, "points": [[0, 30], [50, 20], [100, 20]]},
        ],
    )
    assert any(e["code"] == "duplicate_seam_endpoint" for e in errors)


def test_batch_internal_seam_matches():
    tiles = [
        {"row": 0, "col": 0,
         "polylines": [{"depth": 5, "points": [[0, 30], [50, 35], [100, 30]]}]},
        {"row": 0, "col": 1,
         "polylines": [{"depth": 5, "points": [[100, 30], [150, 25], [200, 30]]}]},
    ]
    _, errors = validate_batch(G, tiles)
    assert errors == []


def test_batch_internal_seam_position_mismatch():
    tiles = [
        {"row": 0, "col": 0,
         "polylines": [{"depth": 5, "points": [[0, 30], [50, 35], [100, 30]]}]},
        {"row": 0, "col": 1,
         "polylines": [{"depth": 5, "points": [[100, 31], [150, 25], [200, 30]]}]},
    ]
    _, errors = validate_batch(G, tiles)
    # 同一接缝两侧各有一个对不上的端点，分别报出，均指明同一条缝
    assert len(errors) == 2
    assert all(e["code"] == "seam_count_mismatch" for e in errors)
    assert {tuple(e["seam"]) for e in errors} == {("v", 0, 0)}
    assert {tuple(e["point"]) for e in errors} == {(100, 30), (100, 31)}


def test_batch_internal_seam_depth_mismatch():
    tiles = [
        {"row": 0, "col": 0,
         "polylines": [{"depth": 5, "points": [[0, 30], [50, 35], [100, 30]]}]},
        {"row": 0, "col": 1,
         "polylines": [{"depth": 6, "points": [[100, 30], [150, 25], [200, 30]]}]},
    ]
    _, errors = validate_batch(G, tiles)
    assert any(e["code"] == "seam_count_mismatch" for e in errors)


def test_batch_missing_opposite_endpoint_detected():
    # 两片都换，但右片在共缝处没有对应端点
    tiles = [
        {"row": 0, "col": 0,
         "polylines": [{"depth": 5, "points": [[0, 30], [50, 35], [100, 30]]}]},
        {"row": 0, "col": 1,
         "polylines": [{"depth": 5, "points": [[200, 30], [150, 25], [100, 40]]}]},
    ]
    _, errors = validate_batch(G, tiles)
    codes = {e["code"] for e in errors}
    assert "seam_count_mismatch" in codes


def test_batch_boundary_seam_allows_single_side():
    # 只换左列：共缝属于批界缝，单侧端点允许（不读未换片数据）
    tiles = [
        {"row": 0, "col": 0,
         "polylines": [{"depth": 5, "points": [[0, 30], [50, 35], [100, 30]]}]},
    ]
    _, errors = validate_batch(G, tiles)
    assert errors == []


def test_outer_edge_endpoints_allowed():
    _, errors = validate_tile_polylines(
        G, 0, 0, [{"depth": 5, "points": [[0, 30], [50, 50], [50, 0]]}]
    )
    assert errors == []
