# AGC comparison (listening test 1), side B: Liquidsoap's normalize with LUFS detection, at the
# same target (-14 LUFS, R8), range (+-10 dB) and gate (-33) as the agc.liq side; its own
# behaviour otherwise (window 0.5 s, up 10 s, down 0.1 s, gain frozen below the gate). Then the
# shared safety limiter. Its gain is linear, so the gauge converts it to dB like agc.liq's.
%include "safety_limiter.liq"

def chain(s) =
  s =
    normalize(
      target=param_float("target", -14.), lufs=true, threshold=param_float("threshold", -33.),
      gain_min=-10., gain_max=10., s
    )
  gauge("agc_gain", {dB_of_lin(s.gain())})
  safety_limiter(s)
end
