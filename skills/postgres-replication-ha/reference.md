# Postgres replication and high availability

Distilled from *Scaling Postgres* episodes 190–439 (Creston Jamison, 2021–2026) and
five standalone tutorials, from auto-generated transcripts. `[NNN]` is an episode
number; `[tutorial: x]` is a standalone tutorial (PG10 era). Many numbers come from
third-party blog posts discussed on the show, so read them as "this happened on one
workload". Where advice changed, the newest take wins. Backups and PITR belong to
postgres-backup-recovery; logical-replication upgrades to postgres-upgrades.

## Checklist

- **Standby build:** physical replica via `pg_basebackup`, `hot_standby` on, replication
  role plus `pg_hba.conf` entry, and `wal_log_hints` or checksums if you ever want
  `pg_rewind`.
- **Slots:** use them, but set `max_slot_wal_keep_size` and alert on inactive slots.
  Never delete files in `pg_wal`.
- **Monitoring:** one row per replica in `pg_stat_replication`, slots `active`, and
  sending, receiving and replay lag tracked separately.
- **Durability:** async is the default and can lose commits on failover. For low RPO use
  quorum sync standbys close to the primary, and set `synchronous_commit` per workload.
- **Read replicas:** never route by "is it a SELECT". Plan for read-after-write
  (PG19 `WAIT FOR LSN`, or read your own writes from the primary).
- **Logical replication:** DDL and sequences are not replicated. Add tables deliberately
  and watch for conflicts, because an error stalls the stream.
- **HA:** use Patroni, pg_auto_failover or CloudNativePG. Require at least three voting
  nodes, and never leave an old primary writable.
- **Multi-master:** avoid it. Scale writes by sharding.

## 1. Streaming replication essentials

**What it is** [tutorial: streaming]
- Physical replication ships WAL and replicates the whole cluster; you cannot pick
  databases or tables. All nodes must be the same major version.
- It is asynchronous by default. Replicas can cascade from other replicas.
- Setup: `wal_level` at least `replica`, enough `max_wal_senders` (backups use them
  too), a role with `REPLICATION` and a `pg_hba.conf` line (use password or SSL, not
  `trust`), then `pg_basebackup` and standby settings.
- `recovery_target_timeline = latest` lets standbys follow a promoted node onto its new
  timeline [tutorial: streaming], [190].

**Building the standby fast** [392]
- `pg_basebackup` is single-threaded: typically 300–500 Mbit/s even on fast hardware.
  PG18 async I/O roughly doubled or tripled that. A parallel pgBackRest copy (32
  processes) reached about 10 Gbit/s, almost 10x faster.

**Replay is the usual bottleneck**
- A single startup process replays WAL one record at a time, so a replica can fall
  behind even with a healthy network [354], [433]. Parallel recovery was still an
  open wish [297].
- ZFS with compression repeatedly left replicas unable to keep up [342], [300], [327].
- PG15 lets WAL replay prefetch via `maintenance_io_concurrency`. In a test, lag grew
  steadily at 0 and stopped growing at higher values. It works only on Linux with
  ext4/xfs [241].
- Replay lag also scales with load on the standby. One test measured 5 ms idle, 25 ms
  with 8 pgbench clients and over 1 s with 48 [433].

**Gotchas**
- Unlogged tables do not exist on replicas [356], [253].
- A replica on a different OS, libc or ICU version can show collation mismatches and
  "missing" rows. Match them [259].
- Minor releases have shipped replica-affecting bugs (standby halts, a PG14–16 replay
  deadlock). Read the release notes first [405], [425].

## 2. Replication slots

**Benefit.** Slots make the primary retain WAL until the replica has it, replacing
guesswork with `wal_keep_size` (`wal_keep_segments` in the tutorial) [tutorial: slots].

**Risk: disk fills.**
- An orphaned slot (replica gone) retains WAL without bound [tutorial: slots].
- Other causes of `pg_wal` growth are a failing or slow `archive_command` and an
  oversized `max_wal_size` or `wal_keep_size`. Never delete `pg_wal` files by hand
  [277], [326], [271].
- An 11 TB `pg_wal` incident came from a broken archive command (permissions), not a
  slot. Check `pg_stat_archiver` before blaming slots [256], [271].
- Orphan slots also hold back vacuum and XID cleanup, which is a second reason to
  hunt them [205], [226], [207].

