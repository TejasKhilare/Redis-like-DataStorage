# ADR-0010: Rebalancing by moving only the keys that change owner

**Status:** accepted (Phase 4)

## Context
Adding or removing a shard group changes the ring, and with it the owner of
some keys. Those keys must move while the cluster keeps serving them: no
lost updates, no key visible in two places, and no key that disappears while
it is in transit.

## Decision
**The ring holds shard-group ids, not addresses.** A failover changes a
group's primary address but moves no keys. Only a change to the set of groups
changes ownership. With consistent hashing, adding a group to N moves about
1/(N+1) of the keys, all of them to the new group.

**Migration is Redis Cluster's slot migration, per key.** It runs in four steps.

1. **Announce.** The manager publishes a config (epoch+1) that lists the new
   group and the *target* ring, but still routes by the old ring.
2. **Move.** Each source primary (`CLUSTER REBALANCE <plan>`) scans its keys
   and moves those whose owner changes, in batches: `DUMP` locally,
   `RESTORE ... REPLACE` on the new owner, then `DEL` locally. The DEL also
   reaches its AOF and replicas; the RESTORE reaches the target's.
3. **Redirect meanwhile.** Before a command runs on a source:
   - a key still there is served there, and any change moves with it later;
   - a key in a batch in flight answers `-TRYAGAIN`, because a write now
     would be lost;
   - a key that isn't there and belongs elsewhere answers
     `-ASK <group> <address>`: it has moved already, or it is new and must be
     created at its new owner. The router follows both transparently.
4. **Switch, then stop redirecting.** When every source reports done, the
   manager publishes the new ring (epoch+1) and only then tells the sources to
   stop redirecting. In the opposite order, a moved key would briefly read as
   missing.

`RESTORE` carries an absolute expiry (`ABSTTL`) in the AOF and the stream, so
replaying it later gives the same result. A failover during a rebalance is
handled by restarting the affected sources' moves from the current config.
Restarting is safe because moves are idempotent (`REPLACE`), and keys still
at a source simply move again.

## Consequences
- ✅ Tested:
  - adding a 4th group to 3 moves exactly the keys whose owner changed
    (≈25%), all of them to the new group;
  - every key ends in exactly one place, and none is lost;
  - concurrent writes (including new keys) during the move all land;
  - removing a group moves all of its keys and nothing else.
- ✅ No stop-the-world: the cluster serves throughout. Keys in flight get
  `TRYAGAIN` for a few milliseconds.
- ⚠️ A multi-key command whose keys are split mid-move gets `-TRYAGAIN` until
  the move completes. Hash-tagged keys always move together, but can be split
  across batches.
- ⚠️ Each source scans its whole keyspace (O(n)) to find what moves. Redis
  Cluster's fixed hash slots would let it read a per-slot key index instead.
  Slots remain the better design at scale (see ADR-0004).
- ⚠️ `POST /v1/cluster/shards` returns only when the move is complete. For
  large datasets it should become an asynchronous job that clients poll.
