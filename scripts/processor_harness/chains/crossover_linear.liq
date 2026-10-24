# Dial-in step 1 (crossover), linear phase: complementary FIR band-passes, summed. Each band is
# FFmpeg's firequalizer with a brick-wall gain curve over its own range; all share one window
# and one delay. The curves partition the spectrum, so the bands' impulse responses add up to
# the window times a single impulse: a pure delay of `delay` seconds, with no phase rotation.
# firequalizer stands in for afir because it designs these FIRs from the gain expression inside
# the graph; afir would need each impulse response fed in as a second audio stream.
# Params:
# - split: crossover frequencies, space-separated (default: today's 100 520 1800 6000)
# - delay: FIR half-length and latency, s. Default 0.02, locked in dial-in step 1 (2026-10-24):
#   pre-ringing reaches at most one delay ahead of a hit, and backward masking covers about
#   20 ms. With one band at -10 dB, 20 ms keeps every hit's pre-ring inside 20 ms; 50 ms put
#   -33 to -40 dB re peak more than 20 ms ahead on bands 1-2 (kick, real drum intros).
#   A longer delay gives steeper band edges (a Hann window's transition is about 2 / delay Hz
#   wide). Superseded default: 0.05 (before the pre-ringing test).
# - wfunc: firequalizer's window index (default 1, Hann)
# - solo: 0 sums every band; n renders band n alone, so the impulse shows its shape
# - gains: static gain per band in dB, space-separated (default all 0), standing in for the
#   unequal gains compression applies (pre-ringing tests); built into each band's FIR
def chain(s) =
  split = param_string("split", "100 520 1800 6000")
  delay = param_float("delay", 0.02)
  wfunc = param_int("wfunc", 1)
  solo = param_int("solo", 0)
  edges = list.map(float_of_string, string.split(separator=" ", split))
  n = list.length(edges) + 1
  gains = string.split(separator=" ", param_string("gains", ""))
  def band_db(i) =
    if list.length(gains) == n then list.nth(gains, i) else "0" end
  end
  # Band i passes [edge i-1, edge i): half-open, so no frequency lands in two bands.
  def band_gain(i) =
    if
      i == 0
    then
      "if(lt(f,#{list.nth(edges, 0)}),#{band_db(i)},-INF)"
    elsif
      i == n - 1
    then
      "if(gte(f,#{list.nth(edges, i - 1)}),#{band_db(i)},-INF)"
    else
      "if(gte(f,#{list.nth(edges, i - 1)})*lt(f,#{list.nth(edges, i)}),#{band_db(i)},-INF)"
    end
  end
  raw =
    ffmpeg.raw.encode.audio(
      %ffmpeg(%audio.raw(sample_format = "flt", sample_rate = 44100, channels = 2)), s
    )
  def mkfilter(graph) =
    dry = ffmpeg.filter.audio.input(graph, source.tracks(raw).audio)
    let (copies, _) = ffmpeg.filter.asplit(graph, outputs=n, dry)
    def band(i, x) =
      gain = if solo == 0 or solo == i + 1 then band_gain(i) else "-INF" end
      ffmpeg.filter.firequalizer(graph, gain=gain, delay=delay, wfunc=wfunc, x)
    end
    mixed =
      ffmpeg.filter.amix(graph, inputs=n, normalize=false, list.mapi(band, copies), [])
    source({audio=ffmpeg.filter.audio.output(graph, mixed)})
  end
  ffmpeg.raw.decode.audio(ffmpeg.filter.create(mkfilter))
end
