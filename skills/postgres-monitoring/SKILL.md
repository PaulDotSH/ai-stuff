---
name: postgres-monitoring
description: >
  Set up and use PostgreSQL monitoring: pg_stat_statements, pg_stat_activity
  and wait events, lock and blocking analysis, I/O and WAL statistics,
  auto_explain, logging settings, and the health metrics worth alerting on
  (XID age, replication lag, slots, disk and WAL growth, long transactions,
  sequence exhaustion). Use when finding which queries are slow, figuring
  out why the database is slow right now, choosing what to alert on,
  configuring Postgres logging, or picking observability tooling.
---

# Postgres monitoring and observability

A playbook distilled from *Scaling Postgres*. Evidence and history are in
[reference.md](reference.md).

Once a slow query is found, fix it with `postgres-query-tuning`. For vacuum
findings, use `postgres-autovacuum`.

## Baseline setup

```conf
shared_preload_libraries = 'pg_stat_statements,auto_explain'
pg_stat_statements.track_planning = on   # planning time can dominate (partitions, many indexes)
track_io_timing = on                     # measure overhead first on old/virtualized hosts
log_min_duration_statement = '500ms'     # pick a threshold your log volume tolerates
log_lock_waits = on
log_autovacuum_min_duration = '1s'
log_temp_files = 0
log_checkpoints = on
log_line_prefix = '%m [%p] %q%u@%d app=%a '
auto_explain.log_min_duration = '2s'     # never 0 on a busy system
auto_explain.log_analyze = off           # timing every node has real overhead
```

## Answering "what is slow?"

1. **Top queries by total time** (not mean). The things to fix are often fast
   queries that run millions of times.

   ```sql
   SELECT calls, round(total_exec_time) AS total_ms, round(mean_exec_time,2) AS mean_ms,
          round(total_plan_time) AS plan_ms, rows, left(query, 80)
   FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;
   ```

2. **Know its blind spots**
   - Time spent waiting in a pooler queue never shows up.
   - Long `IN (...)` lists may split into many entries; prefer `= ANY($1)`.
   - The view is cumulative. Snapshot it, or reset it deliberately, to see
     trends.

## Answering "why is it slow right now?"

```sql
-- what is running and what it waits on
SELECT pid, state, wait_event_type, wait_event, now() - query_start AS runtime, left(query, 80)
FROM pg_stat_activity WHERE state <> 'idle' ORDER BY query_start;
-- who blocks whom
SELECT pid, pg_blocking_pids(pid) AS blocked_by, left(query, 80)
FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;
```

What common waits suggest:

- **`Lock`:** blocking chains, often DDL stuck in the lock queue.
- **`LWLock:LockManager`:** too many relations per query (partitions or
  indexes) at high QPS.
- **`IO`:** storage latency or a working set bigger than memory.
- **`ClientRead`:** the app is holding a transaction open.

## Alert on

- XID age and multixact age, per database and per table.
- Replication lag, and **inactive replication slots** (they pin WAL until the
  disk fills).
- Disk free space and `pg_wal` size or growth, plus archive failures
  (`pg_stat_archiver`).
- Transactions open, or idle in transaction, longer than N minutes.
- Integer/sequence headroom on `int` keys.
- Connection count against `max_connections`, and pooler client waits.
- Checkpoints requested vs timed (too many requested means `max_wal_size` is
  too small).

## Rules

- The cumulative stats views have no history. Use a tool that stores
  snapshots, such as pganalyze, pgwatch, or a Prometheus exporter, or snapshot
  them yourself.
- Every probe has an observer effect. Measure the cost before turning on
  timing-heavy options (`track_io_timing` on slow clocks, `auto_explain`
  analyze/buffers on hot paths, frequent sampling).
- Prefer the newer views when available: `pg_stat_io` (PG16) and
  `pg_stat_checkpointer` (PG17).