**Cap them.**
- `max_slot_wal_keep_size` bounds the damage [270].
- A slot past the cap is invalidated. For logical slots you must restart replication
  from scratch [374].

**Housekeeping**
- Slots are not replicated: after a promotion the new primary has none, and you
  recreate them [tutorial: slots], [tutorial: easy failback].
- After `pg_rewind` the data directory carries the *source's* slots. Drop the stale ones
  and create the right slot [tutorial: pg_rewind].
- For logical slots, `restart_lsn` is pinned by the oldest in-flight transaction, while
  `confirmed_flush_lsn` is what the consumer has acknowledged. Long transactions block
  WAL recycling [378].

## 3. Monitoring lag

**On the primary** [282], [tutorial: monitoring]
- `pg_stat_replication` has one row per standby (or per downstream, for cascading).
  A missing row means the replica is disconnected; `state` should be `streaming`.
- Check `pg_replication_slots.active` as well.
- The pipeline is `sent_lsn`, then `write_lsn` (handed to the OS), `flush_lsn` (on
  disk), `replay_lsn` (visible to queries).
- `pg_wal_lsn_diff(a, b)` gives byte lag between any two LSNs.
- Reading the gaps:
  - sent behind current: the primary is overloaded.
  - written or flushed behind sent: network or standby I/O.
  - replay behind flushed: the standby cannot apply fast enough.
- `write_lag`, `flush_lag` and `replay_lag` are intervals. They go NULL when the system
  is idle.

**On the standby**
- `pg_is_in_recovery()`, `pg_last_wal_receive_lsn()`, `pg_last_wal_replay_lsn()`,
  `pg_last_xact_replay_timestamp()` and `pg_stat_wal_receiver` [tutorial: monitoring], [282].
- PG19 adds `pg_stat_recovery` [433].

**Also watch** replication lag, archive backlog and WAL generation rate [229].
Sessions waiting on `IPC:SyncRep` mean standbys are lagging and commits are blocked
[413]. One cause: `autovacuum_vacuum_cost_delay = 0` produced WAL storms of full-page
images and lag, which went away at 2 ms [390], [413].

## 4. Synchronous replication and RPO

**Why.** Async can lose the last commits if the primary dies before WAL reaches a
standby [329]. Jepsen testing of CloudNativePG showed loss without sync and none with it
[382].

**`synchronous_commit` ladder** [329], [365]
- `local`, `remote_write`, `on` (flushed on the standby), `remote_apply` (replayed, so
  queries on the standby see it). Each step adds latency.
- It can be set per session, user or transaction. Set it narrowly rather than globally,
  so batch jobs and migrations do not pay [365], [433].
- Plain `on` can still serve stale reads, because flushed is not applied [433].

**Latency.** Keep the sync standby in the same AZ or datacenter, and use async replicas
for other AZs. At 3 ms of network delay the four levels converged closely [329].
- The RDS Multi-AZ *instance* (sync standby on EBS) was the slowest option. The Multi-AZ
  *cluster* (semi-sync, local NVMe) roughly matched a single instance [320].

**Quorum.** For low RPO use multiple sync standbys with a quorum (for example any 2 of
3) across AZs [270], [433]. Define RPO and RTO first [270].
- Without a reachable sync standby the primary cannot commit [tutorial: streaming].
- A "preferred" durability mode that drops sync when no replica exists lost data in
  some failure runs (48% in one, 4% in another), against 0% for "required" [387], [348].
- Sync is not fully synchronous: the primary keeps generating WAL while a commit
  waits [390]. Even sync setups have loss modes [375].

## 5. Hot standby query conflicts

- Long queries on a standby conflict with replay (vacuum cleanup, and vacuum's
  truncation phase taking an `ACCESS EXCLUSIVE` lock) and get cancelled [227], [419].
- Two knobs, both trade-offs [227]:
  - `hot_standby_feedback` stops cancellations but holds back vacuum on the primary,
    causing bloat [226], [207].
  - A long `max_standby_streaming_delay` pauses replay instead, so the standby lags.
    One user turned feedback off and set about six hours.
- Analytics options, in order of isolation:
  - A dedicated physical replica. It has no schema or index freedom, and long queries
    create replay lag and cancellations [334], [412].
  - WAL log-shipping replicas, which put no load on the primary [412].
  - A logical replica, where cancellations do not occur [258].
