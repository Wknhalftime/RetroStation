------------------------------ MODULE MBPacing ------------------------------
(***************************************************************************)
(* MusicBrainz request pacing: the machine-wide clock that every            *)
(* MusicBrainzApiClient waits on (AUD-R102, PR #231), with the API process  *)
(* and the Huey worker as the clients.                                      *)
(*                                                                         *)
(* What is modelled, and where it lives in the code:                       *)
(*   - mb_clock.wait_turn: take the file lock, read the clock file's mtime,  *)
(*     sleep min(cadence, max(0, last + cadence - time.time())), touch the  *)
(*     clock file, release the lock.                                        *)
(*   - mb_client.MusicBrainzApiClient._fetch: wait_turn(), then             *)
(*     self._http.get(). The request leaves AFTER the lock is released.     *)
(*     A tenacity retry re-enters _fetch, so it is one more request here.   *)
(*                                                                         *)
(* The question: MusicBrainz counts requests when they ARRIVE. The lock     *)
(* spaces the clock stamps by the cadence. Does that space the arrivals?    *)
(*                                                                         *)
(* Time is discrete: one tick is 0.1 s, so the cadence of 1.1 s is 11       *)
(* ticks and MusicBrainz's one-second spacing is 10.                        *)
(*                                                                         *)
(* Abstractions:                                                           *)
(*   - "Out" is everything between the stamp and the request reaching      *)
(*     MusicBrainz: the thread being scheduled, DNS, TCP and TLS on a new   *)
(*     connection, one-way network time. It takes MinOut..MaxOut ticks,     *)
(*     chosen freely per request.                                           *)
(*   - "Back" is MusicBrainz's answer reaching us: 1..MaxBack ticks.        *)
(*   - Our clients are the only ones on this IP (no Picard, no browser on   *)
(*     musicbrainz.org). MusicBrainz's global limiter is not modelled.      *)
(*   - The wall clock (time.time(), file mtimes) may jump forward once by   *)
(*     MaxStep ticks; time.sleep runs on true time.                         *)
(*                                                                         *)
(* Design "stamp_before_send" is today's code. Design                      *)
(* "stamp_after_response" is the alternative: hold the lock across the      *)
(* request and touch the clock when the answer (or the error) comes back.  *)
(***************************************************************************)
EXTENDS Integers, Sequences

CONSTANTS
    Procs,      \* model values: the processes sharing the clock, e.g. {api, worker}
    Requests,   \* requests each process makes (keeps the state space finite)
    Cadence,    \* ticks between stamps: 11 = CADENCE_S 1.1 s
    Window,     \* one second in ticks: 10
    RateSeconds, \* MusicBrainz averages over this many seconds (not published)
    MinOut,     \* fewest ticks from the stamp to the request arriving
    MaxOut,     \* most ticks from the stamp to the request arriving
    MaxBack,    \* most ticks from arrival to the answer coming back (>= 1)
    MaxStep,    \* size of the one forward wall-clock jump; 0 = none
    Design,     \* "stamp_before_send" (today) or "stamp_after_response"
    MaxTime,    \* bound on true time (keeps the state space finite)
    None        \* model value: nobody holds the lock

ASSUME
    /\ Requests \in Nat \ {0}
    /\ Cadence \in Nat /\ Window \in Nat /\ RateSeconds \in Nat \ {0}
    /\ MinOut \in Nat /\ MaxOut \in Nat /\ MinOut <= MaxOut
    /\ MaxBack \in Nat \ {0}
    /\ MaxStep \in Nat
    /\ Design \in {"stamp_before_send", "stamp_after_response"}
    /\ None \notin Procs


VARIABLES
    now,         \* true time
    skew,        \* wall time = now + skew
    stepped,     \* the wall clock has jumped
    clock,       \* the clock file's mtime (wall time)
    clockSet,    \* the clock file exists
    lock,        \* the file lock's holder, or None
    pc,          \* per process: "idle", "sleep", "out", "back"
    earliest,    \* per process: first tick the current step may finish
    due,         \* per process: last tick the current step may finish
    done,        \* per process: requests answered
    arrivals     \* ghost: when each request reached MusicBrainz, in order

vars == <<now, skew, stepped, clock, clockSet, lock, pc, earliest, due, done,
          arrivals>>

Min(a, b) == IF a < b THEN a ELSE b
Max(a, b) == IF a > b THEN a ELSE b

Wall == now + skew

Init ==
    /\ now = 0
    /\ skew = 0
    /\ stepped = FALSE
    /\ clock = 0
    /\ clockSet = FALSE
    /\ lock = None
    /\ pc = [p \in Procs |-> "idle"]
    /\ earliest = [p \in Procs |-> 0]
    /\ due = [p \in Procs |-> 0]
    /\ done = [p \in Procs |-> 0]
    /\ arrivals = <<>>

(* wait_turn up to the sleep: take the lock, read the mtime, work out the  *)
(* sleep (capped at one cadence).                                          *)
Begin(p) ==
    /\ pc[p] = "idle"
    /\ done[p] < Requests
    /\ lock = None
    /\ LET wait == IF clockSet THEN Min(Cadence, Max(0, clock + Cadence - Wall))
                                 ELSE 0
       IN /\ due' = [due EXCEPT ![p] = now + wait]
          /\ earliest' = [earliest EXCEPT ![p] = now + wait]
    /\ lock' = p
    /\ pc' = [pc EXCEPT ![p] = "sleep"]
    /\ UNCHANGED <<now, skew, stepped, clock, clockSet, done, arrivals>>

(* The sleep ends and the request leaves. Today: touch the clock and       *)
(* release the lock first. Alternative: keep the lock, touch later.        *)
Send(p) ==
    /\ pc[p] = "sleep"
    /\ now = due[p]
    /\ IF Design = "stamp_before_send"
          THEN /\ clock' = Wall
               /\ clockSet' = TRUE
               /\ lock' = None
          ELSE UNCHANGED <<clock, clockSet, lock>>
    /\ pc' = [pc EXCEPT ![p] = "out"]
    /\ earliest' = [earliest EXCEPT ![p] = now + MinOut]
    /\ due' = [due EXCEPT ![p] = now + MaxOut]
    /\ UNCHANGED <<now, skew, stepped, done, arrivals>>

(* The request reaches MusicBrainz, which counts it now.                   *)
Arrive(p) ==
    /\ pc[p] = "out"
    /\ now >= earliest[p]
    /\ arrivals' = Append(arrivals, now)
    /\ pc' = [pc EXCEPT ![p] = "back"]
    /\ earliest' = [earliest EXCEPT ![p] = now + 1]
    /\ due' = [due EXCEPT ![p] = now + MaxBack]
    /\ UNCHANGED <<now, skew, stepped, clock, clockSet, lock, done>>

(* The answer (or an error, or the timeout) comes back. The alternative    *)
(* touches the clock here, in a finally, and releases the lock.            *)
Answer(p) ==
    /\ pc[p] = "back"
    /\ now >= earliest[p]
    /\ IF Design = "stamp_after_response"
          THEN /\ clock' = Wall
               /\ clockSet' = TRUE
               /\ lock' = None
          ELSE UNCHANGED <<clock, clockSet, lock>>
    /\ pc' = [pc EXCEPT ![p] = "idle"]
    /\ done' = [done EXCEPT ![p] = @ + 1]
    /\ UNCHANGED <<now, skew, stepped, earliest, due, arrivals>>

(* Time passes unless some step has reached its deadline.                  *)
Tick ==
    /\ \A p \in Procs : pc[p] \in {"sleep", "out", "back"} => now < due[p]
    /\ now' = now + 1
    /\ UNCHANGED <<skew, stepped, clock, clockSet, lock, pc, earliest, due, done,
                   arrivals>>

(* The wall clock jumps forward (w32time correcting a slow clock).         *)
Step ==
    /\ MaxStep > 0
    /\ ~stepped
    /\ skew' = skew + MaxStep
    /\ stepped' = TRUE
    /\ UNCHANGED <<now, clock, clockSet, lock, pc, earliest, due, done,
                   arrivals>>

Finished ==
    /\ \A p \in Procs : done[p] = Requests
    /\ UNCHANGED vars

Next ==
    \/ \E p \in Procs : Begin(p) \/ Send(p) \/ Arrive(p) \/ Answer(p)
    \/ Tick
    \/ Step
    \/ Finished

Spec == Init /\ [][Next]_vars

StateConstraint == now <= MaxTime

-----------------------------------------------------------------------------
(* Properties *)

TypeOK ==
    /\ now \in Nat
    /\ lock \in Procs \cup {None}
    /\ pc \in [Procs -> {"idle", "sleep", "out", "back"}]
    /\ done \in [Procs -> 0..Requests]

(* MusicBrainz's per-IP limit is "(on average) 1 request per second",    *)
(* measured over a window it does not publish. With RateSeconds = K:     *)
(* any K+1 of our arrivals span at least K seconds, so no K-second       *)
(* window holds more than K of them. K = 1 is a strict one-second gap.   *)
WithinRate ==
    \A i \in 1..(Len(arrivals) - RateSeconds) :
        arrivals[i + RateSeconds] - arrivals[i] >= RateSeconds * Window

(* Only a process in the middle of a request holds the lock.              *)
OneHolder == lock # None => pc[lock] # "idle"

=============================================================================
