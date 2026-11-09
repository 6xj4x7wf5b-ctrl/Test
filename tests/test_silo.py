import math

import pytest

from warehouse_pc.silo_model import build_silo_snapshot


def snapshot(count=4, rows=(), layout=None, **kwargs):
    options = {"connected": True, "stale": False}
    options.update(kwargs)
    return build_silo_snapshot(
        {"id": 7, "grain_count": count, "source": "simulator"},
        rows, layout or {}, **options,
    )


def reading(point, temp=20.0, status="ok", kind="grain"):
    return {"point": point, "kind": kind, "temp": temp, "status": status}


def points(result):
    return [point for layer in result.layers for point in layer.points]


def test_unmapped_full_device_is_paged_without_inventing_layers():
    result = snapshot(165, [reading(1), reading(1, 90.0, kind="ambient")])
    assert len(result.layers) == 7
    assert [point.point for point in points(result)] == list(range(1, 166))
    assert all(not layer.registered and len(layer.points) <= 24 for layer in result.layers)
    assert all(not point.layer for point in points(result))
    assert points(result)[0].temperature == 20.0
    assert points(result)[1].temperature is None
    assert points(result)[1].status == "not_ready"


def test_registered_cable_slots_stay_stable_across_sparse_layers():
    result = snapshot(4, layout={
        "grain:1": {"line": "10", "layer": "2"},
        "grain:2": {"line": "2", "layer": "2"},
        "grain:3": {"line": "2", "layer": "10"},
        "grain:4": {"line": "1", "layer": "2"},
    })
    assert [layer.label for layer in result.layers] == ["第2层", "第10层"]
    assert all(layer.slots == ("1", "2", "10") for layer in result.layers)
    assert [point.point for point in result.layers[0].points] == [4, 2, 1]
    assert result.layers[1].points[0].slot == "2"


def test_conflicts_and_partial_mappings_do_not_hide_or_overlap_points():
    result = snapshot(layout={
        "grain:1": {"line": "1", "layer": "1"},
        "grain:2": {"line": "1", "layer": "1"},
        "grain:3": {"line": "2", "layer": "1", "position": "西侧"},
        "grain:4": {"line": "2"},
    })
    assert sorted(point.point for point in points(result)) == [1, 2, 3, 4]
    assert [point.point for point in result.layers[0].points] == [3]
    assert result.layers[0].points[0].position == "西侧"
    assert [point.point for point in result.layers[1].points] == [1, 2, 4]
    assert len(set(result.layers[1].slots)) == 3
    assert "2 点的线 / 层重复" in result.message


def test_equal_natural_labels_have_stable_cable_and_layer_order():
    result = snapshot(4, layout={
        "grain:1": {"line": "a", "layer": "1"},
        "grain:2": {"line": "A", "layer": "1"},
        "grain:3": {"line": "01", "layer": "01"},
        "grain:4": {"line": "1", "layer": "01"},
    })
    assert [layer.label for layer in result.layers] == ["第01层", "第1层"]
    assert all(layer.slots == ("01", "1", "A", "a") for layer in result.layers)


@pytest.mark.parametrize("options, expected", [
    ({"connected": False}, "offline"), ({"stale": True}, "stale"),
])
def test_old_values_and_alarms_are_not_shown_as_current(options, expected):
    result = snapshot(1, [reading(1, 42)], alarms=[
        {"device_id": 7, "kind": "grain", "point": 1, "active": 1},
    ], **options)
    point = points(result)[0]
    assert point.status == expected
    assert point.temperature is None
    assert not point.alarm


def test_invalid_values_and_probe_faults_are_never_normal_temperatures():
    result = snapshot(6, [reading(1, math.nan), reading(2, math.inf),
                          reading(3, True), reading(4, None),
                          reading(5, 0, "crc_error"), reading(6, -19.8)])
    assert all(point.temperature is None for point in points(result)[:5])
    assert points(result)[4].status == "crc_error"
    assert points(result)[5].temperature == -19.8


def test_alarm_uses_device_and_kind_identity_and_keeps_acknowledged_active_alarm():
    result = snapshot(4, [reading(i) for i in range(1, 5)], alarms=[
        {"device_id": 8, "kind": "grain", "point": 1, "active": 1},
        {"device_id": 7, "kind": "ambient", "point": 2, "active": 1},
        {"device_id": 7, "kind": "grain", "point": 3, "active": 0},
        {"device_id": 7, "kind": "grain", "point": 4, "active": 1, "acknowledged_at": "2026-10-16"},
    ])
    assert [point.point for point in points(result) if point.alarm] == [4]


def test_cable_grouping_preserves_all_registered_points():
    layout = {f"grain:{i}": {"line": str((i - 1) // 5 + 1), "layer": str((i - 1) % 5 + 1)}
              for i in range(1, 166)}
    result = snapshot(165, layout=layout)
    assert len(result.layers) == 10
    assert len(points(result)) == 165
    assert len({point.point for point in points(result)}) == 165
    assert all(len(layer.slots) <= 24 and layer.registered for layer in result.layers)


def test_no_device_or_ambient_only_has_no_grain_geometry():
    assert snapshot(0, [reading(1, kind="ambient")]).layers == ()
    assert build_silo_snapshot(None, [], {}, connected=False, stale=False).device_id is None


def test_view_preserves_layer_and_selection_on_refresh_but_resets_on_device_change(qt_application):
    from dataclasses import replace
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from warehouse_pc.silo_ui import SiloView

    view = SiloView()
    view.resize(780, 330)
    view.show()
    selected = []
    view.point_selected.connect(selected.append)
    try:
        result = snapshot(50, [reading(i) for i in range(1, 51)])
        view.set_snapshot(result)
        view.layer_combo.setCurrentIndex(2)
        qt_application.processEvents()
        region = next(item for item in view.canvas.hit_regions if item[1].point == 49)
        QTest.mouseClick(view.canvas, Qt.MouseButton.LeftButton, pos=region[0].center().toPoint())
        assert selected == [49]
        assert view.selected_point == 49
        key = view.layer_combo.currentData()
        view.display_combo.setCurrentIndex(1)
        view.set_snapshot(snapshot(50, [reading(i, 21.7) for i in range(1, 51)]))
        assert view.layer_combo.currentData() == key
        assert view.selected_point == 49
        assert "21.7" in view.selection_label.text()
        assert view.display_combo.currentData() == "point"
        view.set_snapshot(replace(result, device_id=8))
        assert view.layer_combo.currentIndex() == 0
        assert view.selected_point is None
        assert view.canvas.selected_point is None
    finally:
        view.close()


def test_sparse_layer_empty_slot_cannot_select_an_invented_sensor(qt_application):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from warehouse_pc.silo_ui import SiloView

    view = SiloView()
    view.resize(780, 330)
    view.show()
    selected = []
    view.point_selected.connect(selected.append)
    try:
        view.set_snapshot(snapshot(3, layout={
            "grain:1": {"line": "1", "layer": "1"},
            "grain:2": {"line": "2", "layer": "1"},
            "grain:3": {"line": "1", "layer": "2"},
        }))
        view.layer_combo.setCurrentIndex(1)
        qt_application.processEvents()
        region = next(item for item in view.canvas.hit_regions if item[1] is None)
        QTest.mouseClick(view.canvas, Qt.MouseButton.LeftButton, pos=region[0].center().toPoint())
        assert selected == []
        assert view.selected_point is None
    finally:
        view.close()
