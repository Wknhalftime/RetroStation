"""Command line: prepare the corpus, then render and measure it through each preset, then write
the reports. Run through scripts/run_processor_harness.py."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from processor_harness.corpus import CorpusError, load_corpus
from processor_harness.pipeline import Tools, check_aimed_tones, measure_renders, render_inputs
from processor_harness.prepare import prepare_inputs, silence
from processor_harness.presets import Backend, Preset, PresetError, load_preset
from processor_harness.render import RenderError, ffmpeg_version, liquidsoap_version
from processor_harness.report import PresetResult, write_reports

HARNESS = Path(__file__).resolve().parent
DEFAULT_CACHE = Path("var") / "harness-cache"


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render the corpus through chain presets and report their metrics."
    )
    parser.add_argument("presets", nargs="+", type=Path, help="preset TOML files")
    parser.add_argument("--out", type=Path, required=True, help="run directory (var/harness/…)")
    parser.add_argument("--corpus", type=Path, default=HARNESS / "corpus.toml")
    parser.add_argument("--liquidsoap", type=Path, default=os.environ.get("LIQUIDSOAP_PATH"))
    parser.add_argument("--ffmpeg", type=Path, default=shutil.which("ffmpeg"))
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--reuse-renders",
        action="store_true",
        help="measure renders already in --out instead of making them again",
    )
    return parser.parse_args(argv)


def _tools(args: argparse.Namespace, presets: Sequence[Preset]) -> Tools:
    if args.ffmpeg is None or not Path(args.ffmpeg).is_file():
        raise RenderError("ffmpeg not found: pass --ffmpeg or put it on PATH")
    needs_liquidsoap = any(p.backend is Backend.LIQUIDSOAP for p in presets)
    if needs_liquidsoap and (args.liquidsoap is None or not Path(args.liquidsoap).is_file()):
        raise RenderError("a Liquidsoap preset needs --liquidsoap (or LIQUIDSOAP_PATH)")
    return Tools(Path(args.ffmpeg), args.liquidsoap and Path(args.liquidsoap), args.cache)


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        corpus = load_corpus(args.corpus)
        presets = [load_preset(p) for p in args.presets]
        for preset in presets:
            check_aimed_tones(preset, corpus.signals, corpus.sample_rate)
        tools = _tools(args, presets)
        inputs = prepare_inputs(corpus, args.out / "inputs", tools.ffmpeg)
        startup = silence(args.out / "inputs", corpus.sample_rate)
        standalone = ffmpeg_version(tools.ffmpeg)
        renderers = {Backend.FFMPEG: standalone}
        if tools.liquidsoap is not None:
            renderers[Backend.LIQUIDSOAP] = liquidsoap_version(tools.liquidsoap)
        results = []
        for preset in presets:
            directory = args.out / f"{preset.id}-v{preset.version}"
            renders = render_inputs(
                preset, inputs, tools, directory, startup, reuse=args.reuse_renders
            )
            metrics = measure_renders(preset, inputs, renders, tools.ffmpeg)
            results.append(
                PresetResult(
                    preset.id,
                    preset.version,
                    preset.backend.value,
                    renderers[preset.backend],
                    standalone,
                    metrics,
                )
            )
            # Reports after every preset, so a later failure cannot take earlier results.
            write_reports(results, args.out)
    except (CorpusError, PresetError, RenderError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"summary: {args.out / 'summary.md'}")
    return 0
