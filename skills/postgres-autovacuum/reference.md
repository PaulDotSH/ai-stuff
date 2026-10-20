# Postgres autovacuum, bloat, and wraparound

Distilled from the autovacuum-related segments of *Scaling Postgres* (Creston
Jamison, episodes from about 124 to 439), from auto-generated transcripts. `[NNN]`
is an episode number. Most numbers come from third-party blog posts discussed on
the show, so read them as "this happened on one workload", not as constants.
Where advice changed over the years, the newest take wins. For schema choices that
avoid bloat in the first place, see `postgres-schema-design`.

## Checklist

- **Throttle:** raise `autovacuum_vacuum_cost_limit` first (thousands). Add
  workers only when the default ones are always busy. Never set the autovacuum
  cost delay to 0.
- **Triggers:** lower the scale factor from the 20% default to about 1-2% on big
  or hot tables. Append-only tables still need vacuum (PG13+ insert threshold).
- **Horizon:** set `idle_in_transaction_session_timeout`; hunt forgotten
  replication slots, prepared transactions, and `hot_standby_feedback`. "Dead but
  not yet removable" means something holds xmin.
- **Wraparound:** alert on XID age and multixact age, per database and per table.
  Don't ignore "unnecessary" vacuums.
- **Bloat:** `REINDEX CONCURRENTLY` for indexes. For tables, an online rewrite
  tool or a logical-replication cutover. Never `VACUUM FULL`.
- **Stats:** `ANALYZE` partitioned parents by hand, and everything after an
  upgrade or reload.

## 1. What autovacuum does

**Four jobs** [420][168]
- Garbage-collect dead row versions.
- Refresh planner statistics (autoanalyze).
- Freeze old tuples so transaction IDs can be reused.
- Maintain the visibility map, which index-only scans depend on [329].

**Why it matters**
- Every update writes a full new version, so updating a row 1,000 times leaves
  about 1,000 versions to clean [428].
- One test: 1M rows were 135 MB of heap. After nine whole-table updates with
  autovacuum off the table and index were about 10x larger, and a full read was
  4-5x slower [405].
- Selects also write. They set hint bits and prune pages, which dirties buffers
  [351].

**Posture**
- Keep autovacuum on. The only sane case for `autovacuum_enabled = off` is a
  throwaway staging table [168][268].
- Vacuum is rarely the cause of an incident, and the usual answer is to run it
  more [397].
- Vacuum is I/O bound, not CPU bound [415].

## 2. When it fires

**Scale factor** [420][276]
- The default (20%) means a billion-row table accumulates 200M dead tuples before
  a vacuum starts.
- Aim for 1-2%, and below 1% on very large tables.
- Prefer global settings, with per-table overrides for the few hot or huge tables
  [325][222]. Opinions differ on whether per-table settings are worth the
  management burden [129].

**Insert-only tables** [180][226]
- Before PG13 an append-only table was autoanalyzed but never autovacuumed, so
  the visibility map went stale. One index-only query took about 10 ms on the
  primary and 300 ms on a replica until a manual vacuum fixed it [180].
- PG13 added `autovacuum_vacuum_insert_threshold` and
  `autovacuum_vacuum_insert_scale_factor` [180][226].
- On older versions: cron a manual vacuum, or lower the table's
  `autovacuum_freeze_max_age` [180].
- Append-only tables still need vacuum for statistics and the visibility map [343].

**Naptime.** A shorter naptime starts vacuums sooner; together with a lower
scale factor it flattened the TPS dips of a high-churn queue [281].

**Starved tables and peak-hour runs** [222]
- Busy tables take every vacuum while quiet tables age toward an anti-wraparound
  run.
- Supplement with a nightly manual `VACUUM FREEZE` on the tables with the oldest
  `relfrozenxid`.

**Is it configured well?** [207]
- Sample `pg_stat_activity` (every 5 minutes) for running autovacuum workers
  versus `autovacuum_max_workers`. If all are always busy, tune.

**PG19 scheduling** [428][433]
- Autovacuum stops using `pg_class` order and scores tables on XID age, multixact
  age, dead tuples, inserted tuples, and changes since the last analyze.
- Five configurable weights. Inspect with `pg_stat_autovacuum_scores`.

