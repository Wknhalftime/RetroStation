# TLA+ models

Models of the system's design, checked with TLC. A model checks the design, not the Python
code, so each spec names the code and the rulings it models, and a change to those rulings
updates the spec in the same PR (the same rule as `ARCHITECTURE.md`).

| Spec | Models | Rulings |
|---|---|---|
| [MatchingStatus.tla](MatchingStatus.tla) | song `match_status`: curator (API) vs. Huey worker(s) | AUD-R015, R017, R018/R021, R029; #264 |
| [MBPacing.tla](MBPacing.tla) | MusicBrainz request pacing: `mb_clock.wait_turn` + `mb_client._fetch`, API and worker | AUD-R102, R104 |

## MatchingStatus models

Two guards on the worker's write (`update_match_status_if_pending`): the status guard
(`Guarded`, AUD-R018) and, since #264, the rejected-files guard (`RejectedGuard`: the song's
`rejected_file_ids` must equal, as a set, the list the worker read). `rejected_file_ids` is a
version per song (`rej`), bounded by `MaxEpoch` like the pending periods.

| Config | Workers | Link cascade | Status guard | Rejected guard | TLC result (2026-11-23) |
|---|---|---|---|---|---|
| `MC_w1_nolink.cfg` | 1 | off | on | on | all hold (334,304 distinct states) |
| `MC_w2_nolink.cfg` | 2 | off | on | on | `NoStaleWrite` fails: a second worker's re-check rewinds a song the first worker read (why AUD-R017 exists) |
| `MC_w1_link.cfg` | 1 | on | on | on | `NoStaleWrite` fails: see below |
| `MC_unguarded.cfg` | 1 | off | off | off | negative control: `CuratorDecisionStands` fails (the worker overwrites a rejection it read as pending) |
| `MC_w1_link_before264.cfg` | 1 | on | on | off | negative control: `NoRejectedStaleWrite` fails: read, curator rejects a file, links the artist (back to pending), worker writes from the old list |
| `MC_w2_nolink_guards.cfg` | 2 | off | on | on | all hold without `NoStaleWrite` (7,392,504) |
| `MC_w1_link_guards.cfg` | 1 | on | on | on | all hold without `NoStaleWrite` (7,200,791) |
| `MC_w2_link_guards.cfg` | 2 | on | on | on | all hold without `NoStaleWrite` (158,624,959) |

The `_guards` configs leave `NoStaleWrite` out so TLC reaches the other properties. With both
guards on, `NoRejectedStaleWrite` holds by construction (the guard and the ghost test the same
equality in the same step); what the runs add is that `CuratorDecisionStands` and
`WorkersNeverWriteManual` hold with the rejected guard in, for two workers and the link cascade.
The informative results are the before-#264 control failing and `NoStaleWrite` still failing.

**Why `MC_w1_link` still fails after #264.** `NoStaleWrite` asks more than #264 guards: that a
worker's write lands only on the pending period it read. A song can leave pending and come back
without its rejected files changing, and the write then lands:

1. the worker reads the song pending;
2. the curator rejects the artist (the song goes `auto_rejected`; no file is rejected);
3. the curator links the artist (the cascade sends the song back to `pending`);
4. the worker writes its result from the read in step 1.

TLC finds this with rejections that always add a file (checked on a scratch copy). The committed
model also lets a Reject or Unmatch record a file already in the list (a widening: the service
accepts any `shown_file_id`, and a manual match to a rejected file can be unmatched), which gives
a shorter trace through `CuratorReject`. Either way the rejected-files list the worker used is
the current one, so it cannot suggest a rejected file; what is stale is the rest of the read.

**A lead the model found, reproduced in code.** `match_identities_for_playlist` reads each pending song's artist once,
at stage start (`identity_matching_service.py`, `artists_by_id`), and the match job the link
cascade enqueues reads only pending songs. So a song the running job read before an artist link
can be decided from the pre-link artist and then skipped by the link's own job, until the next
re-check rewinds it if it ended undecided. Worker results are nondeterministic in this model, so
whether a pre-link read gives a different decision is a question for the code, not for TLC. A
fakes-based test (`tests/services/test_stale_artist_link_read.py`, branch
`test/stale-artist-link-read`, 2026-11-23) shows it does: with two near titles that tie on the
name path, the song the run read before the link ends in review, where the link's Step A would
auto-match it, and the link's job skips it.

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
