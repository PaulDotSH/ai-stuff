---
name: postgres-query-tuning
description: >
  Diagnose and fix a slow PostgreSQL query: read EXPLAIN (ANALYZE, BUFFERS),
  find bad row estimates and fix them with ANALYZE, statistics targets and
  CREATE STATISTICS, avoid planner pitfalls (ORDER BY ... LIMIT picking the
  wrong index, OR vs UNION/ANY, NOT IN, CTE materialization, casts that
  defeat indexes), choose join and LATERAL rewrites, and handle parallel
  query, JIT, prepared-statement generic plans, work_mem spills and cost
  settings. Use when a specific Postgres query is slow or its plan changed.
---

# Postgres query tuning

A playbook distilled from *Scaling Postgres*. Evidence and version notes are in
[reference.md](reference.md). Related skills:

- To find which queries are slow, use `postgres-monitoring`.
- For what indexes and tables should exist, use `postgres-schema-design`.
- For instance-wide memory, use `postgres-server-tuning`.

## Workflow

1. **Reproduce with real data and parameters.** Plans on a tiny dev database
   are meaningless.

   ```sql
   EXPLAIN (ANALYZE, BUFFERS, SETTINGS) <query>;          -- add SERIALIZE (PG17+) if output is large
   ```

   For a prepared statement, also test the generic plan:
   `SET plan_cache_mode = force_generic_plan;`.
2. **Find the expensive node.** Use inclusive time × `loops`, and the
   buffers read. Most problems sit in one node.
3. **Compare estimated and actual rows at that node.** If they're off by 10x
   or more, fix the estimates before anything else:
   - Run `ANALYZE` on the table, and on the partitioned parent.
   - Raise the column's statistics target:
     `ALTER TABLE t ALTER COLUMN c SET STATISTICS 1000; ANALYZE t;`.
   - For correlated columns, use
     `CREATE STATISTICS s (dependencies, ndistinct, mcv) ON a, b FROM t;`.
   - For expressions such as `lower(x)` or JSONB keys, create statistics on
     the expression (PG14+), or add an expression index.
4. **If an index is ignored, check why before changing costs:**
   - a function or cast on the column
   - a parameter type that doesn't match the column type
   - low selectivity
   - stale statistics
   - a missing composite index
5. **Rewrite or add the right index,** then re-run step 1 and confirm the plan
   actually changed.

## Pitfalls and fixes

| Symptom | Fix |
| --- | --- |
| `ORDER BY x LIMIT n` walks an index on `x` and filters millions of rows | Composite index `(filter_col, x)`; or stop the sort index being chosen with `ORDER BY x + 0` |
| `a = 1 OR b = 2` gives a seq scan | Separate indexes plus a BitmapOr, or `UNION ALL` of two indexed queries |
| `(a > x OR (a = x AND b > y))` keyset filter is slow | Row comparison: `(a, b) > (x, y)` |
| `NOT IN (subquery)` is slow or wrong with NULLs | `NOT EXISTS` |
| Huge `IN (...)` lists | `= ANY($1::bigint[])` |
| Top-N per group | `LATERAL (... ORDER BY ... LIMIT n)` against a composite index |
| CTE referenced once is slow | Postgres 12+ inlines it; `MATERIALIZED` / `NOT MATERIALIZED` controls it explicitly |
| Sort or hash spills to disk (`external merge`, temp files) | Raise `work_mem` for that session/role, not globally |
| Fast first runs, slow after 5 executions | Generic plan chosen: `plan_cache_mode = force_custom_plan` for that path |
| Short OLTP queries pay JIT compile time | `jit = off`, or raise `jit_above_cost` |
| Planning time > execution time | Fewer partitions/indexes touched, prepared statements, or pruning that works (stable functions, the key in the WHERE) |

## Settings

- On SSD or NVMe, set `random_page_cost` around 1.1. Set
  `effective_cache_size` to the RAM actually available for caching.
- Use `enable_*` switches only per session, to diagnose. Never disable a join
  type globally.
- Parallel query helps big scans and aggregates. Check that workers were
  actually launched (`Workers Launched`), and look at
  `max_parallel_workers_per_gather`, plus table size thresholds such as
  `min_parallel_table_scan_size`.
- Hints (`pg_hint_plan`) and plan pinning are the last resort, after
  statistics, indexes and rewrites. Revisit them after every major upgrade.
- After a major upgrade, re-run `ANALYZE` (PG18's `pg_upgrade` keeps
  statistics; earlier versions don't), and recheck key plans.
