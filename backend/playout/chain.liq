# The R17 chain (R10-R17; on air in session.liq since 2026-10-27, Lance): the DC blocker
# (R16), agc.liq (R10, R11), the multiband (R12-R14), then makeup and the limiter (R17). Params,
# read through param_float and its kin (chain_params.liq on air, the preset in the harness):
# the high-pass's (hp_hz, hp_order; chain_high_pass.liq), the AGC's (agc.liq's names), the
# multiband's (chain_multiband.liq) and the limiter's (chain_limiter.liq). The result carries
# the AGC's hold: a session calls it as each song starts (agc.liq's intro hold). The harness
# measures this file through scripts/processor_harness/chains/full_chain.liq.
%include "agc.liq"
%include "chain_graph.liq"
%include "chain_high_pass.liq"
%include "chain_multiband.liq"
%include "chain_limiter.liq"

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
  limited = through_graph(s, fun (graph, x) -> limiter_stage(graph, multiband_bands(graph, x)))
  limited.{hold = s.hold}
end
