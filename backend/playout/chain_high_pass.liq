# The high-pass stage (see high_pass.liq for its params): Butterworth as FFmpeg highpass
# biquads in double precision. Called inside an FFmpeg graph; returns the filtered node.
def high_pass_sections(graph, x) =
  hz = param_float("hp_hz", 30.)
  order = param_int("hp_order", 2)
  odd = order mod 2 == 1
  pi = 3.141592653589793
  def q_of(k) =
    if
      odd
    then
      1. / (2. * cos(pi * float(k + 1) / float(order)))
    else
      1. / (2. * cos(pi * float(2 * k + 1) / float(2 * order)))
    end
  end
  qs = list.map(q_of, list.init(order / 2, fun (k) -> k))
  def section(x, q) =
    ffmpeg.filter.highpass(graph, frequency=hz, width=q, poles=2, precision=3, x)
  end
  y = list.fold(section, x, qs)
  y = if odd then ffmpeg.filter.highpass(graph, frequency=hz, poles=1, precision=3, y) else y end
  y
end
