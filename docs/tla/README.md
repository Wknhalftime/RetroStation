# TLA+ models

Models of the system's design, checked with TLC. A model checks the design, not the Python
code, so each spec names the code and the rulings it models, and a change to those rulings
updates the spec in the same PR (the same rule as `ARCHITECTURE.md`).

| Spec | Models | Rulings |
|---|---|---|
| [MatchingStatus.tla](MatchingStatus.tla) | song `match_status`: curator (API) vs. Huey worker(s) | AUD-R015, R017, R018/R021, R029 |
| [MBPacing.tla](MBPacing.tla) | MusicBrainz request pacing: `mb_clock.wait_turn` + `mb_client._fetch`, API and worker | AUD-R102 |

## MatchingStatus models

| Config | Workers | Artist-link cascade | Guard | TLC result (2026-11-22) |
|---|---|---|---|---|
| `MC_w1_nolink.cfg` | 1 | off | on | all hold (12,041 distinct states) |
| `MC_w2_nolink.cfg` | 2 | off | on | `NoStaleWrite` fails: a second worker's re-check rewinds a song the first worker read (why AUD-R017 exists) |
| `MC_w1_link.cfg` | 1 | on | on | `NoStaleWrite` fails: read, curator rejects, curator links the artist (song back to pending), worker writes from the old read |
| `MC_unguarded.cfg` | 1 | off | off | negative control: `CuratorDecisionStands` fails (the worker overwrites a rejection it read as pending) |

`CuratorDecisionStands` alone (`NoStaleWrite` left out of the config, so TLC reaches it) holds
for two workers (146,597 states), one worker with the link cascade (202,649) and two workers
with the link cascade (1,822,724). The guarded write keeps every curator decision; what fails
is only the freshness of the worker's read.

## MBPacing models

MusicBrainz counts a request when it arrives. `wait_turn` spaces the clock-file stamps a
cadence (1.1 s) apart, but `_fetch` sends the request after the lock is released, so the time
from stamp to arrival (thread scheduling, DNS + TCP + TLS on a new connection, network) is
outside the lock. One tick is 0.1 s; "out" is stamp to arrival, "back" is arrival to answer.
Every config: `Window` 10 (one second), `MaxBack` 3, `MaxTime` 80.

| Config | Design | Procs | Cadence | Out | Clock jump | TLC result (2026-11-23) |
|---|---|---|---|---|---|---|
| `MC_pacing_today_steady.cfg` | today | 2 | 1.1 s | 0.2 s always | none | all hold (4,697,987 distinct states) |
| `MC_pacing_today_jitter.cfg` | today | 2 | 1.1 s | 0.1-0.4 s | none | `ArrivalsSpaced` fails |
| `MC_pacing_today_one_proc.cfg` | today | 1 | 1.1 s | 0.1-0.4 s | none | `ArrivalsSpaced` fails with ONE process: stamp at 0, a cold connection arrives at 0.3 s; stamp at 1.1 s, the warm connection arrives at 1.2 s; 0.9 s apart |
| `MC_pacing_after_jitter.cfg` | after answer | 2 | 1.1 s | 0.1-0.4 s | none | all hold (12,116,509) |
| `MC_pacing_after_cadence10.cfg` | after answer | 2 | 1.0 s | 0.1-0.4 s | none | all hold (14,078,993) |
| `MC_pacing_after_step.cfg` | after answer | 2 | 1.1 s | 0.1-0.4 s | +0.5 s once | `ArrivalsSpaced` fails |

Today's design guarantees arrivals at least `Cadence - (MaxOut - MinOut)` apart: the 0.1 s
margin over one second covers 0.1 s of variation in the time to arrive, and no more.

The alternative ("after answer"): hold the file lock across the request and touch the clock
in a `finally` when the answer, the error or the timeout comes back. A request's arrival is
before its answer, so the next request leaves at least a cadence after the last one arrived,
whatever the network does. That also makes the 0.1 s margin unnecessary (cadence 1.0 s holds).
The cost: the lock is held for the whole request (up to the 30 s timeout per attempt), so the
API's lookups queue behind the worker's request in flight, and each request takes its answer
time plus the cadence.

What no design here fixes: a forward jump of the wall clock (`time.time()` and file mtimes)
shortens one wait by the jump. The alternative tolerates a jump up to
`Cadence - Window + MinOut + 1` ticks (0.3 s here); 0.5 s fails.

A vacuity check (invariant `minGap = NoGap`, i.e. "two requests never both arrive") fails on
`MC_pacing_after_jitter.cfg`, so the passing configs do reach arrivals.

Not modelled: other MusicBrainz clients on this IP (Picard, a browser) and MusicBrainz's global
limiter, which refuses requests whatever we do.

## Running

Needs Java 11+ and `tla2tools.jar` from the TLA+ releases page
(<https://github.com/tlaplus/tlaplus/releases>). From this folder:

```bash
java -cp tla2tools.jar tla2sany.SANY MatchingStatus.tla
```

Each config names its spec's constants, so pair `MC_pacing_*.cfg` with `MBPacing.tla` and the
others with `MatchingStatus.tla`.

```bash
java -XX:+UseParallelGC -cp tla2tools.jar tlc2.TLC -workers auto -config MC_w1_nolink.cfg MatchingStatus.tla
```

`tla2tools.jar` and TLC's `states/` folder are not checked in.
