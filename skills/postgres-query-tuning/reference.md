# Postgres query tuning

Distilled from the query-tuning episodes of *Scaling Postgres* (Creston Jamison,
roughly episodes 31-437), from auto-generated transcripts. `[NNN]` is an episode
number. Most numbers come from third-party blog posts discussed on the show, so
read them as "this happened on one workload", not as constants. Where advice
changed, the newest take wins. Index choice belongs to postgres-schema-design,
server-wide memory sizing to postgres-server-tuning, and finding the slow query to
postgres-monitoring.

## Checklist

- **Measure:** `EXPLAIN (ANALYZE, BUFFERS)` on production-sized data. Find the
  slowest node, multiply by loops, compare estimated vs actual rows.
- **Estimates 10x off:** `ANALYZE`, raise the column statistics target, then
  `CREATE STATISTICS`. Confirm the plan actually changed.
- **Index ignored:** check functions, type casts, low selectivity and stale
  statistics before touching cost settings.
- **`ORDER BY ... LIMIT` regression:** composite index (filter column, then sort
  column), or defeat the sort index with `col + 0`.
- **Prefer** `NOT EXISTS` to `NOT IN`, `= ANY(array)` to long `IN` lists,
  `LATERAL` for top-N-per-group, `UNION ALL` for stubborn `OR`.
- **Never** disable join types or `enable_*` globally; use them per session to
  diagnose.
- **SSD:** `random_page_cost` near 1.1 [305]. **JIT:** off for OLTP.
- **Prepared statements on partitioned tables:** consider
  `plan_cache_mode = force_custom_plan`.
- **Hints are the last resort**, after statistics, indexes and settings.

## 1. Reading EXPLAIN

**Always add BUFFERS** [276][197][226]
- Timings vary run to run and depend on cache. Buffer counts show work done, stay
  stable, and reveal spills and unexpected I/O [226].
- PG18 makes BUFFERS default with `EXPLAIN ANALYZE` [353].
- A huge `shared hit` under a nested loop is the same pages hit repeatedly, not
  more disk reads [284].
- Other options [410]: `WAL`, `SETTINGS` (non-default planner settings [062]),
  `VERBOSE` (per-worker detail [172]).

**SERIALIZE (PG17)** [312]
- Adds the cost of preparing results for the wire, including detoasting. One query
  took 750 ms but serialization brought the total to nearly 20 s.

**What to look at** [167][289][172]
- Find the slowest node. A row estimate off by 10x or more is a frequent cause of
  bad plans [167]; pgMustard flags a 10x gap [044].
- Node time = total minus children, times loops [172].
- Bottleneck signals [289]: high cost, wrong row counts, many buffers, filters
  discarding many rows, sequential scans on large tables, external disk merges,
  temp reads and writes.

**Observer effect**
- `EXPLAIN ANALYZE` timing cost about 50% on one query and 700% on a Xen EC2 VM;
  overhead scales with rows per node, not duration [116]. PG19's RDTSC clock cut
  4.2 s timed vs 2 s plain to roughly 20-25% overhead [413].
- `auto_explain` sampling everything raised latency 26%; logging only statements
  over 10 ms cost 0.8% [157].

**Getting the plan you actually run**
- Use production-sized data; small tables give different plans [110][263].
- `EXPLAIN (GENERIC_PLAN)` (PG16) works on `$1` queries from `pg_stat_statements`
  [259][262]. Before it: `PREPARE`, execute six times, or
  `plan_cache_mode = force_generic_plan` [259][233].
- First run slow, second fast, with many dirty buffers: index pages setting kill
  flags, and the planner's actual-value probe gave up after 100 heap pages, so the
  first plan used only the histogram [401].

## 2. Row estimates and statistics

**What the planner knows** [363]
- Per column: distinct values, width, null fraction, correlation, MCVs,
  histograms (`pg_stats`). Nothing about relationships between columns.
- Monotonic columns (serial, timestamp) go out of range fast; analyze often [304].
- Bloat skews estimates: after a massive delete the planner expected 200 rows and
  got 0, since page counts follow file size [397]. One table went from 14 s to
  37 ms after cleanup [039].

**Statistics target**
- Talk advice: leave the global default, raise it per column [358]. A tenfold raise
  on one column fixed a 256-row estimate against 1-3 real rows and read 10,000
  fewer buffers [364].
