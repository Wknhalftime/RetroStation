# Windowed, gated AGC, modelled on the Optimod's (8685 manual pp. 3-10, 3-44 to 3-48, 3-54)
# and Lance's Rock-Light preset. Liquidsoap's normalize rides gain all the time and holds it
# below its gate. This one:
# - window: while the gain is within a few dB of where it should be, it moves at
#   window_rate (about 0.5 dB/s), so already-controlled music is left alone; only a
#   bigger error gets the full attack or release rate. The window is twice as wide on
#   the release (gain up) side as on the attack side, and its edges are soft.
# - gate: below `threshold` the input is not program (a pause, a stop, a fade). The gain then
#   drifts toward `idle` at idle_rate, too slowly to hear, instead of riding up.
# - two detectors: BS.1770 loudness over `window_s` (3 s) steers the window, the gate and
#   the gain going up; over `attack_window_s` (0.4 s) it pulls the gain down as soon as a
#   loud entrance is `entrance` dB too loud. With only the 3 s detector a band coming in
#   after a quiet intro stayed about 3 LU hot for 5 s (Smells Like Teen Spirit). A 0.5 s
#   lookahead on top of this barely changed it (measured), so there is none.
# - bass band (crossover > 0; 0 = one band): first-order low band, the high band being the
#   rest, so the two sum flat when both have the same gain. The bass band's gain follows the
#   master gain plus an offset, within bass_couple dB, that steers the bass-to-full-band
#   balance toward bass_tilt (the Optimod's AGC bass threshold and coupling).
# - bass shelf (bass_shelf dB, with the bass band): a fixed boost on the low band, which is
#   a low shelf below the crossover (+2 dB: measured +1.9/+1.1/+0.5/+0.1 dB at 50/200/400/
#   1000 Hz). It lives here because this band already exists: a separate shelf stage that
#   mixed its own low band made the end-of-schedule shutdown reset the listener's
#   connection (3 runs in 8; 0 in 8 without it).
# - intro hold (intro_hold > 0): call .hold() when a song starts. For intro_hold seconds the
#   gain can still come down but only creeps up, at window_rate: songs arrive already at
#   -18 LUFS, so a quiet intro is the mix, not a level error, and lifting it at full rate
#   sent the band's entrance 5-8 dB hot (Teen Spirit, Stairway, In the Air Tonight).
# Rates are in dB/s.
def agc(
  ~target=-16.,
  ~idle=0.,
  ~threshold=-33.,
  ~gain_min=-10.,
  ~gain_max=10.,
  ~window=3.,
  ~window_rate=0.5,
  ~attack_rate=6.,
  ~release_rate=3.,
  ~idle_rate=0.3,
  ~window_s=3.,
  ~attack_window_s=0.4,
  ~entrance=3.,
  ~intro_hold=15.,
  ~crossover=200.,
  ~bass_couple=3.,
  ~bass_tilt=-3.6,
  ~bass_attack_rate=3.,
  ~bass_release_rate=6.4,
  ~bass_shelf=0.,
  s
) =
  fast = lufs(window={attack_window_s}, s)
  s = lufs(window={window_s}, fast)
  g = ref(idle)
  bass_offset = ref(0.)
  dt = frame.duration()
  # Frames left in the intro hold: gain may come down but only creeps up (window_rate).
  held = ref(int_of_float(intro_hold / dt))

  def hold() =
    held := int_of_float(intro_hold / dt)
  end

  # Full rate past 2x the window edge, window_rate inside the edge, linear between.
  def rate(miss, edge, full) =
    a = abs(miss)
    blend = max(0., min(1., (a - edge) / edge))
    window_rate + blend * (full - window_rate)
  end

  # How far a gain moves this frame toward goal at per_s dB/s.
  def move(current, goal, per_s) =
    m = per_s * dt
    current + max(0. - m, min(m, goal - current))
  end

  def step_gain(goal, per_s) =
    g := move(g(), goal, per_s)
  end

  def step_bass(goal, per_s) =
    bass_offset := move(bass_offset(), goal, per_s)
  end

  def clamp(x) =
    max(gain_min, min(gain_max, x))
  end

  # Bass band: levels in dBFS RMS over window_s, measured before any gain. The full-band
  # meter sits inline ahead of the split so that both bands pull it.
  bass_split = crossover > 0.
  full_rms = rms.smooth(duration=window_s, s)
  split_at = if bass_split then crossover else 200. end
  low = filter.iir.butterworth.low(frequency=split_at, order=1, full_rms)
  low_rms = rms.smooth(duration=window_s, low)

  def update_bass(gated) =
    tilt = dB_of_lin(low_rms.rms()) - dB_of_lin(full_rms.rms())
    # tilt is nan or infinite until both meters have seen sound: hold until then.
    if
      gated
    then
      step_bass(0., idle_rate)
    elsif
      not (tilt > -60. and tilt < 60.)
    then
      ()
    else
      goal = max(0. - bass_couple, min(bass_couple, bass_tilt - tilt))
      if
        goal < bass_offset()
      then
        step_bass(goal, bass_attack_rate)
      else
        step_bass(goal, bass_release_rate)
      end
    end
  end

  def update() =
    level = s.lufs()
    quick = fast.lufs()
    # Written this way round so that nan (no full window measured yet) counts as gated.
    gated = not (level >= threshold)
    if
      gated
    then
      step_gain(idle, idle_rate)
    else
      goal = clamp(target - level)
      quick_goal = if quick >= threshold then clamp(target - quick) else goal end
      if
        quick_goal - g() < 0. - entrance
      then
        step_gain(quick_goal, rate(quick_goal - g(), entrance, attack_rate))
      elsif
        goal > g()
      then
        up = if held() > 0 then window_rate else release_rate end
        step_gain(goal, rate(goal - g(), window, up))
      else
        step_gain(goal, rate(goal - g(), window / 2., attack_rate))
      end
    end
    if bass_split then update_bass(gated) end
    if held() > 0 then held := held() - 1 end
  end

  s.on_frame(synchronous=true, update)
  # override=null: amplify's default override would apply a track's liq_amplify (the item
  # gain, already applied on its deck) in place of the AGC gain.
  result =
    if
      bass_split
    then
      # The high band is the full band minus the low one (sums flat by construction, one
      # filter instead of two): out = master gain x full + (bass gain - master gain) x low.
      # Mixed as tracks, not with add(): add() drops track marks (measured), and the song
      # boundaries must reach the outputs. Metadata and marks come from the full band.
      def bass_extra() =
        lin_of_dB(g() + bass_offset() + bass_shelf) - lin_of_dB(g())
      end
      full = source.tracks(amplify(override=null, {lin_of_dB(g())}, full_rms))
      lo = source.tracks(amplify(override=null, bass_extra, low_rms))
      source(
        {
          audio=track.audio.add(normalize=false, [full.audio, lo.audio]),
          metadata=full.metadata,
          track_marks=full.track_marks
        }
      )
    else
      amplify(override=null, {lin_of_dB(g())}, s)
    end
  result.{gain={g()}, bass_offset={bass_offset()}, hold=hold}
end
