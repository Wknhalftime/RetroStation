# Dial-in band step (band edges, static curves, time constants; Lance, 2026-10-25): the R6
# linear-phase crossover (crossover_linear.liq's bands: complementary firequalizer band-passes,
# one window, one delay) with one FFmpeg acompressor per band (sidechaincompress for bands that
# share a detector), summed. No makeup stage, clipper or limiter after it: the multiband
# shapes balance; peak control is the limiter's job (P4).
# Params:
# - split, delay, wfunc: as crossover_linear.liq (default edges 100 520 1800 6000, 20 ms)
# - threshold_db, ratio, knee_db (knee width), makeup_db, attack_ms, release_ms: one value per
#   band, space-separated, in dB and ms (the preset's band models read the same lists). A list
#   without one value per band falls back to that value's default for every band.
# - detection (0 peak, 1 rms) and link (0 average, 1 maximum): every band
# - shared: bands (1-based, ascending, two or more) that compress from one detector fed by
#   their sum, each through sidechaincompress keyed by it (default none: every band its own)
def chain(s) =
  split = param_string("split", "100 520 1800 6000")
  delay = param_float("delay", 0.02)
  wfunc = param_int("wfunc", 1)
  edges = list.map(float_of_string, string.split(separator=" ", split))
  n = list.length(edges) + 1
  def per_band(name, default) =
    values = list.filter(fun (v) -> v != "", string.split(separator=" ", param_string(name, "")))
    complete = list.length(values) == n
    fun (i) -> if complete then float_of_string(list.nth(values, i)) else default end
  end
  threshold_db = per_band("threshold_db", 0.)
  ratio = per_band("ratio", 1.)
  knee_db = per_band("knee_db", 9.03)
  makeup_db = per_band("makeup_db", 0.)
  attack_ms = per_band("attack_ms", 20.)
  release_ms = per_band("release_ms", 250.)
  detection = param_int("detection", 1)
  link = param_int("link", 0)
  shared =
    list.map(
      fun (v) -> int_of_string(v) - 1,
      list.filter(fun (v) -> v != "", string.split(separator=" ", param_string("shared", "")))
    )
  def is_shared(i) =
    list.length(shared) >= 2 and list.mem(i, shared)
  end
  # Band i passes [edge i-1, edge i): half-open, so no frequency lands in two bands.
  def band_gain(i) =
    if
      i == 0
    then
      "if(lt(f,#{list.nth(edges, 0)}),0,-INF)"
    elsif
      i == n - 1
    then
      "if(gte(f,#{list.nth(edges, i - 1)}),0,-INF)"
    else
      "if(gte(f,#{list.nth(edges, i - 1)})*lt(f,#{list.nth(edges, i)}),0,-INF)"
    end
  end
  raw =
    ffmpeg.raw.encode.audio(
      %ffmpeg(%audio.raw(sample_format = "flt", sample_rate = 44100, channels = 2)), s
    )
  def mkfilter(graph) =
    dry = ffmpeg.filter.audio.input(graph, source.tracks(raw).audio)
    let (copies, _) = ffmpeg.filter.asplit(graph, outputs=n, dry)
    def split2(x) =
      let (pair, _) = ffmpeg.filter.asplit(graph, outputs=2, x)
      pair
    end
    # Each band's filtered signal; a shared band's comes as [main, copy for the key].
    filtered =
      list.mapi(
        fun (i, x) ->
          begin
            y = ffmpeg.filter.firequalizer(graph, gain=band_gain(i), delay=delay, wfunc=wfunc, x)
            if is_shared(i) then split2(y) else [y] end
          end,
        copies
      )
    def key_part(i, p) =
      if is_shared(i) then [list.nth(p, 1)] else [] end
    end
    key_parts = list.flatten(list.mapi(key_part, filtered))
    keys =
      if
        list.length(key_parts) >= 2
      then
        key =
          ffmpeg.filter.amix(
            graph, inputs=list.length(key_parts), normalize=false, key_parts, []
          )
        let (ks, _) = ffmpeg.filter.asplit(graph, outputs=list.length(key_parts), key)
        ks
      else
        []
      end
    def band(i, p) =
      x = list.nth(p, 0)
      threshold = lin_of_dB(threshold_db(i))
      knee = lin_of_dB(knee_db(i))
      makeup = lin_of_dB(makeup_db(i))
      if
        is_shared(i)
      then
        k = list.nth(keys, list.length(list.filter(fun (j) -> j < i, shared)))
        ffmpeg.filter.sidechaincompress(
          graph, threshold=threshold, ratio=ratio(i), knee=knee, makeup=makeup,
          attack=attack_ms(i), release=release_ms(i), detection=detection, link=link, x, k
        )
      else
        ffmpeg.filter.acompressor(
          graph, threshold=threshold, ratio=ratio(i), knee=knee, makeup=makeup,
          attack=attack_ms(i), release=release_ms(i), detection=detection, link=link, x
        )
      end
    end
    mixed =
      ffmpeg.filter.amix(graph, inputs=n, normalize=false, list.mapi(band, filtered), [])
    source({audio=ffmpeg.filter.audio.output(graph, mixed)})
  end
  ffmpeg.raw.decode.audio(ffmpeg.filter.create(mkfilter))
end
