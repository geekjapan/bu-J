--------------------------- MODULE DispatchSafety ---------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS BrokenDispatch, BrokenPrepare
Workers == {1, 2}
Unresolved == {"PREPARED", "DISPATCHED", "UNKNOWN_EFFECT"}
VARIABLES row, pc, lane, transaction, pending, crashes, claim, sends, unsafe
vars == <<row, pc, lane, transaction, pending, crashes, claim, sends, unsafe>>
Init == /\ row = "ABSENT"
        /\ pc = [w \in Workers |-> "Gate"]
        /\ lane = 0 /\ transaction = 0 /\ pending = FALSE /\ crashes = 0
        /\ claim = [w \in Workers |-> FALSE] /\ sends = 0 /\ unsafe = FALSE

Gate(w) == /\ pc[w] = "Gate"
           /\ pc' = [pc EXCEPT ![w] = IF row \in Unresolved THEN "Done" ELSE "Acquire"]
           /\ UNCHANGED <<row, lane, transaction, pending, crashes, claim, sends, unsafe>>
Acquire(w) == /\ pc[w] = "Acquire" /\ lane = 0
              /\ lane' = w /\ pc' = [pc EXCEPT ![w] = "Observe"]
              /\ UNCHANGED <<row, transaction, pending, crashes, claim, sends, unsafe>>
Advance(w, from, to) == /\ pc[w] = from
                        /\ pc' = [pc EXCEPT ![w] = to]
                        /\ UNCHANGED <<row, lane, transaction, pending, crashes, claim, sends, unsafe>>
Begin(w) == /\ pc[w] = "Begin" /\ transaction = 0
            /\ transaction' = w /\ pc' = [pc EXCEPT ![w] = "Insert"]
            /\ UNCHANGED <<row, lane, pending, crashes, claim, sends, unsafe>>
Insert(w) == /\ pc[w] = "Insert" /\ transaction = w
             /\ pending' = (row = "ABSENT" \/ BrokenPrepare)
             /\ pc' = [pc EXCEPT ![w] = IF row = "ABSENT" \/ BrokenPrepare THEN "Commit" ELSE "Rollback"]
             /\ UNCHANGED <<row, lane, transaction, crashes, claim, sends, unsafe>>
Commit(w) == /\ pc[w] = "Commit" /\ transaction = w /\ pending
             /\ row' = "PREPARED" /\ pending' = FALSE /\ transaction' = 0
             /\ pc' = [pc EXCEPT ![w] = "Dispatch"]
             /\ UNCHANGED <<lane, crashes, claim, sends, unsafe>>
Rollback(w) == /\ pc[w] = "Rollback" /\ transaction = w
               /\ transaction' = 0 /\ pending' = FALSE
               /\ pc' = [pc EXCEPT ![w] = "Release"]
               /\ UNCHANGED <<row, lane, crashes, claim, sends, unsafe>>
Dispatch(w) == /\ pc[w] = "Dispatch" /\ transaction = 0
               /\ row' = IF BrokenDispatch THEN row
                           ELSE IF row = "PREPARED" THEN "DISPATCHED" ELSE row
               /\ claim' = [claim EXCEPT ![w] = (~BrokenDispatch /\ row = "PREPARED")]
               /\ pc' = [pc EXCEPT ![w] = IF BrokenDispatch \/ row = "PREPARED" THEN "Validate" ELSE "Release"]
               /\ UNCHANGED <<lane, transaction, pending, crashes, sends, unsafe>>
Send(w) == /\ pc[w] = "Send" /\ sends < 2
           /\ sends' = sends + 1 /\ unsafe' = (unsafe \/ ~claim[w])
           /\ pc' = [pc EXCEPT ![w] = "After"]
           /\ UNCHANGED <<row, lane, transaction, pending, crashes, claim>>
Finish(w) == /\ pc[w] = "Finish" /\ transaction = 0
             /\ row' \in IF row = "DISPATCHED" THEN {"VERIFIED", "UNKNOWN_EFFECT"} ELSE {row}
             /\ pc' = [pc EXCEPT ![w] = "Release"]
             /\ UNCHANGED <<lane, transaction, pending, crashes, claim, sends, unsafe>>
Release(w) == /\ pc[w] = "Release" /\ lane = w
              /\ lane' = 0 /\ pc' = [pc EXCEPT ![w] = "Done"]
              /\ UNCHANGED <<row, transaction, pending, crashes, claim, sends, unsafe>>
Crash(w) == /\ crashes = 0 /\ pc[w] \notin {"Done", "Crashed"}
            /\ crashes' = 1 /\ pc' = [pc EXCEPT ![w] = "Crashed"]
            /\ lane' = IF lane = w THEN 0 ELSE lane
            /\ transaction' = IF transaction = w THEN 0 ELSE transaction
            /\ pending' = IF transaction = w THEN FALSE ELSE pending
            /\ claim' = [claim EXCEPT ![w] = FALSE]
            /\ UNCHANGED <<row, sends, unsafe>>
Recover(w) == /\ pc[w] = "Crashed" /\ transaction = 0
              /\ row' = IF row \in {"PREPARED", "DISPATCHED"} THEN "UNKNOWN_EFFECT" ELSE row
              /\ pc' = [pc EXCEPT ![w] = "Gate"]
              /\ UNCHANGED <<lane, transaction, pending, crashes, claim, sends, unsafe>>
Next == \E w \in Workers:
        Gate(w) \/ Acquire(w) \/ Advance(w, "Observe", "Authorize")
        \/ Advance(w, "Authorize", "Audit") \/ Advance(w, "Audit", "Begin")
        \/ Begin(w) \/ Insert(w) \/ Commit(w) \/ Rollback(w) \/ Dispatch(w)
        \/ Advance(w, "Validate", "Send") \/ Send(w)
        \/ Advance(w, "After", "Finish") \/ Finish(w) \/ Release(w)
        \/ Crash(w) \/ Recover(w)
NoDuplicateSend == sends <= 1
DurableDispatchBeforeSend == ~unsafe
TypeOK == /\ row \in {"ABSENT", "PREPARED", "DISPATCHED", "VERIFIED", "UNKNOWN_EFFECT"}
          /\ sends \in 0..2 /\ lane \in {0, 1, 2} /\ transaction \in {0, 1, 2}
          /\ crashes \in 0..1
Spec == Init /\ [][Next]_vars
=============================================================================
