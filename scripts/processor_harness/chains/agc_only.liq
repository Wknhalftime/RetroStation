# AGC dial-in steps: agc.liq alone (one band, gate, intro hold), nothing after it, so the render
# is exactly what the AGC hands the multiband. Params default to agc.liq's own values:
# target (LUFS), idle (dB; the gain held through gated passages, session.liq's rule being target
# minus the -18 LUFS item-gain level), threshold (gate, LUFS), gain_min / gain_max (dB), and the
# timing: window (dB) and window_rate, attack_rate, release_rate, idle_rate (dB/s), window_s and
# attack_window_s (detector windows, s), entrance (dB), intro_hold (s).
%include "../../../backend/playout/agc.liq"

def chain(s) =
  s =
    agc(
      target=param_float("target", -16.), idle=param_float("idle", 2.),
      threshold=param_float("threshold", -33.), gain_min=param_float("gain_min", -10.),
      gain_max=param_float("gain_max", 10.), window=param_float("window", 3.),
      window_rate=param_float("window_rate", 0.5), attack_rate=param_float("attack_rate", 6.),
      release_rate=param_float("release_rate", 3.), idle_rate=param_float("idle_rate", 0.3),
      window_s=param_float("window_s", 3.), attack_window_s=param_float("attack_window_s", 0.4),
      entrance=param_float("entrance", 3.), intro_hold=param_float("intro_hold", 12.),
      crossover=0., s
    )
  gauge("agc_gain", s.gain)
  s
end
