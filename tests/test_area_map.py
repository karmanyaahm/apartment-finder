"""Search-area outlines for the Pipeline panel's map."""

from aptfinder.area_map import _core, _hull


def test_hull_and_stray_points():
    square = [(37.40, -122.10), (37.40, -122.08), (37.42, -122.10), (37.42, -122.08), (37.41, -122.09)]
    assert sorted(_hull(square)) == sorted(square[:4])  # the inner point is not on the outline
    stray = (38.24, -122.59)  # a listing pinned in Petaluma
    assert stray not in _core(square * 2 + [stray])
