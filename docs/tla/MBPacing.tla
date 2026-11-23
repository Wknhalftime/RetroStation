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
EXTENDS Integers

CONSTANTS
    Procs,      \* model values: the processes sharing the clock, e.g. {api, worker}
    Requests,   \* requests each process makes (keeps the state space finite)
    Cadence,    \* ticks between stamps: 11 = CADENCE_S 1.1 s
    Window,     \* MusicBrainz's spacing between arrivals: 10 = 1 s
    MinOut,     \* fewest ticks from the stamp to the request arriving
    MaxOut,     \* most ticks from the stamp to the request arriving
    MaxBack,    \* most ticks from arrival to the answer coming back (>= 1)
    MaxStep,    \* size of the one forward wall-clock jump; 0 = none
    Design,     \* "stamp_before_send" (today) or "stamp_after_response"
    MaxTime,    \* bound on true time (keeps the state space finite)
    None        \* model value: nobody holds the lock

ASSUME
    /\ Requests \in Nat \ {0}
    /\ Cadence \in Nat /\ Window \in Nat
    /\ MinOut \in Nat /\ MaxOut \in Nat /\ MinOut <= MaxOut
    /\ MaxBack \in Nat \ {0}
    /\ MaxStep \in Nat
    /\ Design \in {"stamp_before_send", "stamp_after_response"}
    /\ None \notin Procs

NoGap == 1000000   \* minGap before a second request has arrived

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
    lastArrival, \* ghost: when the last request reached MusicBrainz (-1 = none)
    minGap       \* ghost: the smallest gap between two arrivals so far

vars == <<now, skew, stepped, clock, clockSet, lock, pc, earliest, due, done,
          lastArrival, minGap>>

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
    /\ lastArrival = -1
    /\ minGap = NoGap

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
    /\ UNCHANGED <<now, skew, stepped, clock, clockSet, done, lastArrival, minGap>>

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
    /\ UNCHANGED <<now, skew, stepped, done, lastArrival, minGap>>

(* The request reaches MusicBrainz, which counts it now.                   *)
Arrive(p) ==
    /\ pc[p] = "out"
    /\ now >= earliest[p]
    /\ minGap' = IF lastArrival = -1 THEN minGap ELSE Min(minGap, now - lastArrival)
    /\ lastArrival' = now
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
    /\ UNCHANGED <<now, skew, stepped, earliest, due, lastArrival, minGap>>

(* Time passes unless some step has reached its deadline.                  *)
Tick ==
    /\ \A p \in Procs : pc[p] \in {"sleep", "out", "back"} => now < due[p]
    /\ now' = now + 1
    /\ UNCHANGED <<skew, stepped, clock, clockSet, lock, pc, earliest, due, done,
                   lastArrival, minGap>>

(* The wall clock jumps forward (w32time correcting a slow clock).         *)
Step ==
    /\ MaxStep > 0
    /\ ~stepped
    /\ skew' = skew + MaxStep
    /\ stepped' = TRUE
    /\ UNCHANGED <<now, clock, clockSet, lock, pc, earliest, due, done,
                   lastArrival, minGap>>

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

(* MusicBrainz never sees two of our requests closer than one second.      *)
ArrivalsSpaced == minGap >= Window

(* Only a process in the middle of a request holds the lock.              *)
OneHolder == lock # None => pc[lock] # "idle"

(* A process that has every request answered is idle (no lost request).    *)
AllAnswered == \A p \in Procs : done[p] = Requests => pc[p] = "idle"

=============================================================================
