# AGC output-target step: agc.liq alone (one band, gate, 12 s intro hold), nothing after it, so
# the render is exactly what the AGC hands the multiband. Params: target (LUFS), idle (dB; the
# gain held through gated passages, session.liq's rule being target minus the -18 LUFS
# item-gain level), threshold (gate, LUFS), gain_min and gain_max (dB).
%include "../../../backend/playout/agc.liq"

def chain(s) =
  s =
    agc(
      target=param_float("target", -16.), idle=param_float("idle", 2.),
      threshold=param_float("threshold", -33.), gain_min=param_float("gain_min", -10.),
      gain_max=param_float("gain_max", 10.), crossover=0., s
    )
  gauge("agc_gain", s.gain)
  s
end
