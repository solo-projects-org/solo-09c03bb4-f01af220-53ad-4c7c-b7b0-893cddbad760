from app.seed import BAD_BATCH, GOOD_BATCH, SEED_TILES


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_full_flow_seed_render(seeded_client):
    r = seeded_client.get("/api/render")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["tile_count"] == 4
    closed = [f for f in body["features"] if f["closed"]]
    opened = [f for f in body["features"] if not f["closed"]]
    assert len(closed) == 1 and closed[0]["depth"] == 10
    assert sorted(f["depth"] for f in opened) == [3, 5]


def test_grid_required_before_batch(client):
    # 空 tiles / 空 polylines 先被 pydantic 拦截
    r = client.post("/api/batches", json={"tiles": []})
    assert r.status_code == 422
    r = client.post(
        "/api/batches",
        json={"tiles": [{"row": 0, "col": 0, "polylines": []}]},
    )
    assert r.status_code == 422
    # 载荷合法但尚未定义网格 -> 409
    r = client.post(
        "/api/batches",
        json={
            "tiles": [
                {"row": 0, "col": 0,
                 "polylines": [{"depth": 5, "points": [[0, 30], [50, 35], [100, 30]]}]}
            ]
        },
    )
    assert r.status_code == 409
    assert "网格" in r.json()["detail"]


def test_bad_batch_rejected_and_old_map_kept(seeded_client):
    before = seeded_client.get("/api/render").json()
    r = seeded_client.post("/api/batches", json=BAD_BATCH)
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "rejected"
    assert body["errors"][0]["code"] == "seam_count_mismatch"
    # 旧图不变
    after = seeded_client.get("/api/render").json()
    assert after == before
    # 拒绝批次有审计记录
    batches = seeded_client.get("/api/batches").json()["batches"]
    assert batches[-1]["status"] == "rejected"
    assert batches[-1]["errors"]


def test_good_batch_committed_and_render_still_valid(seeded_client):
    r = seeded_client.post("/api/batches", json=GOOD_BATCH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "committed"

    rendered = seeded_client.get("/api/render").json()
    assert rendered["status"] == "ok"
    depths = sorted(f["depth"] for f in rendered["features"])
    assert depths == [3, 5, 10]
    # 水深 3 长线仍横贯图幅，且使用了新线型点 (45,30)
    d3 = next(f for f in rendered["features"] if f["depth"] == 3)
    assert [45, 30] in d3["points"]
    assert d3["points"][0] == [0, 20] and d3["points"][-1] == [200, 20]
    # 水深 5 来自未换的下排旧片，原样保留
    d5 = next(f for f in rendered["features"] if f["depth"] == 5)
    assert [50, 135] in d5["points"]


def test_tile_out_of_grid_rejected(seeded_client):
    r = seeded_client.post(
        "/api/batches",
        json={
            "tiles": [
                {"row": 9, "col": 0,
                 "polylines": [{"depth": 1, "points": [[0, 1], [5, 1], [100, 1]]}]}
            ]
        },
    )
    assert r.status_code == 422


def test_duplicate_tile_in_batch_rejected(client):
    client.post("/api/grid", json={"rows": 2, "cols": 2, "tile_size": 100})
    r = client.post(
        "/api/batches",
        json={
            "tiles": [
                {"row": 0, "col": 0, "polylines": []},
                {"row": 0, "col": 0, "polylines": []},
            ]
        },
    )
    assert r.status_code == 422


def test_non_integer_coordinates_rejected(client):
    client.post("/api/grid", json={"rows": 1, "cols": 1, "tile_size": 100})
    r = client.post(
        "/api/batches",
        json={
            "tiles": [
                {"row": 0, "col": 0,
                 "polylines": [{"depth": 5, "points": [[0, 30.5], [100, 30]]}]}
            ]
        },
    )
    assert r.status_code == 422


def test_tiles_listing_does_not_leak_polylines(seeded_client):
    r = seeded_client.get("/api/tiles")
    assert r.status_code == 200
    for t in r.json()["tiles"]:
        assert "polylines" not in t
        assert t["polyline_count"] >= 1


def test_grid_reset(seeded_client):
    r = seeded_client.post(
        "/api/grid", json={"rows": 3, "cols": 3, "tile_size": 50, "reset": True}
    )
    assert r.status_code == 200
    assert r.json()["grid"] == {"rows": 3, "cols": 3, "tile_size": 50}
    assert seeded_client.get("/api/tiles").json()["tiles"] == []


def test_seed_data_matches_seed_module(seeded_client):
    r = seeded_client.get("/api/batches")
    assert r.json()["batches"][0]["tile_count"] == len(SEED_TILES)


def test_corner_endpoint_rejected(client):
    client.post("/api/grid", json={"rows": 1, "cols": 1, "tile_size": 100})
    r = client.post(
        "/api/batches",
        json={
            "tiles": [
                {"row": 0, "col": 0,
                 "polylines": [{"depth": 1, "points": [[0, 0], [50, 50], [100, 0]]}]}
            ]
        },
    )
    assert r.status_code == 422
    assert r.json()["errors"][0]["code"] == "endpoint_not_on_edge"


def test_render_empty_map(client):
    client.post("/api/grid", json={"rows": 1, "cols": 1, "tile_size": 100})
    r = client.get("/api/render")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["features"] == [] and body["tile_count"] == 0
