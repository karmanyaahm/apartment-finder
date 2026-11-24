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
