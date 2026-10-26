# One FFmpeg acompressor, for checking the harness's math item 3a fits against the real filter:
# declare the same values in the preset's [model] table and the fits should give them back.
# Params (dB and ms, as in [model]): threshold_db, ratio, knee_db (knee width), makeup_db,
# attack_ms, release_ms, detection (0 peak, 1 rms), link (0 average, 1 maximum).
def chain(s) =
  threshold = lin_of_dB(param_float("threshold_db", -18.))
  ratio = param_float("ratio", 2.)
  knee = lin_of_dB(param_float("knee_db", 9.03))
  makeup = lin_of_dB(param_float("makeup_db", 0.))
  attack = param_float("attack_ms", 20.)
  release = param_float("release_ms", 250.)
  detection = param_int("detection", 1)
  link = param_int("link", 0)
  raw =
    ffmpeg.raw.encode.audio(
      %ffmpeg(%audio.raw(sample_format = "flt", sample_rate = 44100, channels = 2)), s
    )
  def mkfilter(graph) =
    a = ffmpeg.filter.audio.input(graph, source.tracks(raw).audio)
    a =
      ffmpeg.filter.acompressor(
        graph, threshold=threshold, ratio=ratio, knee=knee, makeup=makeup, attack=attack,
        release=release, detection=detection, link=link, a
      )
    source({audio=ffmpeg.filter.audio.output(graph, a)})
  end
  ffmpeg.raw.decode.audio(ffmpeg.filter.create(mkfilter))
end
