# Postgres monitoring and observability

Distilled from *Scaling Postgres* (Creston Jamison, 2018–2026), from
auto-generated transcripts. `[NNN]` is an episode number. Most numbers come from
third-party blog posts discussed on the show, so read them as "this happened on
one workload", not as constants. Where advice changed over the years, the newest
take wins. Fixing a slow query once found is out of scope (see
postgres-query-tuning), as is vacuum tuning (see postgres-autovacuum).

## Checklist

- **Query stats:** run `pg_stat_statements` everywhere, with `track_planning` on.
- **Current state:** `pg_stat_activity` for what is running now, wait events for
  why it is slow. Watch idle-in-transaction sessions.
- **Logs:** set `log_min_duration_statement`, `log_lock_waits`,
  `log_autovacuum_min_duration`, `log_temp_files`, `log_checkpoints`, and a
  useful `log_line_prefix`.
- **Plans:** `auto_explain` with a duration threshold, never on every query.
- **Alert on:** XID and multixact age, replication lag and inactive slots, disk
  and WAL growth, sequence/int exhaustion, long transactions, checkpoint health.
- **History:** the cumulative views have no memory. Snapshot them or use a tool
  that does.
- **Cost:** every detailed probe has an observer effect. Measure before enabling
  timing or per-query plans on a hot system.

## 1. pg_stat_statements: setup and blind spots

**Setup** [153]
- Add it to `shared_preload_libraries`, then `CREATE EXTENSION` in each database
  you want it in.
- `track_io_timing` adds read/write time but can be a performance burden; run
  `pg_test_timing` first.

**What it cannot see**
- By default it keeps 5,000 statements, stores query identifiers not literals,
  can lose query text if the text file grows large, and is per node, so collect
  from every node [420].
- Failed queries and anything still running [211]. A statement appears only once
  it completes, so use `pg_stat_activity` for in-flight work [212].
- Rolled-back transactions [265].
- Parameter values, so it can't show that one value is fast and another slow
  [212].
- Delivery of rows to the client. `log_min_duration_statement` includes it [179].
- Example: a sometimes-slow query looked fast in pg_stat_statements because
  clients were queuing for a server connection in PgBouncer. The PgBouncer stats
  showed the waits [332].

**Sort order by goal** [265]
- Total time to reduce CPU, mean time for the slowest statements, buffers to
  reduce I/O.
- Also look at call rate. A heavy query running 500 times a second may not need
  to, and pg_stat_statements alone makes that hard to see [367].

**Reading a normalized query**
- You can't `EXPLAIN` the `$1` form directly. `PREPARE` it and set
  `plan_cache_mode = force_generic_plan` (PG12+) [233].

## 2. Normalization pitfalls and IN lists

- Before PG18, each different-length `IN (...)` list was its own entry; PG18
  normalizes them to one [420].
- High-cardinality workloads are often ORM-generated: one example went from 671
  entries to 120 on PG18, and PG19 goes further by storing an array. Raise the
  entry limit if you still need to [430].
- Pre-PG18 mitigation: `= ANY($1)` gives a single entry [273].
- A distinct `application_name` per client or job makes the source visible in
  activity and logs [150].
- PG14 made the query ID visible in `pg_stat_activity` (set via config), the log
  and `EXPLAIN`, so live activity joins to pg_stat_statements [166][194].

## 3. Overhead, track_planning, resetting

**Overhead is real mainly for hot identical statements** [304][324]
- Quoted figures: 7%, under 10%, 1% [304]; "generally recognized" about 5% [324].
- The bottleneck is lock contention on one entry when the same statement runs
  extremely often [304]. More varied queries mean less overhead [304].
- On a 360-vCPU instance, dropping pg_stat_statements took a test from 1M to
  2.5M TPS, and prepared statements pushed it to 3M [324]. Sampling above a
  call-rate threshold was floated as a fix [304].

**`track_planning`** [345]
- Off by default, so planning time is invisible. Planning grows with index
  count, joins and partitions.
- Conclusion: turn it on. Slowdown reports come mostly from synthetic benchmarks
  that repeat one query, and you'd have to push Postgres to its limits to see it
  in production.
- It arrived in PG13 [109].

**Resetting and history**
- Reset with `pg_stat_statements_reset()` to compare before and after a change
  [265], or periodically (monthly, quarterly) for a fresh base [50].
- PG14 added `pg_stat_statements_info` with a `stats_reset` timestamp [146].
- To see what a deploy changed, copy the view somewhere on a schedule, for
  example hourly [212].
- Cheap DIY: a timestamped snapshot table, made `UNLOGGED`, skipping unchanged
  rows [126].

## 4. pg_stat_activity and wait events

- Rows include non-client backends: checkpointer, autovacuum, archiver, WAL
  writer [223].
- `wait_event_type` and `wait_event` show what a query waits on [93]. Sample them
  every second or five seconds rather than eyeballing once [69].
