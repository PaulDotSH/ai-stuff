---
name: postgres-replication-ha
description: >
  Set up, operate, and troubleshoot PostgreSQL replication and high
  availability: streaming replicas, replication slots and disk-filling WAL,
  lag monitoring, synchronous replication and synchronous_commit, hot
  standby query conflicts and hot_standby_feedback, read-replica routing and
  read-after-write, logical replication (publications, subscriptions,
  conflicts, failover slots), HA tools (Patroni, pg_auto_failover,
  CloudNativePG), split brain, and failback with pg_rewind. Use for any
  replica, failover, or logical replication task.
---

# Postgres replication and high availability

A playbook distilled from *Scaling Postgres* and its replication tutorials.
Evidence and version history are in [reference.md](reference.md). Related
skills:

- Backups and PITR: `postgres-backup-recovery`.
- Logical-replication upgrades: `postgres-upgrades`.

## Building a streaming replica

- Build it with `pg_basebackup -R` (writes `primary_conninfo` and
  `standby.signal` on PG12+).
- Use a replication role, a `pg_hba.conf` entry, and `hot_standby = on`.
- Enable `wal_log_hints = on` or data checksums now if you ever want
  `pg_rewind`.
- **Slots**
  - Use a physical slot, so the primary keeps the WAL the replica needs.
  - Always set `max_slot_wal_keep_size`. An inactive slot otherwise retains
    WAL until the disk fills and the primary stops.
  - Alert on `active = false`.
  - Never delete files from `pg_wal` by hand.

## Monitoring lag

```sql
-- on the primary
SELECT application_name, state, sync_state,
       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_bytes,
       write_lag, flush_lag, replay_lag
FROM pg_stat_replication;
SELECT slot_name, active,
       pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes
FROM pg_replication_slots;
```

- Track send/receive lag and replay lag separately. Replay lag can come from a
  replica query conflict, not the network.

## Durability: synchronous replication

- Async (the default) can lose recently committed transactions on failover.
- For low RPO, use synchronous standbys. Prefer quorum
  (`synchronous_standby_names = 'ANY 1 (a, b)'`) with standbys close to the
  primary, because every commit waits for the round trip.
- `synchronous_commit` can be set per transaction or role, to match each
  workload's durability. The ladder is `off` → `local` → `remote_write` → `on`
  → `remote_apply`.

## Read replicas

- Replica queries can be cancelled by replay conflicts.
  - `hot_standby_feedback = on` reduces cancellations but moves bloat to the
    primary.
  - `max_standby_streaming_delay` trades replica lag for fewer cancellations.
  - Keep long analytics off feedback-enabled replicas.
- **Read-after-write.** A user who just wrote may not see the write on a
  replica. Read from the primary for that session or request, or wait for the
  LSN (PG19 `WAIT FOR LSN`, or compare LSNs yourself).
- Route by consistency need, not by "is it a SELECT".

## Logical replication

- DDL and sequences are **not** replicated. Apply schema changes on the
  subscriber first, and sync sequences at cutover.
- A conflict (duplicate key, missing row) stops the apply worker and the
  stream stalls. Monitor subscription errors and resolve them. Use
  `ALTER SUBSCRIPTION ... SKIP` only deliberately.
- Tables need a primary key or a `REPLICA IDENTITY` for UPDATE/DELETE.
- PG16 can decode from a standby, and PG17 adds failover slots so logical
  consumers survive a primary failover.

## High availability

- Use a proven manager (Patroni, pg_auto_failover, CloudNativePG on
  Kubernetes) rather than hand-rolled scripts.
- Require at least three voting members (DCS or monitor), so a partition
  can't elect two primaries.
- **Split brain.** Fence the old primary: STONITH, or the manager demoting
  it. Clients must follow the new primary via the manager's endpoint or
  libpq multi-host strings with `target_session_attrs=read-write`.
- **Failback.** Rejoin the old primary with `pg_rewind` (it needs
  `wal_log_hints` or checksums) rather than a full re-clone. Expect lost
  async transactions.
- **Multi-master / active-active.** Avoid it unless you can design out
  conflicts. Scale writes by sharding or functional splits instead.
