# The chain's first stage (target chain table: high-pass, math item 7; Lance, 2026-10-25): a
# Butterworth high-pass to take out rumble and DC offset ahead of the AGC and the multiband,
# built as a cascade of FFmpeg highpass biquads (double precision) with the Butterworth Qs.
# Liquidsoap 2.4.5's own filter.iir.butterworth.high diverges at orders 3 and 4 near 30 Hz
# (inf / NaN on an impulse, 2026-10-25). It is an IIR stage, so it turns the phase at the low
# end; the high-pass step checks it does not raise true peak on dense masters. No high-pass is
# bypass.liq.
# Params:
# - hp_hz: corner frequency (Hz, default 30)
# - hp_order: Butterworth order (default 2). Even orders are order / 2 biquads with
#   Q = 1 / (2 cos(pi (2k - 1) / (2 order))); odd orders are (order - 1) / 2 biquads with
#   Q = 1 / (2 cos(k pi / order)) and one first-order section (order 1: a DC blocker)
%include "lib/graph.liq"
%include "lib/high_pass.liq"

def chain(s) =
  through_graph(s, high_pass_sections)
end
