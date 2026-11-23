# TLA+ models

Models of the system's design, checked with TLC. A model checks the design, not the Python
code, so each spec names the code and the rulings it models, and a change to those rulings
updates the spec in the same PR (the same rule as `ARCHITECTURE.md`).

| Spec | Models | Rulings |
|---|---|---|
| [MatchingStatus.tla](MatchingStatus.tla) | song `match_status`: curator (API) vs. Huey worker(s) | AUD-R015, R017, R018/R021, R029 |
| [MBPacing.tla](MBPacing.tla) | MusicBrainz request pacing: `mb_clock.wait_turn` + `mb_client._fetch`, API and worker | AUD-R102, R104 |

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

MusicBrainz counts a request when it arrives, and limits each IP to "(on average) 1 request per
second" ([Rate Limiting](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting)) over a
window it does not publish. `wait_turn` spaces the clock-file stamps a cadence (1.1 s) apart, but
`_fetch` sends the request after the lock is released, so the time from stamp to arrival (thread
scheduling, DNS + TCP + TLS on a new connection, the network) is outside the lock.

One tick is 0.1 s. "Out" is stamp to arrival. `WithinRate` with `RateSeconds = K` says no
K-second window holds more than K of our arrivals; K = 1 is a strict one-second gap. Every config:
`Window` 10, `MaxBack` 3, `MaxTime` 80; two processes making 2 requests each unless noted.

| Config | Design | Cadence | Out | K | Clock jump | TLC result (2026-11-23) |
|---|---|---|---|---|---|---|
| `MC_pacing_today_steady.cfg` | today | 1.1 s | 0.2 s always | 1 | none | holds (20,398,313 distinct states) |
| `MC_pacing_today_small_jitter.cfg` | today | 1.1 s | 0.1-0.2 s | 1 | none | holds (47,963,957) |
| `MC_pacing_today_jitter.cfg` | today | 1.1 s | 0.1-0.4 s | 1 | none | fails |
| `MC_pacing_today_one_proc.cfg` | today, ONE process, 3 requests | 1.1 s | 0.1-0.4 s | 1 | none | fails: stamp at 0, a cold connection arrives at 0.3 s; stamp at 1.1 s, the warm one arrives at 1.2 s; 0.9 s apart |
| `MC_pacing_today_jitter_rate2.cfg` | today | 1.1 s | 0.1-0.4 s | 2 | none | fails: 3 arrivals in 1.9 s |
| `MC_pacing_today_jitter_rate3.cfg` | today | 1.1 s | 0.1-0.4 s | 3 | none | holds (128,237,139) |
| `MC_pacing_after_jitter.cfg` | after answer | 1.1 s | 0.1-0.4 s | 1 | none | holds (39,476,601) |
| `MC_pacing_after_cadence10.cfg` | after answer | 1.0 s | 0.1-0.4 s | 1 | none | holds (50,503,461) |
| `MC_pacing_after_step3.cfg` | after answer | 1.1 s | 0.1-0.4 s | 1 | +0.3 s once | holds (154,517,338) |
| `MC_pacing_after_step4.cfg` | after answer | 1.1 s | 0.1-0.4 s | 1 | +0.4 s once | fails |

What the runs show:

- **Today's design keeps the average, not the gap.** K stamps span at least K × 1.1 s, and the
  variation in time to arrive does not add up across requests, so K + 1 arrivals span at least
  K × 1.1 s minus the variation. So the 0.1 s margin gives a strict one-second gap only while the
  variation stays within 0.1 s; with 0.3 s of variation, one a second holds only over windows of
  3 s or more. Whether this ever
  costs us a 503 depends on how MusicBrainz measures its average, which it does not publish. A
  longer stall (a 2 s pause between stamp and send) would need a 20 s window.
- **The alternative keeps the gap whatever the network does.** Hold the file lock across the
  request and touch the clock in a `finally` when the answer, the error or the timeout comes back.
  A request arrives before its answer, so the next request leaves at least a cadence after the
  last one arrived. The 0.1 s margin is then unnecessary (cadence 1.0 s holds). The cost: the lock
  is held for the whole request (up to the 30 s timeout per attempt), so the API's lookups queue
  behind the worker's request in flight, and each request takes its answer time plus the cadence.
- **A forward jump of the wall clock** (`time.time()` and file mtimes) shortens one wait by the
  jump in both designs. The alternative absorbs up to 0.3 s here (cadence margin + the fastest
  out + the fastest answer); 0.4 s fails.

Vacuity: an invariant "fewer than 4 arrivals" fails on `today_jitter_rate3`, `after_jitter` and
`after_step3`, so the passing configs do reach the arrivals their property is about.

Not modelled: other MusicBrainz clients on this IP (Picard, a browser), and MusicBrainz's global
limiter (300 requests a second across all users), which refuses requests whatever we do. This
model does not explain the 503s in phase 4.1 PR 2's part B run; it says what our pacing does and
does not promise.

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