- Look at wait states per query: 20 sessions waiting on CPU or disk at once is
  the kind of finding that changes what you fix [367].
- Idle-in-transaction: Postgres is fine, the client holds a lock while doing
  other work [357]. It also blocks vacuum and risks TXID wraparound [212].

**What common waits mean**
- **`LWLock:LockManager`:** lock-manager contention, seen with many joins plus
  many indexes, or many partitions [366].
- **SubtransControl:** the sub-transaction array overflowed, from savepoints or
  PL/pgSQL `EXCEPTION` blocks, visible under concurrent sessions [105][181].
- **MultiXact offset SLRU:** a replication-deadlock bug in recent 15.x and 16.x
  minor releases hung a replica's startup process on this wait; PG17 and above
  were not affected [425].
- **IO, ClientRead, Lock:** the show gives no per-event guide. Use the wait type
  to separate CPU, disk, locks and client behaviour.
- A hung query can show `active` when it is waiting. Patches to make it
  cancellable and to add a wait event were accepted [374].
- **PG18 async I/O:** workers do the reads, the backend waits for AIO completion,
  wait events change, and `pg_aios` shows requests in flight. `EXPLAIN ANALYZE`
  timings get harder to interpret [365].

## 5. Locks, blocking and deadlocks

- `log_lock_waits` logs waits longer than `deadlock_timeout`, generally 1 second
  [223]; tune `deadlock_timeout` if needed [212].
- Inspect `pg_locks` [392]. Alert on backends waiting on locks [115] and on lock
  and deadlock counts [229].
- Also alert on orphaned prepared transactions and idle transactions [122].

## 6. I/O, WAL, and newer stats views

- **`pg_stat_io` (PG16):** cumulative reads, writes, extends, evictions, reuses,
  fsyncs by backend type and context [253]. PG18 improved it, can export
  statistics, and added columns to pg_stat_statements [386].
- Per-backend I/O stats exist; the next ask is a plan hash to detect plan changes
  [349].
- **WAL:** PG13 tracks WAL records, full-page images and bytes per statement and
  in `EXPLAIN` [133][109]. PG14's `pg_stat_wal` is cumulative, so reset or diff it
  [166]. PG14 added I/O timings to autovacuum logging [194].
- **Progress views:** index builds [198], `COPY` [170][301], cluster [84]. A
  failed `CREATE INDEX CONCURRENTLY` leaves an invalid index, so check the job's
  log [336].
- **PG19:** `pg_stat_lock` and `pg_plan_advice` are new [421]; nested-query
  tracking in pg_stat_statements was dropped from the release [435].

## 7. auto_explain and EXPLAIN overhead

**Use it for plans you can't reproduce** [252][157]
- The host doesn't enable it by default because of load; he turns it on when
  queries vary between runs [252].
- Measured with pgbench: `log_analyze` about 0.3% slower, adding `log_timing`
  about 2.2%. Folk numbers ran from about 3% to a factor of two. Only log the
  slowest queries [157].
- Wins: it found an integer-to-double cast from a JDBC layer defeating an index
  [89], and a sequential scan growing as a deferred-trigger table grew [238].
- It can replace a slow-query log, since it prints statement and plan together
  [370]. Not every cloud provider exposes all its parameters [326].

**Plain `EXPLAIN ANALYZE` has an observer effect**
- One example ran about 50% slower than the bare query [116].

## 8. Logging settings

**Baseline** [276][99]
- `log_min_duration_statement`, `log_checkpoints`, `log_autovacuum_min_duration`,
  `log_temp_files`, `log_lock_waits`, connection logging, log rotation.
- Threshold: the host logs slow queries at about 1 second [252]; others suggest
  5 seconds [63][212].
- PG13 added statement sampling and parameter logging for failed statements
  [133]; a 25% sample was an example [150].
- `log_line_prefix` should include application, user, database and host [150].
- PG15 JSON logs: bigger, but easier for tools [199].
- Disk can fill with logs, but old log files are safe to delete, unlike WAL [326].
- `pgaudit` limited to DDL plus `auto_explain` round it out [380].
- Analyze logs with pgBadger [263] or `pglog` [183].

**Checkpoints** [423]
- Enable `log_checkpoints`, alarm on how long checkpoints take, and on
  checkpoints that are too frequent (raise `max_wal_size`).
- A database with no successful checkpoint for a week would not restart, a
  40-hour outage.

## 9. Key health metrics and alerts

The show never gives universal percentages, so set thresholds by trend and
runway.

**Core list**
- TPS/QPS, latency, connections, longest transactions, commits vs rollbacks,
  transactions left to wraparound, replication lag, WAL archive lag, WAL
  generation rate, locks and deadlocks, top queries by total time [229].
- Alerts: database up, out of disk (including excess logging), vacuum and
  wraparound, replication lag, sequence exhaustion [136].
- Also inactive replication slots and backend counts [115], oldest vacuum and
  deadlocks [117], `hot_standby_feedback` and temp storage [122].
