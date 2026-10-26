# Dial-in band step (band edges, static curves, time constants; Lance, 2026-10-25): the R6
# linear-phase crossover (crossover_linear.liq's bands: complementary firequalizer band-passes,
# one window, one delay) with one FFmpeg acompressor per band (sidechaincompress for bands that
# share a detector), summed. No makeup stage, clipper or limiter after it: the multiband
# shapes balance; peak control is the limiter's job (P4).
# Params:
# - split, delay, wfunc: as crossover_linear.liq (default edges 100 520 1800 6000, 20 ms)
# - threshold_db, ratio, knee_db (knee width), makeup_db, attack_ms, release_ms: one value per
#   band, space-separated, in dB and ms (the preset's band models read the same lists). A list
#   without one value per band falls back to that value's default for every band.
# - detection (0 peak, 1 rms) and link (0 average, 1 maximum): every band
# - shared: bands (1-based, ascending, two or more) that compress from one detector fed by
#   their sum, each through sidechaincompress keyed by it (default none: every band its own)
%include "lib/graph.liq"
%include "lib/multiband.liq"

def chain(s) =
  through_graph(s, multiband_bands)
end
