"""Chain presets: one TOML file per chain variant, versioned.

A Liquidsoap preset names a chain file (a .liq defining ``chain(s)``, next to the preset); an
FFmpeg preset carries a filtergraph whose input is ``[0:a]`` and output ``[out]``, with
``$name`` placeholders filled from ``[params]``. A changed parameter bumps ``version`` and moves
the old value into ``[[superseded]]`` (param, value, date, reason) instead of being overwritten,
so the active value is never ambiguous.

    id = "bypass"
    version = 1
    backend = "liquidsoap"
    chain = "../chains/bypass.liq"
    ceiling_dbtp = -1.0        # optional; P5's starting point
    description = "..."
    [params]
    target = -16.0
    [gauge_limits]             # optional: a logged gauge's range, for its pinned time
    agc_gain = [-10.0, 10.0]
    [model]                    # optional: what the chain's compressor is set to (item 3a);
    kind = "acompressor"       # the stepped-tone and burst fits then run that model
    threshold_db = -24.0
    ratio = 4.0
    knee_db = 9.03             # knee width; FFmpeg's knee = 10^(knee_db / 20)
    makeup_db = 6.0
    attack_ms = 20.0
    release_ms = 250.0
    detection = "rms"          # or "peak"
    link = "average"           # or "maximum"

A multiband preset's ``split`` param (crossover frequencies, space-separated) gives its band
edges; with ``threshold_db``, ``ratio``, ``knee_db``, ``makeup_db``, ``attack_ms`` and
``release_ms`` as one value per band (space-separated) and ``detection`` (0 peak, 1 rms) and
``link`` (0 average, 1 maximum), it also gives each band's compressor settings. ``shared``
names bands (1-based, ascending, two or more) that compress from one detector fed by their
sum.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import cast

from processor_harness.metrics.acompressor import Settings

type ParamValue = bool | int | float | str

DEFAULT_CEILING_DBTP = -1.0


class Backend(StrEnum):
    LIQUIDSOAP = "liquidsoap"
    FFMPEG = "ffmpeg"


class PresetError(ValueError):
    """A preset file is missing a field or has a bad value."""


@dataclass(frozen=True)
class Superseded:
    param: str
    value: ParamValue
    date: str
    reason: str


@dataclass(frozen=True)
class Preset:
    id: str
    version: int
    backend: Backend
    chain: Path | None
    graph: str | None
    params: Mapping[str, ParamValue]
    ceiling_dbtp: float
    description: str
    superseded: tuple[Superseded, ...]
    source: Path
    model: Settings | None = None
    gauge_limits: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    level_check: tuple[str, float] | None = None  # (gauge, target LUFS) for playlists
    band_edges: tuple[float, ...] = ()
    band_models: tuple[Settings, ...] = ()  # one per band, from the per-band params
    band_shared: tuple[int, ...] = ()  # 1-based bands sharing one detector


def _param_value(path: Path, name: str, value: object) -> ParamValue:
    if isinstance(value, bool | int | float | str):
        return value
    raise PresetError(f"{path}: param {name!r} must be a number, boolean or string")


def _superseded(path: Path, entry: object) -> Superseded:
    fields = ("param", "value", "date", "reason")
    if not isinstance(entry, dict) or any(f not in entry for f in fields):
        raise PresetError(f"{path}: each [[superseded]] needs {', '.join(fields)}")
    return Superseded(
        param=str(entry["param"]),
        value=_param_value(path, str(entry["param"]), entry["value"]),
        date=str(entry["date"]),
        reason=str(entry["reason"]),
    )


def _required(path: Path, data: Mapping[str, object], field: str) -> object:
    if field not in data:
        raise PresetError(f"{path}: missing field {field!r}")
    return data[field]


def _backend(path: Path, data: Mapping[str, object]) -> Backend:
    value = _required(path, data, "backend")
    try:
        return Backend(cast(str, value))
    except ValueError:
        choices = ", ".join(b.value for b in Backend)
        raise PresetError(f"{path}: backend {value!r} is not one of {choices}") from None


def _chain(path: Path, data: Mapping[str, object]) -> Path:
    chain = (path.parent / str(_required(path, data, "chain"))).resolve()
    if not chain.is_file():
        raise PresetError(f"{path}: chain file not found: {chain}")
    return chain


_MODEL_NUMBERS = ("threshold_db", "ratio", "knee_db", "makeup_db", "attack_ms", "release_ms")
_DETECTION = {"rms": True, "peak": False}
_LINK = {"maximum": True, "average": False}


def _model(path: Path, table: object) -> Settings | None:
    """The [model] table as acompressor settings, or None when the preset has none."""
    if table is None:
        return None
    if not isinstance(table, dict) or table.get("kind") != "acompressor":
        raise PresetError(f'{path}: [model] kind must be "acompressor"')
    numbers = {}
    for name in _MODEL_NUMBERS:
        value = table.get(name)
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise PresetError(f"{path}: [model] {name} must be a number")
        numbers[name] = float(value)
    detection, link = table.get("detection", "rms"), table.get("link", "average")
    if detection not in _DETECTION or link not in _LINK:
        raise PresetError(f"{path}: [model] detection must be rms or peak, link average or maximum")
    return Settings.from_db(rms=_DETECTION[str(detection)], link_max=_LINK[str(link)], **numbers)


def _numbers(path: Path, params: Mapping[str, ParamValue], name: str) -> list[float]:
    """A space-separated list param as numbers."""
    try:
        return [float(v) for v in str(params[name]).split()]
    except ValueError:
        raise PresetError(f"{path}: param {name!r} must be numbers separated by spaces") from None


def _band_edges(path: Path, params: Mapping[str, ParamValue]) -> tuple[float, ...]:
    return tuple(_numbers(path, params, "split")) if "split" in params else ()


def _band_models(
    path: Path, params: Mapping[str, ParamValue], edges: tuple[float, ...]
) -> tuple[Settings, ...]:
    """One acompressor setting per band, when the preset sets the bands' compressors."""
    if not edges or "threshold_db" not in params:
        return ()
    count = len(edges) + 1
    lists = {}
    for name in _MODEL_NUMBERS:
        if name not in params:
            raise PresetError(f"{path}: param {name!r} is missing (one value per band)")
        lists[name] = _numbers(path, params, name)
        if len(lists[name]) != count:
            raise PresetError(f"{path}: param {name!r} needs {count} values, one per band")
    rms = int(params.get("detection", 1)) == 1
    link_max = int(params.get("link", 0)) == 1
    return tuple(
        Settings.from_db(**{n: v[i] for n, v in lists.items()}, rms=rms, link_max=link_max)
        for i in range(count)
    )