- Delayed replicas are not covered in this source material.

## 6. Read routing and read-after-write

- **Never route by statement type.** A `SELECT` can call a function that writes [333].
- Replica reads are eventually consistent. A just-placed cart item may not appear [298].
- **Old workarounds:** pin reads to the primary, sleep, or Redis flags [433]. In one
  test, write-then-read-replica returned stale data 992 times out of 1,000. A 50 ms
  sleep fixed staleness but set a 50 ms latency floor [433].
- **PG19 `WAIT FOR LSN`** (RC1 as of [439]) [432], [433]:
  1. After the write, capture the current WAL flush LSN.
  2. Before the replica read, wait for that LSN. Modes: standby replay, flush, write,
     or primary flush.
  3. Set a timeout and optionally `NO_THROW`.
  - Result: zero stale reads at only 1–2 ms extra latency.
  - It holds a connection while waiting.
  - Too-tight timeouts push every read to the primary, so fall back to the primary on
    timeout.
- **Client-side routing:** libpq multi-host strings with `target_session_attrs`
  (PG14 added `read-only`, `primary`, `standby`, `prefer-standby`). Roles are only
  re-evaluated on reconnect [210]. HAProxy gives read-write and read-only endpoints [410].
- Scale-out limit: OpenAI's roughly 50 replicas made WAL fan-out real load on the
  primary, so they were studying cascading replication [402].

## 7. Logical replication

**Model** [370], [223]
- Row-level changes through publications and subscriptions. It works across versions
  and OSes, targets are writable, and you can replicate subsets. It is not a failover
  mechanism [258].
- Setup:
  - Set `wal_level = logical` and size `max_replication_slots` and `max_wal_senders`.
  - Create the subscriber role, then `CREATE PUBLICATION` and `CREATE SUBSCRIPTION`.
  - `REPLICA IDENTITY` decides whether updates and deletes replicate. `NONE` blocks them.
- Add tables to an existing publication, then `ALTER SUBSCRIPTION ... REFRESH PUBLICATION`
  [372]. Prefer adding tables individually over `FOR ALL TABLES` [358].
- PG15 added row filters, column lists and publish-by-schema [241].

**Initial sync is the slow part** [372], [255], [296]
- To pre-load big tables: create the slot and export its snapshot, dump or `COPY` in
  parallel from that snapshot, then `CREATE SUBSCRIPTION ... copy_data = false,
  enabled = false` and enable it.
- PG17 `pg_createsubscriber` converts a physical standby into a logical subscriber [335], [331].

**What it does not replicate**
- DDL: apply schema changes manually and keep both sides in sync [370], [312]. DDL
  replication was not being worked on [412].
- Sequences: until PG19 you copy them yourself. PG19 adds a one-time sync (not
  continuous): `ALTER SUBSCRIPTION ... REFRESH SEQUENCES` [392], [423].

**Conflicts**
- A subscriber-side unique violation stalls the stream [390].
- PG15 added `disable_on_error`, `ALTER SUBSCRIPTION ... SKIP` and
  `pg_stat_subscription_stats` [241], [220]. The older `pg_replication_origin_advance`
  route is risky [218].
- Don't split related tables across publishers, because foreign-key ordering causes
  conflicts [360], [369]. `session_replication_role = replica` bypasses FKs and can
  leave orphans [296].

**Large transactions.** Streaming of in-progress transactions arrived in PG14. Parallel
apply came in PG16, with reported gains of 25–40% [284], [358]. PG13 added
`logical_decoding_work_mem` [204].

**Version highlights**
- **PG16:** logical decoding and replication from a standby [284]. Creating a slot on a
  standby can hang for hours waiting for a consistent point, and could not be cancelled
  [374]. Also origin filtering, which prevents bidirectional loops [284].
- **PG17:** failover slots, and logical slots survive `pg_upgrade` [335], [318].
- **PG19:** further failover-slot enhancements and visibility [431]. `wal_level` rises to
  `logical` automatically while a slot exists, forcing a checkpoint, so slot creation can
  block [417].

**Failover slots** [337] (the setup is fiddly)
- Create the slot or subscription with `failover = true`.
- On the standby set `sync_replication_slots = on`, use a physical slot via
  `primary_slot_name`, and enable `hot_standby_feedback`.
