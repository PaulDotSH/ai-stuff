---
name: postgres-autovacuum
description: >
  Diagnose and tune PostgreSQL vacuum and autovacuum: dead tuples that won't
  go away, table and index bloat, autovacuum that can't keep up, per-table
  settings, insert-only tables, transaction ID and multixact wraparound
  warnings, long transactions or replication slots holding back cleanup, and
  online bloat removal (REINDEX CONCURRENTLY, pg_repack, pg_squeeze, REPACK).
  Use when a Postgres table or index keeps growing, vacuum is slow or
  blocked, XID age alerts fire, or you are setting autovacuum parameters.
---

# Postgres autovacuum, bloat, and wraparound

A playbook distilled from *Scaling Postgres*. Evidence, numbers and version
history are in [reference.md](reference.md). To design tables that bloat less
in the first place, use `postgres-schema-design`.

## Diagnose in this order

1. **Is something holding the xmin horizon?** If it is, no tuning helps. A
   VACUUM VERBOSE line like "dead but not yet removable" means exactly this.

   ```sql
   -- oldest open transactions
   SELECT pid, state, now() - xact_start AS age, query
   FROM pg_stat_activity WHERE xact_start IS NOT NULL ORDER BY xact_start LIMIT 10;
   -- replication slots (inactive ones pin xmin and WAL)
   SELECT slot_name, active, xmin, catalog_xmin FROM pg_replication_slots;
   -- forgotten two-phase transactions
   SELECT gid, prepared FROM pg_prepared_xacts;
   ```

   Also check standbys running with `hot_standby_feedback = on` and long
   queries.
2. **Is autovacuum reaching the table?** Compare `n_dead_tup`,
   `last_autovacuum`, and `autovacuum_count` in `pg_stat_user_tables`. Watch
   running work in `pg_stat_progress_vacuum`.
3. **Is it keeping up once it runs?** The usual bottleneck is the shared cost
   budget, not the worker count. Raise `autovacuum_vacuum_cost_limit` first.
4. **How close is wraparound?**

   ```sql
   SELECT datname, age(datfrozenxid), mxid_age(datminmxid) FROM pg_database ORDER BY 2 DESC;
   SELECT c.oid::regclass, age(c.relfrozenxid)
   FROM pg_class c WHERE c.relkind IN ('r','m','t') ORDER BY 2 DESC LIMIT 20;
   ```

5. **How much bloat is there really?** Estimates are rough. Use `pgstattuple`
   for tables and `pgstatindex` (`avg_leaf_density`) for indexes before acting.

## Tuning rules

- **Throttle**
  - Raise `autovacuum_vacuum_cost_limit` (into the thousands on fast storage)
    before adding workers. Workers share the budget.
  - Never set the cost delay to 0. That caused WAL and replica-lag storms.
- **Triggers**
  - On big or hot tables, lower `autovacuum_vacuum_scale_factor` from 20% to
    about 1–2%, per table with `ALTER TABLE ... SET (...)`.
  - Do the same for `autovacuum_analyze_scale_factor`.
- **Insert-only tables (PG13+)**
  - Rely on, or tune, `autovacuum_vacuum_insert_threshold` /
    `insert_scale_factor`. They keep the visibility map fresh for index-only
    scans, and freeze tables early.
- **Memory**
  - Give vacuum enough `maintenance_work_mem` / `autovacuum_work_mem` so it
    needs fewer index passes. PG17 removed the old 1 GB effective cap.
- **Partitioned parents are never auto-analyzed.** Run `ANALYZE parent`
  yourself after loads and after upgrades.
- **Timeouts that protect vacuum**
  - `idle_in_transaction_session_timeout`, and `transaction_timeout` (PG17).
  - `max_slot_wal_keep_size` for slots.

## Removing bloat

| Target | Tool | Notes |
| --- | --- | --- |
| Index | `REINDEX CONCURRENTLY` | Default fix. Vacuum never shrinks indexes. |
| Table, online | `pg_repack`, `pg_squeeze` | Needs about 2x space, takes brief exclusive locks, try on small tables first. |
| Table, online (PG19) | `REPACK ... CONCURRENTLY` | One final lock attempt, one at a time, the slot holds back vacuum. |
| Table, whole cluster | Logical-replication cutover | Rebuilds everything compactly. |
| Table, offline | `VACUUM FULL` / `CLUSTER` | ACCESS EXCLUSIVE for the whole run. Only in a maintenance window. |

## Wraparound emergencies

- Anti-wraparound autovacuum doesn't yield to other work, so it can block DDL.
  Let it finish; don't keep killing it.
- When age climbs anyway, the cause is almost always a horizon holder (step 1)
  or a vacuum that can't finish on one giant table.
  - Fix the holder first.
  - Then run a manual `VACUUM (FREEZE, VERBOSE)` on the oldest tables,
    starting with the oldest `relfrozenxid`.
- Monitor multixact age too. Row locks and FK checks on hot parent rows
  consume multixacts.

## Replicas

- Vacuum's file-truncation step takes ACCESS EXCLUSIVE, which replays on
  standbys and cancels queries there. Consider `vacuum_truncate = off` on
  affected tables.
- `hot_standby_feedback = on` stops those cancellations but moves bloat back
  onto the primary.
