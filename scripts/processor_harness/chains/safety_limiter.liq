# The shared tail of the AGC comparison chains: FFmpeg's 5 ms lookahead limiter at -1 dBFS, so an
# AGC's gain never drives the MP3 encoder past full scale. Identical on both sides of the
# comparison, so it favours neither.
def safety_limiter(s) =
  raw =
    ffmpeg.raw.encode.audio(
      %ffmpeg(%audio.raw(sample_format = "flt", sample_rate = 44100, channels = 2)), s
    )
  def mkfilter(graph) =
    a = ffmpeg.filter.audio.input(graph, source.tracks(raw).audio)
    a =
      ffmpeg.filter.alimiter(
        graph, limit=lin_of_dB(-1.), attack=5., release=50., level=false, a
      )
    source({audio=ffmpeg.filter.audio.output(graph, a)})
  end
  ffmpeg.raw.decode.audio(ffmpeg.filter.create(mkfilter))
end