- On the primary set `synchronized_standby_slots` to the physical slot.
- Put a `dbname` in the standby's `primary_conninfo`.
- After promotion, disable the subscription, repoint its connection, then re-enable it.
- Before PG17: the `pg_failover_slots` extension [331], [262], or Patroni's slot
  handling [198].

**Slot hygiene** [374]: use `pgoutput`, cap slot size, emit heartbeats, enable
failover slots, consider `REPLICA IDENTITY FULL` for old TOAST values, and monitor.

## 8. HA and failover tooling

- Postgres does not fail over by itself. Promotion is a manual command [337].
- **Patroni:** per-node agent with a consensus store (etcd, Consul or ZooKeeper). It
  starts, stops and promotes nodes, tracks LSNs, manages slots, uses `pg_rewind` and
  `pg_basebackup` to re-add nodes, and fences. Put HAProxy, an F5 or an ELB in front
  [407], [198]. Use at least 3 nodes [396].
  - It is sensitive to network blips and etcd disk latency, which cause spurious
    failovers [190].
  - Multi-datacenter failover is a manual promotion [396], [393].
- **pg_auto_failover:** possibly the simplest, ideally with 3+ instances [191], [272].
- **CloudNativePG:** uses Kubernetes primitives instead of Patroni [337], [308].
  - Data durability `required` vs `preferred` (section 4) [348].
  - Cross-site replica clusters [323], [331] and logical-decoding slot sync [379].
- The host's rule: Patroni if you are not on Kubernetes, CloudNativePG if you are [407].
- Client discovery: multi-host strings, VIP manager or HAProxy [396]. Roles float, so
  size every replica for primary duty [196].

## 9. Split brain and fencing

- Use a quorum of at least 3 nodes to avoid false failovers and split brain [271], [396].
- A two-node Patroni setup "ensures" an eventual split brain [393].
- Fencing means isolating a misbehaving node [407]. Failback relies on the same
  principle: the old primary must take no writes after promotion [tutorial: easy failback].

## 10. Failback

**Easy failback** [tutorial: easy failback]
- Works if the old primary took **no writes** after the promotion.
- Stop the current primary, promote the node you want back, and restart the other as a
  standby. It follows the new timeline.
- Promotion renames the standby config (`recovery.done` in the PG10 tutorial). Rename it
  back to reuse it.

**`pg_rewind`** [tutorial: pg_rewind]
- Use it when the old primary took writes after the promotion. Without it, startup fails
  with "requested starting point ... not in the server's history".
- It needs `wal_log_hints` (or checksums) [270].
- It **discards** changes made on the target after the fork.
- Stop the target, then rewind it from the new primary over a connection.
- Afterwards recreate the standby config, because the sync overwrote it. Drop and create
  slots as in section 2.
- Patroni automates this [407]. A PG17.2-era release fixed a bug where `pg_rewind` could
  delete still-needed WAL [344].

## 11. Multi-master and active-active

- PG16 origin filtering enables bidirectional logical replication [284]. The show's
  verdict: so new you cannot say Postgres has multi-master [283].
- A blog post on pglogical bidirectional replication said "here be dragons". Slot
  positions are lost on failover, and last-update-wins corrupts counters and balances
  [201].
- Costs include conflict resolution and cross-region traffic. For write scale, shard
  rather than go multi-master [374], [248].

## How the advice moved

| Topic | Older | Current |
| --- | --- | --- |
| Retaining WAL | Large `wal_keep_segments` [tutorial: slots] | Slots, capped with `max_slot_wal_keep_size` [270] |
| Logical slots on failover | Lost; rebuild | Extension or Patroni [262], [198]; PG17 failover slots [337]; PG19 more [431] |
| Logical from a standby | Primary only | PG16 [284]; slot creation can hang [374] |
| Sequences | Manual copy | PG19 one-time refresh [392], [423] |
| Read-after-write | Pin to primary, sleep, `remote_apply` | PG19 `WAIT FOR LSN` [433] |
| Subscription errors | Stuck; origin advance | PG15 `disable_on_error`, `SKIP` [241], [218] |
| Large logical transactions | Wait for commit | Streamed (PG14), parallel apply (PG16) [358], [284] |
| Multi-master | Not a thing [248] | Origin filter in PG16, still experimental [283] |
| HA tooling | repmgr, EFM | Patroni, pg_auto_failover, CloudNativePG [407], [272] |
