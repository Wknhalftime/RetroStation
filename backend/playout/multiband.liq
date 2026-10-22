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
#   sidechaincompress keyed like band 4). Its delta release and max delta GR only act on its
#   own compressor, so they do not apply.
# - 3>4 coupling (`couple34`, preset 30%): band 4 takes the larger of its own gain reduction
#   and 30% of band 3's (the "OR" rule, p. 3-64). Band 4's sidechain carries four channels,
#   band 4 itself and a copy of band 3 that compand maps to the level giving 30% of band 3's
#   gain reduction; linked by maximum, the compressor follows whichever needs more. Over six
#   songs the coupling decides band 4's gain 17% of the time (median +0.8 dB, up to 5.6).
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
  ~couple34=0.3,
  s
) =
  # drive 13 with these references puts band 3 at about 5 dB of gain reduction.
  calibration = 15.
  def threshold_db(t, r) =
    r + t - drive + calibration
  end

  def threshold(t, r) =
    lin_of_dB(threshold_db(t, r))
  end

  t3_db = threshold_db(t3, ref3)
  t4_db = threshold_db(t4, ref4)
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
    let (band3, _) = ffmpeg.filter.asplit(graph, outputs=2, list.nth(bands, 2))
    let (band4, _) = ffmpeg.filter.asplit(graph, outputs=2, list.nth(bands, 3))
    # Band 3 at its threshold maps to band 4's threshold; above it, couple34 dB per dB.
    coupled =
      ffmpeg.filter.compand(
        graph, attacks="#{a3 / 1000.}", decays="#{release / 1000.}", volume=-90.,
        points="-90/-150|#{t3_db}/#{t4_db}|0/#{t4_db + couple34 * (0. - t3_db)}",
        list.nth(band3, 1)
      )
    key =
      ffmpeg.filter.join(
        graph, inputs=2, channel_layout="quad", [list.nth(band4, 1), coupled], []
      )
    let (keys, _) = ffmpeg.filter.asplit(graph, outputs=2, key)
    c1 = squeeze(t1, ref1, a1, list.nth(bands, 0))
    # The clip sits on band 1's output after makeup, so its threshold takes makeup off.
    c1 =
      ffmpeg.filter.asoftclip(
        graph, type=0, threshold=lin_of_dB(ceiling + bass_clip - makeup), c1
      )
    c2 = squeeze(t2, ref2, a2, list.nth(bands, 1))
    c3 = squeeze(t3, ref3, a3, list.nth(band3, 0))
    # Bands 4 and 5 both follow the four-channel key, so band 5 keeps band 4's full gain.
    def follow(x, k) =
      ffmpeg.filter.sidechaincompress(
        graph, threshold=threshold(t4, ref4), ratio=20., attack=a4, release=release, knee=1.,
        detection=0, link=1, x, k
      )
    end
    c4 = follow(list.nth(band4, 0), list.nth(keys, 0))
    c5 = follow(list.nth(bands, 4), list.nth(keys, 1))
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