- Cost: more statistics mean longer planning [393]. PG19 hashes MCV lists in join
  estimation because the old loop was quadratic: 40% faster than PG18 at target
  1000, about 16x at 10,000, no gain at the default 100 [395].
- Sometimes no target worked; the fix was an index with the `ORDER BY` column
  last [392].

**`CREATE STATISTICS`** (`dependencies`, `ndistinct`, `mcv`)
- Use for correlated columns: city/zip, tenant and parent ids [153][213].
- Evidence:
  - `ndistinct` on three join columns turned a merge join into a hash join, 843 ms
    to 154 ms to faster still [324].
  - `dependencies` gave 20,000x on a three-id lookup [213].
  - Max per-column targets plus dependency stats turned a ~2 min plan (1 row
    estimated, 2000+ real) into 42 ms [128].
- `ndistinct` on `GROUP BY` columns cut a 240,000 estimate toward the real 60,000,
  but runtime did not change because the plan did not [350]. Expression statistics
  fix `date_trunc('day', ts)` estimates [247].
- Doubts: the host never used them to fix an OLTP problem [356]; one talk found
  gains at planning only [358]. Verify the plan changed [350].
- Analyze cost grows steeply with columns: 8-column `ndistinct` took 7.5 s, 38 s
  with mcv + dependencies [325].
- Extended stats are not carried over by upgrade; run
  `vacuumdb --analyze-only --missing-stats-only` [371][410].

**Statistics portability (PG18)**
- `pg_upgrade` and dump/restore keep planner statistics [385][371].
- `pg_restore_relation_stats` / `pg_restore_attribute_stats` copy production stats
  to a small test box [408][366]. `pg_set_relation_stats` fakes sizes until
  `ANALYZE` resets them [338].

## 3. Planner pitfalls and steering

**`ORDER BY ... LIMIT` picks the sort index** [383][315][336][392]
- The planner scans the sort-column index until it finds N rows and ignores the
  selective `WHERE` index; statistics can't predict the scan length [336]. It
  surfaces as regressions after upgrades [383]; one case went from sub-ms to
  10 minutes [315].
- Fixes, in order:
  1. Composite index (filter column, then sort column) [315][398][392].
  2. `ORDER BY id + 0`, `col || ''` or `ts + interval '0 s'`, which stops the
     sort index being used [383][398][392].
  3. A higher statistics target [315].
  4. A `MATERIALIZED` CTE that finds the few rows first [364].
- PG17 incremental sort turned one query from seconds into 10 minutes;
  `enable_incremental_sort = off` was the workaround [336]. Two `IN` lists plus
  `ORDER BY LIMIT` also fall back to the sort index [337].

**`OR`:** split into `UNION ALL` [398]. A `UNION` rewrite is not always
equivalent, e.g. `OR` across a join [241]. PG18 optimizes more `OR` conditions
[385].

**`IN` vs `ANY`**
- A long `IN` builds a node per item; `= ANY(array)` cut planning from 40 ms to
  2 ms, saved memory and favored a bitmap scan [418].
- PG17 searches several `IN` values per B-tree descent, ~10x in one example [336].

**`NOT IN` vs `NOT EXISTS`**
- `NOT IN` is an AND of `<>`, so one NULL returns no rows [422]. Postgres handles
  `EXISTS`, `NOT EXISTS` and `IN` well, `NOT IN` poorly [274].
- Inclusion forms (`IN`, `ANY`, `EXISTS`, join) get the same plan; exclusion forms
  differ [110].
- Scalar correlated subqueries become nested loops; uncorrelated ones and CTEs are
  rarely a problem [274].

**CTEs**
- Before PG12 a CTE was an optimization fence [031][052]. PG12 inlines by default
  with `MATERIALIZED` / `NOT MATERIALIZED` to choose [084].
- Use `MATERIALIZED` deliberately to force a small set first [364][287]. It fixed a
  bitmap-and at 100% CPU [202]. A `DELETE ... LIMIT` in a non-materialized CTE ran
  its limit 5 times [364].

**Views, functions, volatility**
- Views inline unless they use `DISTINCT ON`, window functions, set ops, some
  aggregates, a materialized CTE, volatile functions or complex subqueries [353].
