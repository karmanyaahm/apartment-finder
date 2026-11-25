from aptfinder.config import load_area, load_caltrain, load_goals, load_point_sets, load_points


def test_repo_config_loads():
    goals = load_goals()
    assert set(goals.goals) == {"temp", "lease"}
    assert load_area().cities
    points = load_points()
    assert len(points["caltrain_stations"]) == 8
    assert {p.name for p in points["commute_targets"]} >= {"Downtown Palo Alto", "Meta HQ"}
    assert points["google_bus_stops"] == []
    sets = load_point_sets()
    assert set(sets) <= set(points)
    caltrain = load_caltrain()
    station_names = {p.name for p in points["caltrain_stations"]}
    for pattern in caltrain["patterns"].values():
        assert set(pattern["offsets"]) <= station_names
    for set_name, pts in points.items():
        names = [p.name for p in pts]
        assert len(names) == len(set(names)), f"duplicate point names in {set_name}"


def test_areas_have_no_chunks_and_walk_zones_come_from_the_boxes(tmp_path, monkeypatch):
    import shutil

    from aptfinder import areas, settings
    from aptfinder.config import load_areas

    got = load_areas()
    assert {a.kind for a in got} >= {"zip", "city", "box", "site"}
    assert all("chunks" not in a.model_dump() for a in got)
    areas._cfg.cache_clear()
    assert len(areas.zones()["zones"]) == 8
    assert areas.in_walk_zone(37.3947, -122.0763)  # beside Mountain View station
    assert not areas.in_walk_zone(37.56, -122.27) and not areas.in_walk_zone(None, None)  # Foster City
    # an areas.toml still carrying the old chunk keys reads the same
    shutil.copytree(settings.ROOT / "config.example", tmp_path / "cfg")
    f = tmp_path / "cfg" / "areas.toml"
    f.write_text('chunks = ["a"]\nactive = ["a"]\nchunk_stations = { a = ["Mountain View"] }\n'
                 + f.read_text().replace("[[zip]]\n", '[[zip]]\nchunks = ["a"]\n'))
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path / "cfg")
    areas._cfg.cache_clear()
    assert [(a.kind, a.id) for a in load_areas()] == [(a.kind, a.id) for a in got]
    assert len(areas.zones()["zones"]) == 8
    areas._cfg.cache_clear()
