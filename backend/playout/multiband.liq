# Five-band compressor and final peak control, modelled on the Optimod 8685's Five-Band
# structure (manual pp. 3-24, 3-49 to 3-51, 3-60 to 3-66) with Lance's Rock-Light preset,
# run as ONE ffmpeg filter graph (compiled C). One graph matters twice over: a first version
# built from Liquidsoap operators cost more than a whole core per listener (its gains were
# getters, evaluated on every sample), and each extra filter graph widens Liquidsoap 2.4.5's
# end-of-schedule shutdown race (PR #179).
# - acrossover splits at 100, 520, 1800 and 6000 Hz; its bands sum flat (within 0.04 dB).
# - bands 1-4: acompressor each, peak detection, stereo linked (Max Delta GR 0 dB), 20:1 with
#   the hardest knee (ffmpeg's nearest to infinity:1, knee 0), the preset's attacks, one
#   release for all ("Slow2", here 1 s).
# - thresholds as in the preset, but measured from each band's own reference level, as the
#   Optimod's are (p. 3-61: "small changes around the factory settings"). The references are
#   each band's median level into the multiband over six test songs, so equal thresholds work
#   the bands equally; `drive` 13 then gives band 3 about 5 dB of gain reduction (p. 3-21).
#   Without references the bass bands, which carry most of the energy, compressed hardest
#   and tilted the sound 1.5-3 dB brighter.
# - band 5's compressor is Off in the preset: band 5 follows band 4 at 100% (4>5 coupling,
#   sidechaincompress keyed on band 4). Its delta release and max delta GR only act on its
#   own compressor, so they do not apply. The 3>4 coupling (30%) is left out: band 4's own
#   gain reduction (median 4.4 dB) is nearly always above 30% of band 3's (1.5 dB), and the
#   coupling takes the larger of the two (p. 3-64).
# - bass clipper on band 1 at `bass_clip` dB under the limiter ceiling (preset -4), a tanh
#   soft clip (ffmpeg's nearest to clip shape 7.6).
# - peak control: the former 1 ms, 20:1 compressor at -1.5 dBFS, then a 5 ms lookahead
#   limiter at `ceiling` (-2 dBFS: the 128k MP3 encode adds up to 1.5 dB on dense material).
# Not modelled: the multiband gate (ffmpeg compressors cannot freeze), per-band limiters.
def multiband(
  ~drive=13.,
  ~t1=-10.75,
  ~t2=-6.5,
  ~t3=-7.,
  ~t4=-6.,
  ~ref1=-15.6,
  ~ref2=-12.1,
  ~ref3=-16.,
  ~ref4=-15.6,
  ~a1=34.,
  ~a2=33.,
  ~a3=33.,
  ~a4=25.,
  ~release=1000.,
  ~makeup=0.,
  ~bass_clip=-4.,
  ~ceiling=-2.,
  s
) =
  # drive 13 with these references puts band 3 at about 5 dB of gain reduction.
  calibration = 15.
  def threshold(t, r) =
    lin_of_dB(r + t - drive + calibration)
  end
  raw =
    ffmpeg.raw.encode.audio(
      %ffmpeg(%audio.raw(sample_format = "flt", sample_rate = 44100, channels = 2)), s
    )
  def mkfilter(graph) =
    dry = ffmpeg.filter.audio.input(graph, source.tracks(raw).audio)
    let (bands, _) = ffmpeg.filter.acrossover(graph, split="100 520 1800 6000", dry)
    def squeeze(t, r, a, x) =
      ffmpeg.filter.acompressor(
        graph, threshold=threshold(t, r), ratio=20., attack=a, release=release, knee=1.,
        detection=0, link=1, x
      )
    end
    let (band4, _) = ffmpeg.filter.asplit(graph, outputs=2, list.nth(bands, 3))
    c1 = squeeze(t1, ref1, a1, list.nth(bands, 0))
    # The clip sits on band 1's output after makeup, so its threshold takes makeup off.
    c1 =
      ffmpeg.filter.asoftclip(
        graph, type=0, threshold=lin_of_dB(ceiling + bass_clip - makeup), c1
      )
    c2 = squeeze(t2, ref2, a2, list.nth(bands, 1))
    c3 = squeeze(t3, ref3, a3, list.nth(bands, 2))
    c4 = squeeze(t4, ref4, a4, list.nth(band4, 0))
    c5 =
      ffmpeg.filter.sidechaincompress(
        graph, threshold=threshold(t4, ref4), ratio=20., attack=a4, release=release, knee=1.,
        detection=0, link=1, list.nth(bands, 4), list.nth(band4, 1)
      )
    mixed = ffmpeg.filter.amix(graph, inputs=5, normalize=false, [c1, c2, c3, c4, c5], [])
    mixed = ffmpeg.filter.volume(graph, volume="#{makeup}dB", mixed)
    mixed =
      ffmpeg.filter.acompressor(
        graph, threshold=lin_of_dB(-1.5), ratio=20., attack=1., release=80., knee=1.,
        detection=0, link=1, mixed
      )
    mixed =
      ffmpeg.filter.alimiter(
        graph, limit=lin_of_dB(ceiling), attack=5., release=50., level=false, mixed
      )
    source({audio=ffmpeg.filter.audio.output(graph, mixed)})
  end
  ffmpeg.raw.decode.audio(ffmpeg.filter.create(mkfilter))
end
