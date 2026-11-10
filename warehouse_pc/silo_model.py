"""Display-only layer mapping; never changes device numbering or probe bindings."""
from __future__ import annotations
from .domain import configured_point_keys

from collections import Counter
from dataclasses import dataclass
import math
import re


@dataclass(frozen=True)
class SiloPoint:
    point: int
    slot: str
    cable: str
    layer: str
    position: str
    temperature: float | None
    status: str
    alarm: bool
    registered: bool


@dataclass(frozen=True)
class SiloLayer:
    key: str
    label: str
    slots: tuple[str, ...]
    points: tuple[SiloPoint, ...]
    registered: bool


@dataclass(frozen=True)
class SiloSnapshot:
    device_id: int | None
    layers: tuple[SiloLayer, ...]
    message: str
    source: str
    grain_count: int


def _label(record, key):
    value = record.get(key, "") if isinstance(record, dict) else ""
    return value.strip() if isinstance(value, str) else ""


def _natural_key(value):
    return tuple(int(part) if part.isdecimal() else part.casefold()
                 for part in re.split(r"(\d+)", value))


def build_silo_snapshot(device, rows, layout, *, connected, stale, alarms=()):
    """Keep all configured grain points, including faults and missing samples.

    Named lines/layers are used only when the mapping is complete and unique.
    Unmapped points are paged by point number, never assigned invented layers.
    Ring placement is a display convention, not a surveyed physical position.
    """
    if not device:
        return SiloSnapshot(None, (), "请先选择仓房与分机。", "", 0)
    identifier = device["id"]
    grain_points = [point for kind, point in configured_point_keys(device) if kind == "grain"]
    count = len(grain_points)
    source = device.get("source", "serial")
    if not count:
        return SiloSnapshot(identifier, (), "该分机未配置粮温测点，仓温仓湿见上方环境数据。", source, 0)
    records = layout if isinstance(layout, dict) else {}
    labels = {point: tuple(_label(records.get(f"grain:{point}"), field)
                          for field in ("line", "layer", "position"))
              for point in grain_points}
    counts = Counter((line, layer) for line, layer, _ in labels.values() if line and layer)
    readings = {row["point"]: row for row in rows if row.get("kind") == "grain"}
    alarm_points = {row.get("point") for row in alarms
                    if row.get("device_id") == identifier and row.get("kind") == "grain" and row.get("active")}
    located, unplaced = [], []
    conflicts = 0
    for point in grain_points:
        cable, layer, position = labels[point]
        unique = bool(cable and layer and counts[(cable, layer)] == 1)
        if cable and layer and counts[(cable, layer)] > 1:
            conflicts += 1
        row = readings.get(point)
        status = ("offline" if not connected else "not_ready" if row is None
                  else "stale" if stale else row.get("status", "unknown"))
        temperature = None
        if status == "ok":
            value = row.get("temp")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                temperature = float(value)
            else:
                status = "data_error"
        item = SiloPoint(point, cable if unique else f"point:{point}", cable, layer, position,
                         temperature, status, point in alarm_points and temperature is not None, unique)
        (located if unique else unplaced).append(item)

    layers = []
    # Distinct labels can have equal natural keys ("1"/"01", "A"/"a").
    # Tie-break by the original label so ring positions survive an app restart.
    stable_key = lambda label: (_natural_key(label), label)
    cables = sorted({point.cable for point in located}, key=stable_key)
    for name in sorted({point.layer for point in located}, key=stable_key):
        for start in range(0, len(cables), 24):
            slots = tuple(cables[start:start + 24])
            points = tuple(sorted((p for p in located if p.layer == name and p.cable in slots),
                                  key=lambda p: slots.index(p.cable)))
            if not points:
                continue
            label = f"第{name}层" if name.isdecimal() else name
            if len(cables) > 24:
                label += f" · 线组 {start // 24 + 1}"
            layers.append(SiloLayer(f"layer:{name}:group:{start // 24}", label, slots, points, True))
    for start in range(0, len(unplaced), 24):
        points = tuple(unplaced[start:start + 24])
        label = "未布点示意"
        if len(unplaced) > 24:
            label += f" · 第 {start // 24 + 1} 组"
        layers.append(SiloLayer(f"unplaced:{start // 24}", label,
                                tuple(p.slot for p in points), points, False))
    if not unplaced:
        message = "按登记线 / 层展示；层序及环形位置为示意，不代表实际线距与埋深。"
    elif not located:
        message = "尚未完成布点：按点号分组示意，请通过“调整布点”登记测温线和层。"
    else:
        message = f"已布点 {len(located)} 点，未定位 {len(unplaced)} 点；未定位点在“未布点示意”分组中显示。"
    if conflicts:
        message += f"其中 {conflicts} 点的线 / 层重复，需检查布点。"
    return SiloSnapshot(identifier, tuple(layers), message, source, count)