## 3. Throttling and resources

**Mechanism** [209]
- Each page hit, miss, and dirty adds cost. At `cost_limit`, the worker sleeps
  `cost_delay`.

**Cost limit is the main lever** [420][325][200]
- Default 200; the host sets 2,000-4,000 or more depending on size [420].
- EDB: usually at least about 2,000 [325].
- Range 1-10,000, and 8-9 thousand on multi-TB systems with spare CPU [200].

**Workers share one budget** [325][222][200]
- Adding workers makes each one slower.
- Default 3; the host goes up to 5-6, never more. A setting of 15 is a classic
  mistake [222][200].
- Nick's rule of thumb: a quarter of cores [420].
- More workers make sense for many small partitions. Keep them low for a few huge
  tables [226].

**Cost delay**
- 2 ms in recent versions versus 20 ms before [209].
- Manual vacuum has delay 0, which is why it feels faster than autovacuum [168].
- **Never set autovacuum delay to 0.** Vacuum raced through an append-only 40 GB
  TOAST table setting hint bits, wrote full-page images faster than replicas
  could replay, and caused `IPC:SyncRep` waits. 2 ms fixed it [413][390].

**Running vacuums.** PG16 lets you raise cost limit on a vacuum already running.
Before that, only new workers saw the change [261][222].

**Memory**
- Pre-17, vacuum stored dead TIDs in an array capped at 1 GB. Overflow forces
  multiple index-vacuum passes [320][311].
- PG17's new structure uses about 20x less memory and removes the cap [335][318].
  In a 100M-row test, PG16 hit the 64 MB `autovacuum_work_mem` nine times; PG17
  needed one index pass, 37 MB, and finished in 619 s versus 773 s [311].
- PG18: an oversized `maintenance_work_mem` risks OOM, so set
  `autovacuum_work_mem` to about half a GB [420].

**Parallel**
- Manual `VACUUM` parallelises index vacuuming from PG13 (on by default); index
  vacuum was over 95% of runtime in a test [130]. Autovacuum never did [130].
- PG19 adds `autovacuum_max_parallel_workers`, but since vacuum is I/O bound set
  `autovacuum_parallel_workers` per table, only for GIN or 4-5+ index tables [415].

**Faster hardware.** Local NVMe is a big win when vacuum falls behind at hundreds
of GB [433]. PG18 async I/O helps vacuum [385]. PG14 sped vacuum up about 25%
[158].

## 4. Monitoring

- **Dead tuples and last vacuum:** `pg_stat_user_tables`. The "is this table
  vacuumed enough" check [295].
- **Cybertec query set** covering dead tuples, bloat, autoanalyze, visibility map,
  and XID/multixact wraparound [415].
- **Logging:** `log_autovacuum_min_duration` [370]. PG18 adds vacuum delay time to
  views and logs [354] and per-table cumulative vacuum/analyze time [352]. PG19
  adds `log_autoanalyze_min_duration` [390]. PG17 shows index-vacuum progress
  [335].
- **Dead tuples that "come back" after ANALYZE** [367][322]
  - Vacuum skips index cleanup when 2% or fewer of the pages have dead line
    pointers. Heap tuples become LP_DEAD, and ANALYZE counts them as dead again.
  - `INDEX_CLEANUP ON` forces the index pass.
- **Partitioned parents are never autovacuumed or autoanalyzed** [382].
  - In one example, row estimates were off by 5,000x until a manual ANALYZE.
  - Run ANALYZE daily or weekly. A patch to autoanalyze them was reverted from
    PG14 [184].
- **After upgrades and reloads** [339][385][371]
  - `pg_restore` and `pg_upgrade` don't analyze. Use `vacuumdb` with jobs equal
    to cores and `--analyze-only` [339].
  - PG18's `pg_upgrade` carries planner stats, but extended stats are missing, so
    run `vacuumdb --missing-stats-only` [385][371].

## 5. Detecting bloat

- **Tables:** `pgstattuple`. One example: 0% fresh, 30% after a delete [158].
- **Indexes:** `pgstatindex` leaf density [379]
  - 90% for a fresh B-tree; 60% after delete plus vacuum.
  - About 70% under load is normal; watch the trend, not one reading.
  - Range scans suffer most; single-row lookups barely notice.
