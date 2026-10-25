"""The two per-preset stages: render every input through the preset, then measure the renders.

The impulse is measured first: its latency lines the output up with the input for the
tone-burst time-constant fit. Each preset also renders one second of silence first; its CPU
time is the chain's startup cost (script compile, graph setup), taken off every programme
render's CPU time before the real-time factor is worked out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from processor_harness.audio_io import Audio, read_wav
from processor_harness.codec import decode, encode_mp3
from processor_harness.measure import PROGRAMME, SEQUENCE, Rendered, measure, read_gauges
from processor_harness.prepare import PreparedInput
from processor_harness.presets import Backend, ParamValue, Preset
from processor_harness.render import (
    RenderRun,
    fill_graph,
    render_ffmpeg,
    render_liquidsoap,
    reuse_run,
)


@dataclass(frozen=True)
class Tools:
    ffmpeg: Path
    liquidsoap: Path | None
    cache_dir: Path  # Liquidsoap's script cache, kept between renders and runs


@dataclass(frozen=True)
class PresetRenders:
    runs: Mapping[str, RenderRun]
    startup_cpu_s: float


def render_params(preset: Preset, item: PreparedInput) -> dict[str, ParamValue]:
    """The preset's params, plus a playlist's song start times as ``boundaries`` (seconds,
    space-separated), so a chain can do at each song start what session.liq does."""
    params = dict(preset.params)
    if item.kind == SEQUENCE:
        params["boundaries"] = " ".join(str(seg.start_s) for seg in item.segments)
    return params


def _render(
    preset: Preset,
    tools: Tools,
    source: Path,
    output: Path,
    params: Mapping[str, ParamValue] | None = None,
) -> RenderRun:
    params = preset.params if params is None else params
    if preset.backend is Backend.LIQUIDSOAP:
        if tools.liquidsoap is None or preset.chain is None:
            raise ValueError(f"{preset.source}: a Liquidsoap preset needs --liquidsoap")
        return render_liquidsoap(
            tools.liquidsoap, preset.chain, params, source, output, tools.cache_dir
        )
    if preset.graph is None:
        raise ValueError(f"{preset.source}: an FFmpeg preset needs a graph")
    return render_ffmpeg(tools.ffmpeg, fill_graph(preset.graph, params), source, output)


def render_inputs(
    preset: Preset,
    inputs: Sequence[PreparedInput],
    tools: Tools,
    directory: Path,
    startup_source: Path,
    reuse: bool = False,
) -> PresetRenders:
    """Render the startup probe, then every input, into ``directory``. With ``reuse``, a
    render already in ``directory`` is measured as it is instead of being made again."""
    directory.mkdir(parents=True, exist_ok=True)
    liquidsoap = preset.backend is Backend.LIQUIDSOAP

    def made(source: Path, output: Path, params: Mapping[str, ParamValue]) -> RenderRun:
        found = reuse_run(output, liquidsoap) if reuse else None
        return found or _render(preset, tools, source, output, params)

    startup = made(startup_source, directory / "_startup.wav", preset.params)
    runs = {i.id: made(i.path, directory / f"{i.id}.wav", render_params(preset, i)) for i in inputs}
    return PresetRenders(runs, startup.cpu_seconds)


def _decoded(run: RenderRun, ffmpeg: Path) -> Audio:
    """The render after the stream's codec: the Liquidsoap back end wrote its MP3 already."""
    mp3 = run.mp3 or encode_mp3(ffmpeg, run.output, run.output.with_suffix(".mp3"))
    return read_wav(decode(ffmpeg, mp3, run.output.with_suffix(".decoded.wav")))[0]


def measure_renders(
    preset: Preset, inputs: Sequence[PreparedInput], renders: PresetRenders, ffmpeg: Path
) -> dict[str, dict[str, float]]:
    """Metrics per input id, in manifest order."""
    latency_s = 0.0
    metrics: dict[str, dict[str, float]] = {}
    for item in sorted(inputs, key=lambda i: i.kind != "impulse"):
        run = renders.runs[item.id]
        inp, fs = read_wav(item.path)
        rendered = Rendered(
            inp=inp,
            out=read_wav(run.output)[0],
            fs=fs,
            segments=item.segments,
            gauges=read_gauges(run.gauges) if run.gauges else {},
            decoded=_decoded(run, ffmpeg) if item.kind == PROGRAMME else None,
            ceiling_dbtp=preset.ceiling_dbtp,
            latency_s=latency_s,
            category=item.category,
            model=preset.model,
            gauge_limits=preset.gauge_limits,
            level_check=preset.level_check,
            band_edges=preset.band_edges,
        )
        row = measure(item.kind, rendered)
        if item.kind == "impulse":
            latency_s = row["latency_ms"] / 1000
        if item.kind == PROGRAMME:
            cpu = max(0.0, run.cpu_seconds - renders.startup_cpu_s)
            row["rtf"] = cpu / run.audio_seconds
        metrics[item.id] = row
    return {i.id: metrics[i.id] for i in inputs}
