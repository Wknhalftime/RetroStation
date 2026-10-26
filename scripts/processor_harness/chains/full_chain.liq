# The full chain so far (limiter step; Lance, 2026-10-26): the DC blocker (R16), agc.liq
# (R10, R11), the multiband (R12-R14), then makeup and the limiter (P4, P5). Params: the
# high-pass's (hp_hz, hp_order; see high_pass.liq), the AGC's (agc_only.liq's names), the
# multiband's (multiband_linear.liq's names) and the limiter's (lib/limiter.liq).
%include "../../../backend/playout/agc.liq"
%include "lib/graph.liq"
%include "lib/high_pass.liq"
%include "lib/multiband.liq"
%include "lib/limiter.liq"

def chain(s) =
  s = through_graph(s, high_pass_sections)
  s =
    agc(
      target=param_float("target", -18.), idle=param_float("idle", 0.),
      threshold=param_float("threshold", -33.), gain_min=param_float("gain_min", -10.),
      gain_max=param_float("gain_max", 10.), window=param_float("window", 6.),
      window_rate=param_float("window_rate", 0.1), attack_rate=param_float("attack_rate", 2.),
      release_rate=param_float("release_rate", 0.5), idle_rate=param_float("idle_rate", 0.3),
      window_s=param_float("window_s", 10.), attack_window_s=param_float("attack_window_s", 0.4),
      entrance=param_float("entrance", 3.), intro_hold=param_float("intro_hold", 12.),
      crossover=0., s
    )
  through_graph(s, fun (graph, x) -> limiter_stage(graph, multiband_bands(graph, x)))
end
