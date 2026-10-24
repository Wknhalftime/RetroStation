# Today's live chain, for baselines: session.liq on master 0add368 (Rock-Light AGC #181, five-band
# multiband #180, EQ removed by #183). Same calls as session.liq, without mksafe (a render must
# end when its file does). Track boundaries don't occur in a one-file render: the AGC's intro
# hold is armed once at the start, as it is at every song start live.
%include "../../../backend/playout/agc.liq"
%include "../../../backend/playout/multiband.liq"

def chain(s) =
  s = agc(target=-16., idle=2., threshold=-33., crossover=0., s)
  gauge("agc_gain", s.gain)
  multiband(s)
end
