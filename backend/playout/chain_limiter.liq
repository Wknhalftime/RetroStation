# The final stage (limiter step, P4 and P5; Lance, 2026-10-26): makeup to the loudness target,
# then FFmpeg's look-ahead limiter. No auto level, latency compensated, no clipper: it catches
# overs only. Called inside an FFmpeg graph; returns the limited node. Params:
# - out_makeup_db: gain ahead of the limiter (dB, default 0); calibrated once, then held
# - limiter: 1 limits (default); 0 leaves the made-up signal as it is (the reference the
#   limiter's gain reduction is measured against)
# - limit_db: the ceiling (dBFS, default -1)
# - lim_attack_ms: alimiter's attack, which is also its only look-ahead (default 5)
# - lim_release_ms: (default 50)
# - oversample: 1 (default), or a factor: the limiter then runs at 44.1 kHz times it, between
#   two aresample stages, so it sees inter-sample peaks (P5 option a)
def limiter_stage(graph, x) =
  x = ffmpeg.filter.volume(graph, volume="#{param_float('out_makeup_db', 0.)}dB", x)
  factor = param_int("oversample", 1)
  def limit(y) =
    ffmpeg.filter.alimiter(
      graph, limit=lin_of_dB(param_float("limit_db", -1.)),
      attack=param_float("lim_attack_ms", 5.), release=param_float("lim_release_ms", 50.),
      level=false, latency=true, y
    )
  end
  if
    param_int("limiter", 1) == 0
  then
    x
  elsif
    factor > 1
  then
    up = ffmpeg.filter.aresample(graph, sample_rate=44100 * factor, x)
    ffmpeg.filter.aresample(graph, sample_rate=44100, limit(up))
  else
    limit(x)
  end
end
