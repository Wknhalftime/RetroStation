# Dial-in phase-rotator step (math item 8; Lance, 2026-10-25): an FFmpeg allpass cascade. It
# leaves every level alone and only turns the phase, which makes an asymmetric waveform (the
# male voice) more symmetric and lowers its peaks. On already-limited music it can bring back
# peaks the mastering limiter shaved, as R6 found for the IIR crossover's phase rotation.
# Params:
# - corners: one allpass section per corner frequency (Hz), space-separated, in order; none
#   (the default) passes the audio through untouched
# - order: 1 (90 degrees at each corner: the classic broadcast rotator, corners staggered
#   across the voice fundamentals) or 2 (180 degrees at the corner, stacked at one frequency)
# - q: the second-order sections' Q (default 0.707)
def chain(s) =
  corners =
    list.map(
      float_of_string,
      list.filter(fun (v) -> v != "", string.split(separator=" ", param_string("corners", "")))
    )
  order = param_int("order", 1)
  q = param_float("q", 0.707)
  raw =
    ffmpeg.raw.encode.audio(
      %ffmpeg(%audio.raw(sample_format = "flt", sample_rate = 44100, channels = 2)), s
    )
  def mkfilter(graph) =
    x = ffmpeg.filter.audio.input(graph, source.tracks(raw).audio)
    def section(x, f) =
      ffmpeg.filter.allpass(graph, frequency=f, width=q, order=order, x)
    end
    # No corners: the fold leaves x as it came, so the graph only passes the audio through.
    y = list.fold(section, x, corners)
    source({audio=ffmpeg.filter.audio.output(graph, y)})
  end
  ffmpeg.raw.decode.audio(ffmpeg.filter.create(mkfilter))
end