- Volatility decides once-per-row vs once-per-query. Mark functions `IMMUTABLE` and
  `PARALLEL SAFE` where true [158][151]. Exception blocks cost 4-5x [151].

**Defeated indexes**
- A function in the `WHERE` needs a matching expression index with identical
  syntax; `||` vs `concat()` mismatched [398][352][037].
- Types:
  - A literal beyond `bigint` becomes `numeric` and bypasses the index; validate
    input or bind parameters [240].
  - Java `BigDecimal` binds or driver-cast doubles do the same; set the type or
    send unknown [208][089].
  - Mismatched join key types need a schema fix, not casts [398][352].
- Low selectivity picks a sequential scan: 18% of rows [423], 95% delivered orders
  [314], a common vs rare name [068]. Small tables too [314].

**Other steering**
- `join_collapse_limit = 1` forces written join order; planning ~5x faster [267].
  Use `SET LOCAL` in a read-only transaction [264].
- `enable_*` toggles diagnose only [378][287]. Disabling merge join was the wrong
  fix; statistics fixed it [324]. PG18 picks the path with the fewest disabled
  nodes, and `EXPLAIN` shows disabled [378].

## 4. Joins, memoize, LATERAL

**Join strategies** [120]
- Nested loop suits a small outer side, hash needs the build side in `work_mem`,
  merge suits two large sides; non-equality joins fall back to nested loop.
- Help the planner with statistics, `random_page_cost`, `effective_cache_size`,
  `effective_io_concurrency`, `work_mem`. `INCLUDE` columns allow index-only scans
  under nested and merge joins [120].
- A nested loop probing an index 13,000+ times signals a bad estimate [128].

**Memoize (PG14)** [249][191]
- Caches inner results of a nested loop with a parameterized index scan. On by
  default. A `LATERAL` benchmark went from 3.5 s to ~1 s; the correlated-subquery
  form didn't benefit [191].

**LATERAL is a per-row nested loop** [174][279]
- Payments per gift: ~10x vs a grouped subquery with hash join [208].
- Latest row per user: `DISTINCT ON` slowest (~10x at one user, worse at scale);
  lateral fastest at 100,000 users, then correlated subquery [430].
- Last value per sensor: lateral 5 ms vs recursive CTE 2.5 ms, though the host saw
  lateral 2x faster on cached PG16.2 [311].

## 5. Parallel query

- Knobs: `max_parallel_workers_per_gather`, `max_parallel_workers`,
  `max_worker_processes`; `work_mem` applies per worker [176][353].
- Pays when a scan reads a lot and returns few rows; returned rows pay
  worker-to-leader transfer [037].
- `min_parallel_table_scan_size` ignores TOAST: a 45 MB heap needed it lowered to
  4 MB to get 3 workers [305].
- `ORDER BY` can block parallelism; per-group distinct counts depend on cost [429].
- Diagnose [353][300]: planned vs launched workers in `EXPLAIN ANALYZE`,
  `backend_type` in `pg_stat_activity`, an exhausted worker pool.
- JDBC protocol-level row limits killed parallel plans; set it to 0 and use
  `LIMIT` [300].

## 6. JIT

- For analytics and expression-heavy queries, not OLTP [045]. PG12 enabled it by
  default; raise `jit_*_cost` so small data skips it [092][066].
- A many-join Django query spent 99.7% of runtime compiling (3 s vs ms to execute);
  fix by `SET jit = off` per session or role [290]. Many turn it off for
  predictability [308][361].
- PG19 turns JIT off by default [433][428].

## 7. Prepared statements and plan cache

- Behavior [411][246][068]: 5 custom plans, then the planner compares their average
  cost with the generic plan's and keeps generic for the session if cheaper or
  equal. With skew, a locked-in generic plan can choose an index where a scan wins
  [411].
- `plan_cache_mode` (PG12): `auto`, `force_generic_plan`, `force_custom_plan`
  [068][084].
- Partitioned tables [391]:
  - 12 partitions with 3 indexes each: custom plans took 8 locks.
  - The 6th run locks every relation; later generic runs lock 13 and can't prune at
    plan time.
  - `force_custom_plan` keeps 8, trading away the planning savings that avoid lock
    manager contention [391][246].
- `force_custom_plan` fixed one team's partitioned-table memory problem [267];
  another chose generic plans because planning cost outweighed slower execution
  [320].
