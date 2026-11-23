---------------------------- MODULE MatchingStatus ----------------------------
(***************************************************************************)
(* The song match_status contract between the curator (the API) and the    *)
(* Huey worker(s): ARCHITECTURE.md "Background work", AUD-R015 (status     *)
(* columns are the work list), R017 (one worker per queue), R018/R021      *)
(* (guarded worker writes), R029 (a rejected song is re-checked next run). *)
(*                                                                         *)
(* What is modelled, and where it lives in the code:                       *)
(*   - Worker match job: identity_matching_service.match_identities_for_   *)
(*     playlist. It reads every pending song once (get_pending_for_        *)
(*     playlist, rejected_file_ids included), then writes each song with   *)
(*     update_match_status_if_pending. One transaction per playlist.       *)
(*   - Worker re-check: matching_recheck_service.rewind_wave              *)
(*     (rewind_undecided + rewind_mb_unavailable), then fan-out.           *)
(*   - Worker scan: missing_file_reconciliation_service._release_matches,  *)
(*     an UNGUARDED write to needs_review when a song's file is deleted.   *)
(*   - Curator: routers/matching.py song resolve (unguarded), artist       *)
(*     link (MANUAL_MATCHED artist) and artist reject cascades.           *)
(*                                                                         *)
(* Abstractions (each one only widens what TLC explores):                 *)
(*   - Every step is one committed SQL statement. The worker really holds  *)
(*     row locks until its playlist commit; locks only remove orderings,   *)
(*     so a safety property that holds here holds with them. This needs    *)
(*     READ COMMITTED, PostgreSQL's default (no isolation level is set in  *)
(*     backend/).                                                          *)
(*   - Artist status and artist matching are not modelled; the curator's   *)
(*     artist actions appear only as their cascades onto songs.           *)
(*   - Matching results are nondeterministic: any worker result can come   *)
(*     out for any song.                                                   *)
(*   - Huey delivers at least once: a taken message may stay queued.      *)
(*                                                                         *)
(* Ghost variables (not in the database, only for the properties):        *)
(*   lastWriter  who wrote the row last                                    *)
(*   epoch       how many times the song has entered pending               *)
(*   snap        the epoch a worker saw when it read the song             *)
(*   staleWrites guarded writes that landed on a later pending period     *)
(*               than the one the worker read                             *)
(*   staleRejWrites  guarded writes that landed although the song's       *)
(*               rejected files changed since the worker read them        *)
(*                                                                         *)
(* rejected_file_ids is modelled by a version per song (rej): a Reject or  *)
(* Unmatch either adds a file (a new version) or records one already in   *)
(* the list (same set, same version). The #264 guard compares sets         *)
(* (@> and <@), so it is version equality here. A library-file fold       *)
(* (AUD-R022) rewrites the list too; it only makes the guard skip more,   *)
(* so it is left out.                                                      *)
(***************************************************************************)
EXTENDS Naturals, Sequences

CONSTANTS
    Songs,        \* model values, e.g. {s1, s2}
    Workers,      \* {w1} is the AUD-R017 constraint; {w1, w2} breaks it
    MaxMsgs,      \* bound on messages ever sent (keeps the state space finite)
    MaxEpoch,     \* bound on pending periods per song (same reason)
    Guarded,      \* FALSE = negative control: worker writes ignore the guard
    RejectedGuard, \* FALSE = before #264: the guard ignores rejected files
    LinkCascade   \* FALSE = leave out the curator's artist-link cascade

Statuses == {"pending", "auto_matched", "needs_review", "auto_rejected",
             "manual_matched", "manual_rejected"}
WorkerResults == {"auto_matched", "needs_review", "auto_rejected"}
\* domain/matching.py SONG_UNDECIDED_STATUSES
SongUndecided == {"needs_review", "auto_rejected", "manual_rejected"}
\* routers/matching.py: the link cascade rewinds the same three statuses
LinkRewind == SongUndecided
\* routers/matching.py _PROTECTED_STATUSES (artist reject cascade skips these)
Protected == {"manual_matched", "manual_rejected"}
Writers == {"ingest", "curator", "worker", "recheck", "release"}
Msgs == {"match", "recheck", "scan"}
Jobs == {"idle"} \cup Msgs

VARIABLES
    status,       \* track_identities.match_status
    hasMatch,     \* the song has a matches row
    lastWriter,   \* ghost
    epoch,        \* ghost
    queue,        \* the Huey queue (one SQLite queue, FIFO)
    sent,         \* messages sent so far
    job,          \* what each worker is running
    snap,         \* ghost: per worker, the epoch it read per song (0 = not read)
    staleWrites,  \* ghost
    rej,          \* rejected_file_ids, as a version per song
    snapRej,      \* per worker, the rejected files it read per song (a version)
    staleRejWrites \* ghost

vars == <<status, hasMatch, lastWriter, epoch, queue, sent, job, snap, staleWrites,
          rej, snapRej, staleRejWrites>>

TypeOK ==
    /\ status \in [Songs -> Statuses]
    /\ hasMatch \in [Songs -> BOOLEAN]
    /\ lastWriter \in [Songs -> Writers]
    /\ epoch \in [Songs -> Nat \ {0}]
    /\ queue \in Seq(Msgs)
    /\ sent \in 0..MaxMsgs
    /\ job \in [Workers -> Jobs]
    /\ snap \in [Workers -> [Songs -> Nat]]
    /\ staleWrites \in Nat
    /\ rej \in [Songs -> Nat]
    /\ snapRej \in [Workers -> [Songs -> Nat]]
    /\ staleRejWrites \in Nat

-----------------------------------------------------------------------------
(* One UPDATE: the rows it matched take value val. A row that goes from   *)
(* another status to pending starts a new pending period.                 *)
Write(rows, val, who) ==
    /\ status' = [s \in Songs |-> IF s \in rows THEN val ELSE status[s]]
    /\ lastWriter' = [s \in Songs |-> IF s \in rows THEN who ELSE lastWriter[s]]
    /\ epoch' = [s \in Songs |->
                    IF s \in rows /\ status[s] # "pending" /\ val = "pending"
                    THEN epoch[s] + 1 ELSE epoch[s]]

(* A non-blocking enqueue (enqueue_or_log): past the bound it is lost,    *)
(* which the real system also allows.                                    *)
Enqueue(m) ==
    IF sent < MaxMsgs
    THEN /\ queue' = Append(queue, m)
         /\ sent' = sent + 1
    ELSE UNCHANGED <<queue, sent>>

Init ==
    /\ status = [s \in Songs |-> "pending"]
    /\ hasMatch = [s \in Songs |-> FALSE]
    /\ lastWriter = [s \in Songs |-> "ingest"]
    /\ epoch = [s \in Songs |-> 1]
    /\ queue = <<"match">>          \* ingestion hands off to matching
    /\ sent = 1
    /\ job = [w \in Workers |-> "idle"]
    /\ snap = [w \in Workers |-> [s \in Songs |-> 0]]
    /\ staleWrites = 0
    /\ rej = [s \in Songs |-> 0]
    /\ snapRej = [w \in Workers |-> [s \in Songs |-> 0]]
    /\ staleRejWrites = 0

-----------------------------------------------------------------------------
(* Curator actions: the API, one transaction each, any time.              *)

\* POST /identities/{id}/resolve MANUAL_MATCHED: unguarded, replaces the match row
CuratorMatch(s) ==
    /\ Write({s}, "manual_matched", "curator")
    /\ hasMatch' = [hasMatch EXCEPT ![s] = TRUE]
    /\ UNCHANGED <<queue, sent, job, snap, staleWrites,
                   rej, snapRej, staleRejWrites>>

\* Reject or Unmatch a song: manual_rejected, match rows deleted, file recorded
CuratorReject(s) ==
    /\ Write({s}, "manual_rejected", "curator")
    /\ hasMatch' = [hasMatch EXCEPT ![s] = FALSE]
    /\ \/ rej' = [rej EXCEPT ![s] = @ + 1]   \* a file not yet rejected
       \/ UNCHANGED rej                       \* a file already in the list
    /\ UNCHANGED <<queue, sent, job, snap, staleWrites, snapRej, staleRejWrites>>

\* Artist resolve MANUAL_MATCHED: undecided children go back to pending, every
\* pending child loses its match rows, identity matching is enqueued
CuratorLinkArtist ==
    /\ LinkCascade
    /\ LET rows == {s \in Songs : status[s] \in LinkRewind}
       IN /\ Write(rows, "pending", "curator")
          /\ hasMatch' = [s \in Songs |->
                            IF s \in rows \/ status[s] = "pending"
                            THEN FALSE ELSE hasMatch[s]]
    /\ Enqueue("match")
    /\ UNCHANGED <<job, snap, staleWrites,
                   rej, snapRej, staleRejWrites>>

\* Artist resolve MANUAL_REJECTED: every child not manually decided is auto_rejected
CuratorRejectArtist ==
    /\ Write({s \in Songs : status[s] \notin Protected}, "auto_rejected", "curator")
    /\ UNCHANGED <<hasMatch, queue, sent, job, snap, staleWrites,
                   rej, snapRej, staleRejWrites>>

\* POST /matching/run (Re-run Matching)
CuratorRerun ==
    /\ Enqueue("recheck")
    /\ UNCHANGED <<status, hasMatch, lastWriter, epoch, job, snap, staleWrites,
                   rej, snapRej, staleRejWrites>>

\* The watcher poll queues a scan, which may find a deleted file
WatcherPoll ==
    /\ Enqueue("scan")
    /\ UNCHANGED <<status, hasMatch, lastWriter, epoch, job, snap, staleWrites,
                   rej, snapRej, staleRejWrites>>

-----------------------------------------------------------------------------
(* Worker actions.                                                        *)

\* Take the head of the queue. At-least-once: the message may stay queued.
\* A match job reads its pending songs here (get_pending_for_playlist).
Take(w) ==
    /\ job[w] = "idle"
    /\ queue # <<>>
    /\ LET m == Head(queue)
       IN /\ job' = [job EXCEPT ![w] = m]
          /\ \/ /\ queue' = Tail(queue)
                /\ UNCHANGED sent
             \/ /\ sent < MaxMsgs
                /\ queue' = Append(Tail(queue), m)
                /\ sent' = sent + 1
          /\ snap' = IF m = "match"
                     THEN [snap EXCEPT ![w] = [s \in Songs |->
                              IF status[s] = "pending" THEN epoch[s] ELSE 0]]
                     ELSE snap
          /\ snapRej' = IF m = "match" THEN [snapRej EXCEPT ![w] = rej] ELSE snapRej
    /\ UNCHANGED <<status, hasMatch, lastWriter, epoch, staleWrites, rej, staleRejWrites>>

\* One song's result: update_match_status_if_pending, then its match row
WriteSong(w, s) ==
    /\ job[w] = "match"
    /\ snap[w][s] # 0
    /\ \E r \in WorkerResults :
         LET lands == /\ Guarded => status[s] = "pending"
                      /\ RejectedGuard => rej[s] = snapRej[w][s]
         IN IF lands
            THEN /\ Write({s}, r, "worker")
                 /\ hasMatch' = [hasMatch EXCEPT ![s] = (r # "auto_rejected")]
                 /\ staleWrites' = staleWrites +
                                   (IF snap[w][s] # epoch[s] THEN 1 ELSE 0)
                 /\ staleRejWrites' = staleRejWrites +
                                      (IF snapRej[w][s] # rej[s] THEN 1 ELSE 0)
            ELSE UNCHANGED <<status, hasMatch, lastWriter, epoch, staleWrites,
                             staleRejWrites>>
    /\ snap' = [snap EXCEPT ![w][s] = 0]
    /\ UNCHANGED <<queue, sent, job, rej, snapRej>>

\* Every song read has been written: the playlist commits
FinishMatch(w) ==
    /\ job[w] = "match"
    /\ \A s \in Songs : snap[w][s] = 0
    /\ job' = [job EXCEPT ![w] = "idle"]
    /\ UNCHANGED <<status, hasMatch, lastWriter, epoch, queue, sent, snap, staleWrites,
                   rej, snapRej, staleRejWrites>>

\* rewind_wave: undecided songs back to pending (match rows kept), then fan-out
DoRecheck(w) ==
    /\ job[w] = "recheck"
    /\ Write({s \in Songs : status[s] \in SongUndecided}, "pending", "recheck")
    /\ Enqueue("match")
    /\ job' = [job EXCEPT ![w] = "idle"]
    /\ UNCHANGED <<hasMatch, snap, staleWrites,
                   rej, snapRej, staleRejWrites>>

\* _release_matches: a deleted file's song with no match left goes to review,
\* whatever its status (unguarded). Or the scan finds nothing deleted.
DoScan(w) ==
    /\ job[w] = "scan"
    /\ \/ \E s \in Songs :
            /\ hasMatch[s]
            /\ Write({s}, "needs_review", "release")
            /\ hasMatch' = [hasMatch EXCEPT ![s] = FALSE]
       \/ UNCHANGED <<status, hasMatch, lastWriter, epoch>>
    /\ job' = [job EXCEPT ![w] = "idle"]
    /\ UNCHANGED <<queue, sent, snap, staleWrites,
                   rej, snapRej, staleRejWrites>>

-----------------------------------------------------------------------------
Next ==
    \/ \E s \in Songs : CuratorMatch(s) \/ CuratorReject(s)
    \/ CuratorLinkArtist
    \/ CuratorRejectArtist
    \/ CuratorRerun
    \/ WatcherPoll
    \/ \E w \in Workers :
         \/ Take(w)
         \/ FinishMatch(w)
         \/ DoRecheck(w)
         \/ DoScan(w)
         \/ \E s \in Songs : WriteSong(w, s)

Spec == Init /\ [][Next]_vars

StateConstraint == \A s \in Songs : epoch[s] <= MaxEpoch /\ rej[s] <= MaxEpoch

-----------------------------------------------------------------------------
(* Properties.                                                            *)

\* A worker's write lands only on the pending period it read. If this fails,
\* the worker decided a song from a read taken before the song left pending
\* and came back. Stricter than what #264 guards (NoRejectedStaleWrite).
NoStaleWrite == staleWrites = 0

\* What #264 guards: a worker's write lands only if the song's rejected files
\* are the ones it read, so it never suggests a file rejected since its read.
NoRejectedStaleWrite == staleRejWrites = 0

\* Workers never produce a manual status.
WorkersNeverWriteManual ==
    \A s \in Songs : lastWriter[s] \in {"worker", "recheck"}
                     => status[s] \notin Protected

\* AUD-R018 / R029: the curator's decision stands. An action property: it
\* talks about a step (status -> status', who wrote it), so it is checked as
\* [][...]_vars under PROPERTIES, not INVARIANTS.
\* Allowed moves (Lance, 2026-11-22): the curator may change any decision,
\* the artist-link cascade included; a deleted file's release may send a
\* manual match to review; a re-check may send a rejected song back to
\* pending (AUD-R029). No other writer moves a manual status.
CuratorDecisionStands ==
    [][ \A s \in Songs :
          /\ (status[s] = "manual_matched" /\ status'[s] # "manual_matched")
                => lastWriter'[s] \in {"curator", "release"}
          /\ (status[s] = "manual_rejected" /\ status'[s] # "manual_rejected")
                => \/ lastWriter'[s] = "curator"
                   \/ (lastWriter'[s] = "recheck" /\ status'[s] = "pending")
      ]_vars

=============================================================================