- **Rebuild signals** [397]: after a massive delete, bloat ratio above 2.0 and
  rising, plans flipping to seq scans, index size out of proportion to row count.
- **Vacuum doesn't shrink files** except by truncating the tail [236][332]. It
  also can't merge sparse index pages or lower tree depth [397]. Dropped columns
  stay in the file, and the 1,600-column limit is never reset [364][392].
- **Danger** [301]: when bloat outpaces autovacuum, XIDs climb, and a bloated
  table hurts I/O because pages are full of dead space.

## 6. Removing bloat

**Indexes: `REINDEX CONCURRENTLY` first**
- The host says index bloat is usually worse than table bloat [414].
- One reindex cut a scan from about 24,000 buffers to 33 [405]. Clients reclaimed
  10-30% of disk this way [326].
- Reindexing partitioned tables has caused query cancellations [397].
- Clean up failed `_ccnew` leftovers [151].
- `pg_index_watch` automates it from the index-size-to-tuples ratio [273].

**Tables**
- **`VACUUM FULL` / `CLUSTER`:** ACCESS EXCLUSIVE and about 2x disk. "When to use
  vacuum full: never" [252][270].
- **pg_repack:** online and trigger-based, but needs 2-3x space [158][349].
  - It hung or never finished on a ~1 TB table for the host [414][301].
  - It has locking caveats [171].
- **pg_squeeze:** logical decoding. A 69 MB table doubled to 138 MB after updates
  and squeezed back to 69 MB [136][349].
- **Logical-replication cutover:** the host's preferred reset, because it rebuilds
  everything compactly, not just one table [414][301][326].
- **Retention cases:** copy the keepers, swap, drop [333], or use partitions
  [422][359].

**PG19 `REPACK`** [410][414][437]
- `REPACK` replaces `VACUUM FULL` and `CLUSTER` [410]. `CONCURRENTLY` copies under
  SHARE UPDATE EXCLUSIVE, using logical decoding on a replication slot [414].
- It takes a brief ACCESS EXCLUSIVE lock at the swap [437].
- On an ~18 GB table it beat `VACUUM FULL` on time, matched pg_squeeze on WAL,
  and used less peak extra disk. 3,000 updates/s added about 20 s [437].
- While it runs the slot holds back vacuum for other tables and databases, so
  quiet the whole system [437].
- It tries the swap once; a `lock_timeout` aborts the entire repack [437].
- That post was written before release, with "assuming it does not get reverted"
  [437].

**Prevention**
- Batch bulk changes at about one second of work. Start with 1,000 rows, ramp to
  5,000-20,000, and interleave vacuum [217][198][313].
- Select the IDs, then delete; run a manual vacuum between rounds [344].
- One giant transaction can't prune or vacuum its own dead rows [238].
- Failed inserts still burn an XID and leave a dead tuple. `ON CONFLICT DO
  NOTHING` avoids both; `DO UPDATE` locks the row and assigns an XID
  [321][207][414].
- A queue with a pinned horizon spirals: 4M dead tuples meant 1 s claim queries
  and 100% CPU [419]. `postgres-schema-design` covers queue design.

## 7. What holds back the horizon

**Symptom:** "dead but not yet removable". There are three usual causes [205]:
- long transactions;
- unconsumed replication slots (usually seen first as WAL growth);
- prepared transactions.

**Also on the list** [226][207][140]
- `hot_standby_feedback`, which bloats the primary. `vacuum_defer_cleanup_age` is
  similar.
- Idle-in-transaction sessions [398][169].
- A long initial `COPY` during a migration [419], or a migration tool's
  long-running transaction [212].
- Subtransactions, which burn XIDs [293][181].
- A running REPACK [437].
- An idle session holding a temp table, which autovacuum can't touch, led to
  wraparound [400].

**Timeouts** [398][173][411]
- Set `idle_in_transaction_session_timeout` and `statement_timeout`, plus
  `transaction_timeout` where available.
- Apply `statement_timeout` per role, not database-wide. A global value cancels
  backups and vacuums [173].