- Prepared statements cut planning of a 14-way self-join from ~170 ms to a fraction
  of a millisecond [288].

## 8. Memory, spills, cost settings

**`work_mem`**
- `external merge` means too little memory or a missing index; check the index
  first and don't oversize [187]. Each parallel worker gets its own [176].
- GIN lossy heap blocks: raise `work_mem` to make them exact [131].
- Per query: `SET LOCAL`. Per role: `ALTER ROLE ... SET` [284]. Per function:
  `ALTER FUNCTION ... SET` for `work_mem` or `enable_*` [097].
- The transcripts have nothing on `hash_mem_multiplier`.

**`random_page_cost`**
- Default 4 vs sequential 1 assumes spinning disks; use ~1.1 on SSD and leave
  `seq_page_cost` at 1. One change took an 11 s sequential scan to a 1.5 ms
  index-only scan [305]. Another test used 2.2; test your own load [374].
- Data shifts flip plans: `random_page_cost` 4 plus new data produced a sequential
  scan [408].

**Async I/O (PG18)** [365][385]
- Speeds sequential scans, bitmap heap scans and vacuum, not index scans.
  `io_method`: `sync` equals PG17, `worker` is default (3 workers), `io_uring`.
- On EBS a count went from 15 s to ~10 s with workers and just over 5 s with
  io_uring [365]. PG19 adds an `IO` option to `EXPLAIN` [414].

## 9. Plan stability and hints

- No inline hints; the levers are session settings and `pg_hint_plan` [414][352].
- Against hints [414]: today's hint is tomorrow's regression, they excuse skipping
  the statistics fix, they don't scale with data. The argument shifted to
  stabilizing plans across upgrades [414].
- `pg_hint_plan`: fix cost settings, `effective_cache_size`, `work_mem` and
  statistics first [352].
- PG19 `pg_plan_advice` [407][437]:
  - `EXPLAIN` emits an advice string; replay or edit it via
    `pg_plan_advice.advice` (e.g. force hash or merge join).
  - `pg_stash_advice` binds advice to a query id in configuration, like Oracle SQL
    plan management or SQL Server Query Store.
  - Aurora/RDS already pin plans [407][408].
- **Plan flips** [408]: `ANALYZE` can swap a good plan for a worse one (a Clerk
  outage). Check timeouts, autovacuum cadence and statistics target.

## 10. Version improvements

- **PG12:** CTE inlining, `plan_cache_mode`, JIT default, MCV extended stats
  [084][066].
- **PG14:** memoize [191], expression statistics [248].
- **PG16:** `GENERIC_PLAN` [259]; right anti-join, parallel hash full/right joins,
  trivial `DISTINCT` short-circuit (1250x) [303].
- **PG17:** `IN` on B-tree [336], `SERIALIZE` [312], `NOT NULL` simplification
  [339]. `OR`-to-`ANY` missed it [318].
- **PG18:** skip scan, best with a low-cardinality leading column [403]; `OR`
  optimization [385]; async I/O; stats survive upgrade; BUFFERS default.
- **PG19:** hashed MCV join estimation [395]; `count(1)` and `count(not_null_col)`
  become `count(*)`, up to 64% [394]; aggregation below small joins, ~5x [394];
  RDTSC timing [413]; JIT off; `pg_plan_advice`.

## How the advice moved

| Topic | Older | Current |
| --- | --- | --- |
| CTEs | optimization fence [052][031] | inlined since PG12; `MATERIALIZED` to steer [084] |
| JIT | new feature to enable [045][032] | default on in PG12 [092]; often disabled [361]; off by default in PG19 [433] |
| Hints | "no planner hints dogma" [108], `pg_hint_plan` [264] | `pg_plan_advice` for stability, statistics first [407][437] |
| `random_page_cost` | 4 default | ~1.1 on SSD [305]; test your value [374] |
| Extended stats | 3000x win [128] | rarely changes OLTP plans [356]; not carried over by upgrade [371] |
| `ORDER BY LIMIT` | raise statistics target [315] | composite index or `+ 0`; recurs in PG17 [383][336] |
| BUFFERS | recommended option [197] | default in PG18 [353] |
| `enable_*` | adds huge cost to a path | PG18 counts disabled nodes [378] |
| Prepared statements | save planning [288] | force custom plans on partitions [391] |
