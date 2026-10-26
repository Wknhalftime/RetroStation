# Dial-in step 1 (crossover): FFmpeg's IIR acrossover, every band at unity gain, summed. The
# band sum is flat in magnitude but not in phase (an allpass), so on programme whose peaks a
# limiter has lined up it can raise the true peak while loudness stays put.
# Params:
# - split: crossover frequencies, space-separated (default: today's 100 520 1800 6000)
# - order: acrossover's order index: 0 = 2nd, 1 = 4th (default, today's), 3 = 8th
#   (12 / 24 / 48 dB per octave)
# - solo: 0 sums every band; n renders band n alone, so the impulse shows its shape
# - gains: static gain per band in dB, space-separated (default all 0), standing in for the
#   unequal gains compression applies (pre-ringing tests)
def chain(s) =
  split = param_string("split", "100 520 1800 6000")
  order = param_int("order", 1)
  solo = param_int("solo", 0)
  n = list.length(string.split(separator=" ", split)) + 1
  gains = string.split(separator=" ", param_string("gains", ""))
  raw =
    ffmpeg.raw.encode.audio(
      %ffmpeg(%audio.raw(sample_format = "flt", sample_rate = 44100, channels = 2)), s
    )
  def mkfilter(graph) =
    dry = ffmpeg.filter.audio.input(graph, source.tracks(raw).audio)
    let (bands, _) = ffmpeg.filter.acrossover(graph, split=split, order=order, dry)
    def keep(i, band) =
      if
        solo != 0 and solo != i + 1
      then
        ffmpeg.filter.volume(graph, volume="0", band)
      elsif
        list.length(gains) == n
      then
        ffmpeg.filter.volume(graph, volume="#{list.nth(gains, i)}dB", band)
      else
        band
      end
    end
    mixed =
      ffmpeg.filter.amix(graph, inputs=n, normalize=false, list.mapi(keep, bands), [])
    source({audio=ffmpeg.filter.audio.output(graph, mixed)})
  end
  ffmpeg.raw.decode.audio(ffmpeg.filter.create(mkfilter))
end
