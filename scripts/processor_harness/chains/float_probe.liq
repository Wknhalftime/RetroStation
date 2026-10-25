# Harness check: does the multiband graph run in float end to end, so a level over full scale is
# carried rather than clipped (Lance, 2026-10-24)? The planned structure between the AGC and the
# limiter: the 20 ms linear-phase crossover (R6), an acompressor per band at +6 dB makeup (ratio
# 1, so nothing compresses), the band sum, and a +6 dB volume. ashowinfo after every stage logs
# each frame's sample format; FFmpeg's verbose log also names any converter it inserts between
# filters. No limiter at the end, so an over-full-scale input shows up whole in the output if
# nothing on the way clipped it (tests/scripts/processor_harness/test_float_path.py).
settings.ffmpeg.log.verbosity := "verbose"

def chain(s) =
  edges = [100., 520., 1800., 6000.]
  n = list.length(edges) + 1
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
    dry = ffmpeg.filter.ashowinfo(graph, dry)
    let (copies, _) = ffmpeg.filter.asplit(graph, outputs=n, dry)
    def band(i, x) =
      b = ffmpeg.filter.firequalizer(graph, gain=band_gain(i), delay=0.02, x)
      b = ffmpeg.filter.ashowinfo(graph, b)
      b = ffmpeg.filter.acompressor(graph, threshold=1., ratio=1., makeup=2., b)
      ffmpeg.filter.ashowinfo(graph, b)
    end
    mixed =
      ffmpeg.filter.amix(graph, inputs=n, normalize=false, list.mapi(band, copies), [])
    mixed = ffmpeg.filter.ashowinfo(graph, mixed)
    mixed = ffmpeg.filter.volume(graph, volume="6dB", mixed)
    mixed = ffmpeg.filter.ashowinfo(graph, mixed)
    source({audio=ffmpeg.filter.audio.output(graph, mixed)})
  end
  ffmpeg.raw.decode.audio(ffmpeg.filter.create(mkfilter))
end