- `idle_session_timeout` also helps [400].
- `old_snapshot_threshold` was the older suggestion [169].

## 8. Freezing and wraparound

**Parameters**
- XIDs are 32-bit, about 2 billion usable [168][207].
- `autovacuum_freeze_max_age` (200M default) triggers an aggressive anti-wraparound
  vacuum. `vacuum_failsafe_age` (1.6B) is the last resort. Warnings begin before
  2B [322].
- A very active OLTP system can move freeze max age up to about 1B, no further
  [209][161].
- Anti-wraparound vacuum doesn't yield to conflicting locks such as an ALTER
  TABLE that would normally cancel a regular vacuum [419].

**Monitor and alert** [161][229][136]
- Percent toward wraparound, per database then per table.
- Include `template0`: allow connections, `VACUUM FREEZE`, then disallow [322].

**If you are close** [161][351][384][364]
- `VACUUM (FREEZE)` the worst database, then the worst table [161].
- `INDEX_CLEANUP OFF` gives a fast freeze-only vacuum. It is for emergencies and
  doesn't remove dead tuples [351].
- Clear long transactions and orphan slots first [384].
- One team throttles ingestion until the database catches up [364].

**Burn fewer XIDs.** Batch inserts [265], avoid subtransactions [293][181], and
use `ON CONFLICT DO NOTHING` [321].

**Corruption error.** `found xmin from before relfrozenxid` appears only during
vacuum. It means corruption, and a dump and restore of the table is the safe fix
[353].

**Multixacts** [418][376][181]
- Created when several transactions lock one row (`SELECT FOR SHARE`, FK checks,
  `FOR UPDATE` with subtransactions).
- Anti-wraparound kicks in at 400M versus 200M for XIDs [418].
- Monitor with the same discipline: add multixact age monitoring, watch FK parent
  tables, adjust the multixact freeze max age if needed, and don't dismiss
  "unnecessary" vacuums [418].
- A separate limit is the **member space**. Multixact entries are immutable, so
  growth is quadratic, and as few as 63,000 overlapping transactions can exhaust
  it. Long transactions are the aggravator [376].
- Watch the `pg_multixact/members` directory size where you can [376].
- Multixact offsets were widened to 64 bits in PG19, but 64-bit XIDs are not
  coming soon, because of `pg_upgrade` link mode [417].

## 9. vacuum_truncate and replicas

- The truncate-the-tail step takes ACCESS EXCLUSIVE. Selects hung behind it until
  `ALTER TABLE ... SET (vacuum_truncate = false)`. Run `VACUUM (TRUNCATE,
  VERBOSE)` manually when you want it [370]. `pg_freespacemap` shows whether the
  high-water mark is growing [370].
- On a replica the same lock can cancel queries [419][140].
- Replayed vacuum cleanup cancels standby queries (snapshot conflicts).
  `hot_standby_feedback` stops that but bloats the primary [140].
- Separate standbys for HA and for queries/backups avoid the trade-off [140].

## How the advice moved

| Topic | Older | Current |
| --- | --- | --- |
| Insert-only tables | never autovacuumed; cron a vacuum [180] | PG13 insert threshold and scale factor [180] |
| Cost delay default | 20 ms [209] | 2 ms [209] |
| Changing cost limit | only new workers see it [222] | PG16 updates running vacuums [261] |
| Index bloat | PG13: unchanged index 8x larger [182] | PG14 bottom-up deletion [182] |
| Vacuum memory | 1 GB cap, multiple index passes [320] | PG17 no cap and 20x less [318]; PG18 keep `autovacuum_work_mem` modest [420] |
| Parallel vacuum | PG13 manual only [130] | PG19 `autovacuum_max_parallel_workers`, per table [415] |
| More workers | "raise workers" | raise cost limit first; workers share one budget [222][420] |
| Table bloat | `VACUUM FULL` or pg_repack/pg_squeeze [252] | PG19 `REPACK CONCURRENTLY` [437] |
| Partitioned parents | no autoanalyze; patch reverted in PG14 [184] | still manual ANALYZE [382] |
| Table choice | `pg_class` order [433] | PG19 score-based [433] |
| 64-bit XIDs | "eventually" | not soon; PG19 only widens multixact offsets [417] |
