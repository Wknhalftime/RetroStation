# AGC comparison (listening test 1), side A: agc.liq as session.liq calls it (one band, gate -33,
# 12 s intro hold), but at R8's -14 LUFS target. idle +4 dB keeps session.liq's rule, target
# minus the -18 LUFS item-gain level. Then the shared safety limiter.
%include "../../../backend/playout/agc.liq"
%include "safety_limiter.liq"

def chain(s) =
  s =
    agc(
      target=param_float("target", -14.), idle=param_float("idle", 4.),
      threshold=param_float("threshold", -33.), crossover=0., s
    )
  gauge("agc_gain", s.gain)
  safety_limiter(s)
end
