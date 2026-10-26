# The chain's first stage (target chain table: high-pass, math item 7; Lance, 2026-10-25): a
# Butterworth high-pass to take out rumble and DC offset ahead of the AGC and the multiband,
# built as a cascade of FFmpeg highpass biquads (double precision) with the Butterworth Qs.
# Liquidsoap 2.4.5's own filter.iir.butterworth.high diverges at orders 3 and 4 near 30 Hz
# (inf / NaN on an impulse, 2026-10-25). It is an IIR stage, so it turns the phase at the low
# end; the high-pass step checks it does not raise true peak on dense masters. No high-pass is
# bypass.liq.
# Params:
# - hp_hz: corner frequency (Hz, default 30)
# - hp_order: Butterworth order, even (default 2); section k of order / 2 has
#   Q = 1 / (2 cos(pi (2k - 1) / (2 order)))
def chain(s) =
  hz = param_float("hp_hz", 30.)
  order = param_int("hp_order", 2)
  half = order / 2
  def q_of(k) =
    1. / (2. * cos(3.141592653589793 * float(2 * k + 1) / float(2 * order)))
  end
  qs = list.map(q_of, list.init(half, fun (k) -> k))
  raw =
    ffmpeg.raw.encode.audio(
      %ffmpeg(%audio.raw(sample_format = "flt", sample_rate = 44100, channels = 2)), s
    )
  def mkfilter(graph) =
    x = ffmpeg.filter.audio.input(graph, source.tracks(raw).audio)
    def section(x, q) =
      ffmpeg.filter.highpass(graph, frequency=hz, width=q, poles=2, precision=3, x)
    end
    y = list.fold(section, x, qs)
    source({audio=ffmpeg.filter.audio.output(graph, y)})
  end
  ffmpeg.raw.decode.audio(ffmpeg.filter.create(mkfilter))
end
