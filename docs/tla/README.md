# TLA+ models

Models of the system's design, checked with TLC. A model checks the design, not the Python
code, so each spec names the code and the rulings it models, and a change to those rulings
updates the spec in the same PR (the same rule as `ARCHITECTURE.md`).

| Spec | Models | Rulings |
|---|---|---|
| [MatchingStatus.tla](MatchingStatus.tla) | song `match_status`: curator (API) vs. Huey worker(s) | AUD-R015, R017, R018/R021, R029 |

## Models

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

## Running

Needs Java 11+ and `tla2tools.jar` from the TLA+ releases page
(<https://github.com/tlaplus/tlaplus/releases>). From this folder:

```bash
java -cp tla2tools.jar tla2sany.SANY MatchingStatus.tla
```

```bash
java -XX:+UseParallelGC -cp tla2tools.jar tlc2.TLC -workers auto -config MC_w1_nolink.cfg MatchingStatus.tla
```

`tla2tools.jar` and TLC's `states/` folder are not checked in.