- If you only watch one machine metric, the host picks CPU, split into I/O wait
  vs system vs user, then goes to pg_stat_statements [294].
- RED: rate is the delta of summed `calls`, errors are rollbacks in
  `pg_stat_database`, duration is summed total time [138].

**Transaction IDs and multixacts**
- Limit is about 2 billion [366]. Report percent toward wraparound and percent
  toward emergency autovacuum [161].
- `autovacuum_freeze_max_age` defaults to 200M. The host allows raising it for
  busy systems, but not past 1B [161]. Earlier he leaned toward defaults, noting
  some go to 1.5B only with close monitoring [50]. Mailchimp had an outage from a
  write spike causing wraparound [50].
- Find holders via backend xmin age, prepared transactions and replication slots
  [101].
- Also track multixact age; many tools skip it, and it matters with `FOR SHARE`
  and foreign keys. It freezes at a different age than XIDs [418][376].
- Monitor int4 key and sequence usage, and the foreign keys too [400][366].

**Other**
- Cache hit ratio: keep it around 99% [66]; warehouses run lower [280].
- Vacuum: dead rows, size, last vacuum, lock conflicts, long transactions [7];
  Cybertec's queries cover bloat, analyze status, visibility map [415].
- `work_mem`: keep it modest and watch pg_stat_statements for queries that start
  spilling, then raise it selectively [263].

## 10. Replication, slots, WAL and disk

- `pg_stat_replication` on the primary (or upstream in cascading setups) knows
  only its direct replicas [303]. On the standby, `pg_stat_wal_receiver` is
  harder to read [282]. Expect one row per replica and an active slot for each
  [10].
- Logical replication: watch `pg_replication_slots` as well [372].
- Inactive or forgotten slots make the primary fill with WAL [115][129]. A
  logical consumer such as Debezium must keep consuming [142].
- `max_slot_wal_keep_size` (PG13) deactivates a slot at a WAL cap, trading the
  replica for primary uptime [125].
- WAL disk-full causes: an orphan slot, a mass update or delete, or no alerting.
  Never delete WAL by hand [161].
- The host watches disk space rather than WAL file counts [43].
- Single-threaded WAL sender/receiver can be the replication ceiling [366].

## 11. Tooling

- **DIY:** snapshots of pg_stat_statements and activity [126]; `pg_extras` for
  cache hit, unused indexes, locks and bloat [135].
- **Prometheus:** `postgres_exporter`, `node_exporter`, Alertmanager and Grafana
  [46]; `pgscv` covers Postgres and PgBouncer [168]; PgBouncer and Pgpool
  exporters exist [143].
- **pgwatch:** connects remotely, so it works on managed databases [204].
  Postgres AI monitoring builds on pgwatch3 with Prometheus, but keeps
  slow-query statements in Postgres [377].
- **pgmonitor (Crunchy):** an extension refreshes metrics into materialized views
  via a background worker [331].
- **pg_now (Redgate, free):** two snapshots show activity between them [358].
- **pg_stat_monitor (Percona):** buckets the statements; default 10 buckets of
  60 seconds, so ship data off-box [214]. It offers P99s, overhead unknown [371].
  Otherwise approximate P99 from mean and stddev, or use an APM [369].
- **Waits:** `pg_wait_sampling` has profile and history views; Google Cloud
  supports it, RDS and Azure don't (AWS has Performance Insights) [326].
  `pg_wait_tracer` reads backend memory with no patches, extensions or restarts
  [415]. `pg_flight_recorder` is pure SQL, sampling about once a second into a
  local table [406].
- **Others:** `system_stats` exposes CPU, memory, disk and network via SQL
  [124][120]; `pg_qualstats` stores predicates that pg_stat_statements lacks [74];
  `pg_show_plans` can be enabled or disabled dynamically [109]; Coroot's
  open-source agent combines statements, activity and logs [211].
- **AI:** advisor only. The host would still want an alert rather than
  auto-growing storage [393].

## How the advice moved

| Topic | Older | Current |
| --- | --- | --- |
| pg_stat_statements | Reset periodically [50] | Plus `track_planning` on [345], `stats_reset` in PG14 [146] |
| IN lists | `= ANY` avoids entry bloat [273] | PG18 normalizes; PG19 stores arrays [420][430] |
| Overhead | 1–7% quoted [304] | Hot-statement contention [324]; planning cost judged small [345] |
| History | Hand-made snapshot tables [126] | pg_stat_monitor, pg_now, hosted tools [214][358] |
| Wait events | Sample pg_stat_activity [69] | pg_wait_sampling, pg_wait_tracer, flight recorder [326][415][406] |
| I/O visibility | `track_io_timing` with caution [153] | `pg_stat_io` (PG16), richer in PG18 [253][386] |
| Slow log | Threshold 5s [63] | About 1s [252], sampling [133], `auto_explain` instead [370] |
| Multixact | Rarely monitored | Monitor age with XID age [418] |