def _band_shared(
    path: Path, params: Mapping[str, ParamValue], edges: tuple[float, ...]
) -> tuple[int, ...]:
    """The bands sharing one detector, checked against the preset's bands."""
    if "shared" not in params:
        return ()
    bands = [int(v) for v in _numbers(path, params, "shared")]
    valid = len(bands) >= 2 and bands == sorted(set(bands))
    if not valid or bands[0] < 1 or bands[-1] > len(edges) + 1:
        raise PresetError(
            f"{path}: param 'shared' must name two or more of bands 1-{len(edges) + 1}, "
            "in ascending order"
        )
    return tuple(bands)


def _gauge_limits(path: Path, table: object) -> dict[str, tuple[float, float]]:
    """[gauge_limits]: each gauge name to its [low, high] range."""
    if table is None:
        return {}
    limits = {}
    if not isinstance(table, dict):
        raise PresetError(f"{path}: [gauge_limits] must be a table of [low, high] pairs")
    for name, pair in table.items():
        if not (isinstance(pair, list) and len(pair) == 2):
            raise PresetError(f"{path}: [gauge_limits] {name} must be [low, high]")
        limits[str(name)] = (float(pair[0]), float(pair[1]))
    return limits


def _level_check(path: Path, table: object) -> tuple[str, float] | None:
    """[level_check]: the gauge holding the chain's gain (dB) and the LUFS it aims at."""
    if table is None:
        return None
    if not isinstance(table, dict) or not isinstance(table.get("gauge"), str):
        raise PresetError(f"{path}: [level_check] needs gauge (a name) and target_lufs")
    target = table.get("target_lufs")
    if not isinstance(target, int | float) or isinstance(target, bool):
        raise PresetError(f"{path}: [level_check] target_lufs must be a number")
    return str(table["gauge"]), float(target)


def load_preset(path: Path) -> Preset:
    """Read and validate a preset file; errors name the file and the field."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    preset_id = _required(path, data, "id")
    version = _required(path, data, "version")
    if not isinstance(preset_id, str) or not preset_id:
        raise PresetError(f"{path}: id must be a non-empty string")
    if not isinstance(version, int) or version < 1:
        raise PresetError(f"{path}: version must be a whole number from 1")
    backend = _backend(path, data)
    params = {name: _param_value(path, name, v) for name, v in data.get("params", {}).items()}
    edges = _band_edges(path, params)
    return Preset(
        id=preset_id,
        version=version,
        backend=backend,
        chain=_chain(path, data) if backend is Backend.LIQUIDSOAP else None,
        graph=str(_required(path, data, "graph")) if backend is Backend.FFMPEG else None,
        params=params,
        ceiling_dbtp=float(data.get("ceiling_dbtp", DEFAULT_CEILING_DBTP)),
        description=str(data.get("description", "")),
        superseded=tuple(_superseded(path, e) for e in data.get("superseded", [])),
        source=path,
        model=_model(path, data.get("model")),
        gauge_limits=_gauge_limits(path, data.get("gauge_limits")),
        level_check=_level_check(path, data.get("level_check")),
        band_edges=edges,
        band_models=_band_models(path, params, edges),
        band_shared=_band_shared(path, params, edges),
    )
