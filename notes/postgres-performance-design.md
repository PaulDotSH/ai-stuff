# Postgres schema design for performance

Distilled from 439 episodes of *Scaling Postgres* (Creston Jamison, 2018–2026),
from auto-generated transcripts. `[NNN]` is an episode number; the key ones are
linked under [Sources](#sources). Most numbers come from third-party blog posts
discussed on the show, so read them as "this happened on one workload", not as
constants. Where advice changed over the years, the newest take wins.

## Checklist

- **Keys:** `bigint` identity or UUIDv7 for primary keys. Never `int`, never
  random UUIDv4.
- **Types:** `text` (plus a CHECK if you need a limit) over `varchar(n)`.
  `timestamptz` over `timestamp`. Integer cents or `numeric` for money, never
  float.
- **Shape:** normalize by default. Put fields you filter, join, or constrain on
  in real columns, and keep JSONB for sparse extras.
- **MVCC:** Postgres hates update-heavy rows. Design hot data as insert-only, or
  split frequently-updated columns into a narrow side table.
- **Indexes:** composite indexes with equality columns first and range/sort
  columns last. Use partial indexes for skewed or low-cardinality filters. Each
  extra index taxes every write.
- **Foreign keys:** index the referencing column by default.
- **Partitioning:** partition for retention and maintenance, not for speed. Keep
  the count in the hundreds, not thousands, and make every query filter on the
  key.
- **Deletes:** the only scalable bulk delete is dropping a partition.
- **Pagination and counts:** keyset pagination, never deep `OFFSET`. Avoid exact
  `count(*)` on big sets.
- **Analytics:** pre-aggregate incrementally into rollup tables. Don't run heavy
  analytics on the OLTP primary.
- **Tenancy:** a shared schema with `tenant_id` on every table and leading in
  every key/index. Scale up, then partition, then split by function, then shard.

## 1. Primary keys and IDs

**Default to `bigint`** [169][255]
- A test found about 6–7% overhead vs `int4`, negligible next to an emergency
  migration.
- Sequences burn values on failed inserts and rollbacks, so an `int` key
  overflows well before 2B live rows.
- Monitor sequence usage.
- Prefer `GENERATED ... AS IDENTITY` over `serial`: it is SQL-standard, owns its
  sequence, and `ALWAYS` rejects stray manual IDs. Identity on partitioned tables
  arrived in PG17.

**Fixing an int key that is near overflow** [255][400]
- Online recipe:
  1. Add a `bigint` column.
  2. Add a trigger that mirrors writes into it.
  3. Backfill in batches.
  4. Build a unique index `CONCURRENTLY`.
  5. In one short transaction with `lock_timeout`, swap the PK and rename the
     columns.
- Do the same for every FK column.
- Emergency stopgap: restart the sequence at `-1` with `INCREMENT BY -1` to buy
  the negative range.

**Random UUIDv4 primary keys hurt writes** [432][366]
- Mechanism: inserts land randomly across the B-tree, causing page splits and
  about 71% leaf density vs about 90% for ordered keys.
- After each checkpoint every touched page writes a full-page image, so WAL
  volume explodes and the whole index must stay hot in cache. The heaviest WAL
  producers the host has seen all used random UUID PKs.
- Evidence: moving to time-ordered UUIDv7 gave up to 23x faster inserts on some
  production tables [432].
- PG18 ships `uuidv7()` built in [366][385]. Current advice: "if you are using
  v4 for a primary key, now is the time to move to v7".

**UUIDv7 caveats**
- It leaks the creation time.
- It is 16 bytes, twice a `bigint`, in every index and FK.
- It is ordered only to the millisecond.
- Adding a `DEFAULT uuidv7()` column is not metadata-only, because the default
  is volatile.

**When UUIDs are right**
- IDs generated outside the database, multi-writer setups, or sharding.
- Otherwise a common split is a `bigint` PK for joins plus a separate public
  UUID. Always use the `uuid` type, never `text`.

**Partitioned tables** require the partition key in the PK/unique keys; put it
first.

## 2. Column types

- **`text` vs `varchar(n)`.** Same performance. Use `text` with a CHECK if a
  limit matters; changing `varchar(n)` later is a type change.
- **`numeric`.** Several times slower than int/float in aggregates and index
  builds. Use it only where exactness matters (money; or integer cents).
- **Floats.** Sums aren't associative, so totals can differ between parallel and
  serial plans.
- **Type mismatches defeat indexes.** A `numeric` parameter against a `bigint`
  column, or an unbound literal bigger than bigint, forces a seq scan. Keep join
  and FK column types identical.
- **Enum vs CHECK vs lookup table** [245][395][363]. This was revisited for
  years.
  - Enums are 4 bytes and easy to add to or rename, but you can't remove or
    reorder values.
  - The host prefers `text` plus CHECK, or a small lookup table with a
    `smallint` key.
  - Use an enum only for value sets that will truly never shrink.
- **Domains.** Bundle a type with its constraints (email, positive amount).
- **Built-in types.** Use `boolean`, `inet`, `uuid`, and range types rather than
  text.
- **`timestamptz`** always.
- **Column order ("column Tetris")** [363]
  - Alignment padding wastes space, for example a `smallint` before a
    `timestamp` pads 6 bytes.
  - Order columns 8-byte, then 4, then 2, then 1, then variable-length.
  - Worth 10–20% on large, stable tables (warehouse, time-series, sensor). Not
    worth chasing on OLTP tables whose columns keep changing; reordering means a
    full rewrite.
- **Wide rows.** Every UPDATE copies the whole row [264], and deforming late
  columns on very wide tables slows scans. Split rarely-read or
  differently-updated column groups out.

## 3. Normalization, JSONB, arrays, TOAST

**Normalize by default.** Denormalize deliberately (copied columns, pre-computed
aggregates) and keep the copies in sync.

**Avoid entity-attribute-value tables and delimited multi-value columns.** These
are the classic modeling anti-patterns.

**JSONB is for sparse, per-row extras.** Keep anything you join on, constrain,
range-filter, or read in bulk in real columns.

**JSONB costs**
- **The TOAST cliff** [201][406]
  - Values over about 2 KB are compressed and/or moved out of line.
  - Reading toasted JSONB is roughly 2–10x slower; the "toast tax".
  - Any update to a toasted value rewrites the whole value, with matching WAL
    and bloat.
  - Prefer many small rows over frequent edits to one big document.
- **No per-key statistics.** The planner is blind inside JSONB. Use an
  expression index plus `CREATE STATISTICS` on the expression when estimates go
  wrong.
- **Indexing options** [417]
  - An expression B-tree on `(data->>'k')` is fastest and smallest.
  - A stored generated column plus a B-tree suits hot, known fields.
  - GIN serves only containment (`@>`) and existence operators, not `->>`
    comparisons. It is bigger and slower to write.
- **Use `jsonb`, not `json`,** unless you must preserve the input verbatim.
- **Build API JSON in Postgres** (`json_agg`, `row_to_json`) rather than
  assembling it in the app. One example was about 60x faster [15].

**Medium-sized text**
- Text just under the TOAST threshold stays inline and bloats the heap. That
  slows scans of every other column [137].
- Fixes: lower `toast_tuple_target`, or move it to a side table.

**Arrays** [267][400]
- Tags as an `int[]` with GIN were less than half the size of a 3-table tag
  model and several times faster.
- Use arrays only for data that shares the parent row's lifecycle. Updating one
  element rewrites the row.
- If the elements are other tables' keys, use a join table so you keep FK
  integrity.

**`ANY(array)` over long `IN (...)` lists.** Planning is cheaper and there is
one `pg_stat_statements` entry.

**TOAST storage**
- Set `external` (no compression) for already-compressed blobs.
- Use LZ4 (PG14+) over pglz for much faster compression. Set it explicitly: the
  plan to make it the default in PG19 was reverted [435].
- Put big files in object storage with a reference in the database.

**Never `SELECT *`** on tables with wide text/JSONB. Detoasting is the hidden
cost; PG17's `EXPLAIN (ANALYZE, SERIALIZE)` exposes it.

## 4. Constraints and foreign keys

**Use FKs by default, and index the referencing column** [261]
- A parent delete scans the child table to check references. One delete spent
  13.9 ms of 14 ms in the FK check, and indexing the child column cut it to
  1.2 ms.
- You can skip the index only if parents are never deleted and children are
  never looked up by parent.

**Cascades** can wipe history from a single delete. Snapshot price and name onto
order and invoice rows instead of live-linking them to mutable parents.

**Locking**
- A child insert takes `FOR KEY SHARE` on the parent row.
- Use `SELECT ... FOR NO KEY UPDATE` instead of `FOR UPDATE` on parents unless
  you change keys, or you will block child inserts.
- Millions of children referencing one hot parent row (one streamer, millions of
  viewers) can exhaust multixact space.

**Add constraints online**
- `ADD CONSTRAINT ... NOT VALID`, then `VALIDATE CONSTRAINT` separately.
- Always set `lock_timeout`, because a waiting ALTER blocks everything queued
  behind it.

**Failed inserts aren't free**
- A unique violation still consumes an XID and leaves a dead tuple.
- Use `INSERT ... ON CONFLICT DO NOTHING`, which checks the index first [328].
- `ON CONFLICT DO UPDATE SET id = id` as a "touch" re-bloats and defeats HOT.

**Uniqueness on long values**
- A unique B-tree on long URLs is huge.
- `EXCLUDE USING hash (url WITH =)` was about 1/5 the size [401]. It can't be an
  FK target.

**Other constraint tools**
- `UNIQUE NULLS NOT DISTINCT` (PG15).
- Partial unique indexes for soft deletes (`WHERE deleted_at IS NULL`).
- Exclusion constraints with `btree_gist` and ranges for no-overlap bookings.

**Triggers: few, and for integrity or audit only.**
- Prefer stored generated columns to triggers.
- A trigger-maintained single summary row serializes every writer.

## 5. Indexes

**Composite indexes** [381][244][337][392]
- Equality columns first, range and sort columns last. A query can use a left
  prefix of the index, not a suffix.
- They beat combining two single-column indexes by bitmap, about 10x in one
  example [244]. The host says about 75% of his client recommendations are "use
  a multicolumn index this way".
- A `(tenant_id, sort_col)` index also fixes bad `ORDER BY ... LIMIT` plan
  choices.
- PG17 sped up `IN` lists on multi-column B-trees: 8.45 s became 0.11 s in one
  test [337].
- PG18 adds **skip scan**, so a filter on the second column can use the index
  [392]. It still works best when the leading column has few distinct values.

**Partial indexes**
- Use them for skewed or low-cardinality filters (`WHERE status = 'pending'`) and
  mostly-NULL columns. One `WHERE col IS NOT NULL` index shrank from 769 MB to
  5 MB [151].
- Downside: a status flag that flips constantly still costs a row rewrite plus
  index maintenance. A separate results table is often better.

**Covering (`INCLUDE`) and index-only scans**
- They need the visibility map to be current, so the table must be well
  vacuumed.
- For insert-only tables, set `autovacuum_vacuum_insert_scale_factor` (PG13+).

**Writes pay for every index** [189][399][435]
- Every insert and non-HOT update touches every index.
- 15 indexes under update-heavy load gave about 2x WAL, 2x latency and 65% more
  vacuum time [435].
- Including a frequently-updated column in an index kills HOT updates. In one
  test update throughput fell about 33% (46k to 30k TPS) [189].
- LLM- and advisor-suggested index sets tend to over-index.
- Find unused indexes via `idx_scan`, and drop invalid leftovers from a failed
  `CREATE INDEX CONCURRENTLY`.

**Lock manager limit** [306][288]
- Planning takes a lock per table and index. Up to PG17, past 16 relations per
  backend you leave the fast path, and high-QPS systems stall on
  `LWLock:LockManager`.
- GitLab had one table with 57 indexes, so 58 locks before joins.
- Many indexes plus many partitions multiply this. PG18 makes the fast-path
  slots scale with `max_locks_per_transaction`.

**BRIN** [276]
- Tiny (KBs vs hundreds of MBs). Inserts stay about as fast as with no index.
- Wins for large range scans on append-only, physically ordered data
  (timestamps, sequential IDs). Loses to B-tree for point lookups.
- Correlation is fragile: one future-dated row in every 70 still showed 0.97
  correlation, yet a query went from 8 ms to 3.2 s. The `minmax_multi` opclass
  (PG14+) fixed it.

**Hash indexes** [296][401]. Equality-only and smaller. Useful for huge text
keys, since only a 4-byte hash is stored. There is no uniqueness, ordering or
multi-column support; use the exclusion-constraint trick above for uniqueness.

**Trigram GIN** for `LIKE '%x%'`.

**Expression indexes**: the query must match the expression exactly, for example
`lower(email)`, or use `citext`.

**Fillfactor** 80–90 on update-heavy tables leaves room for HOT updates (check
`n_tup_hot_upd`). Keep 100 for append-only tables.

**Bloat and rebuilds**
- Vacuum doesn't shrink indexes. Monitor `pgstatindex` leaf density and use
  `REINDEX CONCURRENTLY`.
- PG13 B-tree deduplication and PG14 bottom-up deletion reduced
  update-driven index bloat a lot.
- After a collation or glibc change, reindex text indexes.

## 6. Designing around MVCC

**Every UPDATE writes a full new row version** [428]. Updating one row 1,000
times leaves about 1,000 versions for vacuum. Postgres "charges the future":
bloat, WAL, vacuum, and XID consumption.
- Undo-log engines (zheap, OrioleDB) have been "coming" since 2018 and still
  aren't mainstream. Design for the heap you have.

**Prefer insert-only designs for hot data** [142]
- Record status changes and counters as appended rows, then roll them up
  periodically.
- Cybertec's insert-only model smoothed write peaks on slow disks.

**Split hot columns** (counters, last-seen timestamps, status) into a narrow
table, so wide rows aren't rewritten on every change.

**Skip no-op updates.** Use `suppress_redundant_updates_trigger()` [168] or a
`WHERE col IS DISTINCT FROM new_value` guard. A blanket `SET x = x` doubled a
table's size.

**Batch big updates and deletes**, at about 1 second per batch, and let vacuum
keep up.

**Long transactions hold back cleanup across the whole cluster**
- Idle-in-transaction sessions, forgotten replication slots, and
  `hot_standby_feedback` all hold back vacuum.
- Set `idle_in_transaction_session_timeout`, `statement_timeout`, and
  `transaction_timeout`.

**Autovacuum per table**
- Set lower scale factors on big or hot tables.
- Raise `autovacuum_vacuum_cost_limit` before adding workers, because workers
  share the budget.

**Recovering bloat**
- `pg_repack` and `pg_squeeze` work online but need 2x space and have caveats on
  very large tables.
- PG19 adds `REPACK ... CONCURRENTLY` [437].
- A logical-replication cutover rebuilds everything compactly, and is the host's
  preferred reset.

## 7. Partitioning

**Why to partition** [363][358]
- The main reason is lifecycle: drop or detach old partitions instead of
  `DELETE`, vacuum a small hot partition instead of a giant table, and freeze
  cold partitions once.
- Query speed-ups are modest (often 0–20%) and only happen when queries filter
  on the partition key. Without the key every partition is scanned and planning
  cost grows with partition count.
- Consider partitioning around 100–200M rows [363]. Don't partition from day one
  without a retention or maintenance reason.

**Choosing the key**
- Time for events, logs, and time-series.
- Hash or list by tenant for SaaS.
- Two levels (hash by tenant, then time) where both matter.
- Partition by the way you will delete.

**Granularity: fewer, wider partitions** [306]
- Midjourney's daily partitions blew up lock-manager contention, and switching
  to weekly fixed it. Rule of thumb: if you planned daily, try weekly.
- Keep the total in the hundreds. Low thousands work but planning gets slower.

**Constraints**
- PK and unique constraints must include the partition key. There are no global
  indexes.
- Build indexes `CONCURRENTLY` on each child, then on the parent.

**Gotchas**
- The parent is never auto-analyzed; `ANALYZE` it after loads.
- Default partitions silently swallow bad rows and block
  `DETACH ... CONCURRENTLY`.
- Generic prepared plans can't prune at plan time and lock every partition;
  consider `plan_cache_mode = force_custom_plan`.
- `clock_timestamp()` (volatile) disables pruning, while `now()` doesn't.
- Pre-create future partitions.

**Migrating an existing table**
- Attach the old table as the default partition, then move small slices out
  under short locks.
- Or dual-write to a mirror table and backfill.
- Or use logical replication.

## 8. Deleting and retention

**"The only scalable delete in Postgres is DROP TABLE"** [422]
- `DELETE` marks tuples dead, updates every index, fires cascades, writes WAL,
  and never shrinks the file.
- Put data you will expire in time partitions.

**`TRUNCATE`** is near-instant and transactional, but takes an ACCESS EXCLUSIVE
lock.

**One-off purges**
- Copy the keepers into a new table in keyset batches, build indexes, and swap
  names in one transaction.
- Or batch-delete by id or ctid in small chunks.

**Soft deletes** cost a predicate on every query and complicate uniqueness and
FKs.
- If you use them, add partial indexes on `deleted_at IS NULL`.
- An alternative is moving deleted rows into a `deleted_record` table that holds
  the row as JSON.

## 9. Queue tables

**Baseline pattern** [376][419]
- Claim work with `FOR UPDATE SKIP LOCKED LIMIT n`, or `FOR NO KEY UPDATE SKIP
  LOCKED` when other tables FK to the queue.
- Select the claimed IDs in a `MATERIALIZED` CTE, then update or delete through
  it. Inline forms have mis-planned and claimed the wrong number of rows.

**The death spiral** [419]
- Claimed and deleted rows become dead tuples. Any long transaction pins the
  xmin horizon, so vacuum can't clean them.
- The `SKIP LOCKED` scan then wades through dead tuples, throughput drops, the
  backlog grows, and the cycle repeats.
- In one incident a migration holding a snapshot for hours led to about 4M dead
  tuples, 1 s claim queries, and 100% CPU on 24 cores.
- Keep long transactions off the queue's cluster, and alert on dead tuples.

**Zero-bloat design: PgQ** [416][437]
- Insert-only. Snapshot-based batches are ticked about 10 times a second, and
  each queue rotates through 3 tables that it `TRUNCATE`s.
- It stays flat even with the horizon held back, at the cost of 50–150 ms
  latency.

**Throughput**
- RudderStack reached about 100K events/s using append-only job and status
  tables of about 100K rows each [376].
- Most apps never need more than a plain `SKIP LOCKED` queue.

**`LISTEN/NOTIFY`** is for waking workers, not for the queue itself. It needs a
session-mode connection.

## 10. Pagination and counting

**Keyset pagination** [248][325][78]
- Write `WHERE (created_at, id) > ($1, $2) ORDER BY created_at, id LIMIT n`,
  with an index on exactly `(created_at, id)`.
- `OFFSET` cost grows linearly. Shopify saw offset 100K take over 2 s, and
  offset 1M time out.
- Use the row-value comparison, not the expanded `a > x OR (a = x AND id > y)`.
  The OR form scanned and discarded 40M rows in a 24 s query [316].
- Mixed sort directions break row-value comparison. Index and order by a negated
  expression instead.

**Total counts** are often the most expensive query on the page.
- Show only next/previous, cap the number of reachable pages, or estimate via
  `EXPLAIN` or `pg_class.reltuples`.

**`count(*)` is slow** because MVCC visibility means visiting every row.
- Options:
  - Index-only scans on a well-vacuumed table.
  - Estimates.
  - `TABLESAMPLE`.
  - HyperLogLog rollups for distinct counts: about 5% of the raw size and
    1.1 s → 80 ms with roughly 2–5% error [269].
- PG19 rewrites `count(1)` and `count(not_null_col)` into `count(*)` [394].

**Counter tables** have problems: a single counter row serializes writers. Fan
out to N rows with a random slot and sum them on read, or insert events and roll
them up.

## 11. Aggregates, analytics, time-series

**Incremental rollups** [17]
- Append raw events, then periodically run `INSERT ... SELECT ... GROUP BY
  bucket ON CONFLICT DO UPDATE` into a rollup table. Track the
  last-aggregated sequence value so each run processes only new rows.
- Citus measured 869 ms on raw data vs 5 ms on a 1-minute rollup.
- `pg_incremental` packages this pattern.

**Materialized views** [411]
- A full refresh recomputes everything, so cost grows with total data.
- `REFRESH ... CONCURRENTLY` needs a unique index and doesn't block reads.
- Good for "expensive query, staleness acceptable".
- For high write volume, use rollup tables instead. `pg_ivm` keeps views live
  but slows writes.

**Separate analytics from OLTP**
- First tune per role: timeouts, parallelism.
- Then a replica or logical replication to a reporting database.
- Then columnar engines (Citus columnar, Hydra, `pg_duckdb`, `pg_mooncake`) for
  scans.

**Time-series** [362]
- Insert-only, partitioned by time, and keyed by `bigint` or UUIDv7.
- Use BRIN on the timestamp, and B-trees on `(entity_id, time)` for "latest per
  device".
- Fillfactor 100, few indexes, batch inserts with `COPY` (or `unnest` arrays) not
  single rows.
- Use small types (`real`, `smallint`).
- Roll up old data and drop raw partitions.
- By 2026 the show's advice was to use TimescaleDB for serious time-series
  work, for its compression and continuous aggregates.

## 12. Multi-tenancy and scaling out

**A pooled shared schema is the norm** [372][292]
- The host has never seen a client choose schema-per-tenant or
  database-per-tenant.
- Isolated models multiply migrations and catalog size, and break
  transaction-mode poolers, since `search_path` is session state.
- Reserve them for a few security-sensitive or huge customers.

**Tenant key everywhere**
- Put `tenant_id` on every table, even when it denormalizes.
- Make it lead composite PKs, unique keys and indexes.
- Choose it early: it is also your future shard key, and co-located joins
  depend on it.

**RLS** has a per-query cost and complicates pooling. The host avoids it for
hot paths.

**Scale-up order**, consistent across years:
1. Fix queries and indexes.
2. Partition for maintenance.
3. Add read replicas and a pooler.
4. Split by function into separate databases (GitLab CI, Instacart).
5. Shard last.

**Evidence and tooling**
- OpenAI ran a single primary with about 50 replicas, and its pain was MVCC
  write bursts [402].
- Shard tooling (Citus, PgDog, Multigres, PlanetScale) is maturing. In 2026 the
  host says sharding "may make sense earlier than historically" [413].
- Notion (480 logical shards) and Figma built their own [187].

## 13. Temp tables, unlogged tables, too many tables

**Temporary tables** [349]
- They are per-session in Postgres, so churn bloats the catalog. One system had
  186 temp schemas and a 25x bloated catalog.
- They pin pooled connections, and autovacuum never touches them.
- Use them briefly with `ON COMMIT DROP`. Otherwise use unlogged tables or
  session variables.

**Unlogged tables**
- About 2–3x faster writes.
- Truncated on crash, absent on replicas and after failover.
- Fine for staging, caches, and tests. "If you care about your data, don't use
  unlogged."

**Too many tables** [424]
- Tens of thousands of tables plus long-lived connections grow each backend's
  relcache and catalog cache (to hundreds of MB), leading to OOM kills.
- Fixes: a pooler, `idle_session_timeout`, and fewer tables.
- The same pressure applies to over-partitioning.

## How the advice moved

| Topic | Older | Current |
| --- | --- | --- |
| UUID PKs | Avoid; use bigint or a sequential-UUID extension | UUIDv7, built in since PG18 |
| int vs bigint | int is fine for small tables | always bigint |
| serial | standard | identity columns |
| Enums | "underrated" | CHECK or lookup table preferred; enums OK for frozen sets |
| Queues | `SKIP LOCKED` is the answer | `SKIP LOCKED` plus partition drops, or PgQ-style zero-bloat |
| Partitioning | for speed; thousands are fine (PG10/11) | for retention; fewer, wider partitions; lock manager is the limit (fast path widened in PG18) |
| Composite index on 2nd column | unusable | PG18 skip scan |
| Bloat cleanup | pg_repack / pg_squeeze | PG19 `REPACK CONCURRENTLY` |
| Sharding | last resort | still last, but tooling is lowering the bar |

## Sources

Scaling Postgres episodes cited above (YouTube):

| Ep | Topic |
| --- | --- |
| [17](https://www.youtube.com/watch?v=ja12mzX6288) | Real-time analytics rollups |
| [78](https://www.youtube.com/watch?v=JmRl0fEZ9KA) | Efficient pagination |
| [137](https://www.youtube.com/watch?v=zZaeJUDnAdU) | TOAST and medium-text impact |
| [142](https://www.youtube.com/watch?v=Djbz6riiYmU) | Insert vs update modeling |
| [151](https://www.youtube.com/watch?v=MUjdmshu43U) | Partial index on mostly-NULL column |
| [168](https://www.youtube.com/watch?v=iK9qqWuFpwY) | Avoiding redundant updates |
| [169](https://www.youtube.com/watch?v=l5Zkjomm7pI) | Use bigint |
| [189](https://www.youtube.com/watch?v=MKYzckNtDtg) | Index downsides and HOT |
| [201](https://www.youtube.com/watch?v=BruAzAdzEXk) | JSONB performance |
| [244](https://www.youtube.com/watch?v=2L3ka-VeZo0) | Index merge vs composite |
| [245](https://www.youtube.com/watch?v=75fWu_a0p6s) | ENUM vs CHECK |
| [248](https://www.youtube.com/watch?v=gajXwHZdKek) | Pagination options |
| [255](https://www.youtube.com/watch?v=9aMFacwWni8) | Integer overflow |
| [261](https://www.youtube.com/watch?v=gBZhpWcSJTc) | FK index on delete |
| [267](https://www.youtube.com/watch?v=ZDbsa1YGDFw) | Tags and arrays |
| [269](https://www.youtube.com/watch?v=6vOrG9BPbNE) | HyperLogLog |
| [276](https://www.youtube.com/watch?v=4Dusemyd31M) | BRIN and correlation |
| [288](https://www.youtube.com/watch?v=1auuMSjynVo) | Lock manager, fast path |
| [292](https://www.youtube.com/watch?v=2WmywVkzxuc) | Tenancy models |
| [306](https://www.youtube.com/watch?v=jpsOnXbBz8E) | SLRU and lock manager limits |
| [316](https://www.youtube.com/watch?v=-GNNSObATM4) | OR vs row-value comparison |
| [325](https://www.youtube.com/watch?v=B0oNW3XRUtk) | Keyset with mixed sort |
| [328](https://www.youtube.com/watch?v=IR2Pex9sIhY) | Get-or-create / ON CONFLICT |
| [337](https://www.youtube.com/watch?v=JMZKinKyYOI) | PG17 IN-list speedup |
| [349](https://www.youtube.com/watch?v=piDfZvDuo6M) | Dangers of temporary tables |
| [358](https://www.youtube.com/watch?v=JW7zJfqRTIU) | Partitioning, IoT ingest |
| [362](https://www.youtube.com/watch?v=Ypk47jtp4t0) | Time-series design |
| [363](https://www.youtube.com/watch?v=yqMJo6Gzf5Q) | Dos & don'ts, alignment |
| [366](https://www.youtube.com/watch?v=z_2I0Lac-XE) | uuidv7 in PG18 |
| [372](https://www.youtube.com/watch?v=TQI6S5cY1cI) | Pooled vs isolated tenancy |
| [376](https://www.youtube.com/watch?v=K8fc9loPGnM) | 100K events/s queue |
| [381](https://www.youtube.com/watch?v=LtEK8YbNC7I) | Multicolumn indexes |
| [385](https://www.youtube.com/watch?v=qDCVncuMm_E) | Postgres 18 release |
| [392](https://www.youtube.com/watch?v=BdPkcEBuKDY) | Skip scan |
| [394](https://www.youtube.com/watch?v=4lEo42sPli8) | Faster count() |
| [395](https://www.youtube.com/watch?v=8kqbahmgl0w) | Enum comparison |
| [399](https://www.youtube.com/watch?v=l-SNJIYW9T8) | Index overhead |
| [400](https://www.youtube.com/watch?v=PO2UHREv-ro) | Cost of arrays |
| [401](https://www.youtube.com/watch?v=HBvfYJYT-f4) | Hash exclusion for uniqueness |
| [402](https://www.youtube.com/watch?v=NHOdbhpPMaI) | OpenAI single primary |
| [406](https://www.youtube.com/watch?v=StyH2O3cZ6w) | JSONB toast tax |
| [411](https://www.youtube.com/watch?v=uZbeQyNnbaI) | Absorbing load, matviews |
| [413](https://www.youtube.com/watch?v=Y0KrM1Lo3Ac) | PgDog resharding |
| [416](https://www.youtube.com/watch?v=ULpnamtBZFM) | Zero-bloat queue (PgQ) |
| [417](https://www.youtube.com/watch?v=Zt_7Xv6GKLg) | JSONB indexing choices |
| [419](https://www.youtube.com/watch?v=ZUrs6FOnPEw) | Queue death spiral |
| [422](https://www.youtube.com/watch?v=_HmF482gcps) | Only scalable delete is DROP |
| [424](https://www.youtube.com/watch?v=gS4hbXVey48) | Too many tables |
| [428](https://www.youtube.com/watch?v=1dDfpnqd04U) | MVCC costs |
| [432](https://www.youtube.com/watch?v=JMN_CF23Z7o) | 23x faster inserts with UUIDv7 |
| [435](https://www.youtube.com/watch?v=KZePj0Li0Uw) | Index write overhead; LZ4 revert |
| [437](https://www.youtube.com/watch?v=HoksjCrDvKE) | REPACK CONCURRENTLY; PgQ |
