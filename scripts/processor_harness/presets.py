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
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
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
    params = data.get("params", {})
    return Preset(
        id=preset_id,
        version=version,
        backend=backend,
        chain=_chain(path, data) if backend is Backend.LIQUIDSOAP else None,
        graph=str(_required(path, data, "graph")) if backend is Backend.FFMPEG else None,
        params={name: _param_value(path, name, v) for name, v in params.items()},
        ceiling_dbtp=float(data.get("ceiling_dbtp", DEFAULT_CEILING_DBTP)),
        description=str(data.get("description", "")),
        superseded=tuple(_superseded(path, e) for e in data.get("superseded", [])),
        source=path,
        model=_model(path, data.get("model")),
    )
