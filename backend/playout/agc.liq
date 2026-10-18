# Windowed, gated AGC, modelled on the Optimod's (8685 manual pp. 3-10, 3-44 to 3-47, 3-54).
# Liquidsoap's normalize rides gain all the time and holds it below its gate. This one:
# - window: while the gain is within a few dB of where it should be, it moves at
#   window_rate (about 0.5 dB/s), so already-controlled music is left alone; only a
#   bigger error gets the full attack or release rate. The window is twice as wide on
#   the release (gain up) side as on the attack side, and its edges are soft.
# - gate: below `threshold` the input is not program (a pause, a stop, a fade). The gain then
#   drifts toward `idle` at idle_rate, too slowly to hear, instead of riding up.
# Rates are in dB/s. Loudness is BS.1770 over `window_s` of input: 3 s (short-term) measured
# half the 1 s gain swings of a 0.4 s window, which chased single beats.
def agc(
  ~target=-16.,
  ~idle=0.,
  ~threshold=-33.,
  ~gain_min=-10.,
  ~gain_max=10.,
  ~window=3.,
  ~window_rate=0.5,
  ~attack_rate=6.,
  ~release_rate=2.,
  ~idle_rate=0.3,
  ~window_s=3.,
  s
) =
  s = lufs(window={window_s}, s)
  g = ref(idle)
  dt = frame.duration()

  # Full rate past 2x the window edge, window_rate inside the edge, linear between.
  def rate(miss, edge, full) =
    a = abs(miss)
    blend = max(0., min(1., (a - edge) / edge))
    window_rate + blend * (full - window_rate)
  end

  def step_toward(goal, per_s) =
    e = goal - g()
    move = per_s * dt
    g := g() + max(0. - move, min(move, e))
  end

  def update() =
    level = s.lufs()
    # Written this way round so that nan (no full window measured yet) counts as gated.
    if
      not (level >= threshold)
    then
      step_toward(idle, idle_rate)
    else
      goal = max(gain_min, min(gain_max, target - level))
      e = goal - g()
      if
        e > 0.
      then
        step_toward(goal, rate(e, window, release_rate))
      else
        step_toward(goal, rate(e, window / 2., attack_rate))
      end
    end
  end

  s.on_frame(synchronous=true, update)
  # override=null: amplify's default override would apply a track's liq_amplify (the item
  # gain, already applied on its deck) in place of the AGC gain.
  amplify(override=null, {lin_of_dB(g())}, s).{gain={g()}}
end
