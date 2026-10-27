# Run an FFmpeg graph builder over a source: raw float stereo at 44.1 kHz in, the builder's
# last node out. Shared by the step chains and the full chain, so every one runs the same stages.
def through_graph(s, build) =
  raw =
    ffmpeg.raw.encode.audio(
      %ffmpeg(%audio.raw(sample_format = "flt", sample_rate = 44100, channels = 2)), s
    )
  def mkfilter(graph) =
    x = ffmpeg.filter.audio.input(graph, source.tracks(raw).audio)
    source({audio=ffmpeg.filter.audio.output(graph, build(graph, x))})
  end
  ffmpeg.raw.decode.audio(ffmpeg.filter.create(mkfilter))
end
