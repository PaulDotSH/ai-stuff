# Postgres upgrades

Distilled from *Scaling Postgres* (Creston Jamison, 2018–2026), from auto-generated
transcripts. `[NNN]` is an episode number. The host runs upgrades for clients (the
"summer of upgrades" [370]), so most of this is one practitioner's field practice plus
third-party case studies; read sizes and downtimes as "this happened on one system".
Where advice changed, the newest take wins.

## Checklist

- **Minor releases:** apply promptly, read the "updating" notes for reindex or manual
  steps, test in staging first. If exposure allows, a short wait avoids early
  regressions.
- **Major method:** small DB → dump/restore. Many small instances, a few minutes of
  downtime acceptable → `pg_upgrade --link` (or swap on PG18). Large or near-zero
  downtime → logical replication cutover, now the host's only preferred method [439].
- **Script it and rehearse:** many dry runs make the cutover a non-event [387].
- **Afterwards:** `ANALYZE`. PG18 `pg_upgrade` keeps planner stats except extended
  stats [385][371].
- **OS, glibc or ICU change with a file-copying method:** reindex text indexes.
  Logical replication and dump/restore avoid this [397].
- **Extensions and poolers:** check compatibility and patch them too [343][438].

## 1. Minor releases

**Mechanics**
- Minor = swap binaries and restart, so a brief outage [343]. Run a checkpoint or two
  first to shorten the restart [317].

**Read the "updating" section every time** [296]
- Reindex is the commonest extra step. Examples: GiST; B-tree on `interval`; BRIN
  minmax-multi on date/timestamp types [290]; BRIN numeric minmax-multi [380]; BRIN
  null handling [277]; one release needed reindexing for non-libc collation providers
  [404]. `REINDEX CONCURRENTLY` exists from PG12, and for partitioned tables from PG14
  [290].
- Others: recreate self-referential FKs on partitions [365]; follow the guideline for
  FKs where you attached/detached partitions [343]; recreate some system views for a
  CVE in 16.3 [317]. Some releases need nothing [316].

**Out-of-cycle releases happen**
- 17.1 changed an ABI struct and broke extensions (Apache AGE, TimescaleDB), and 17.2
  followed. Older majors were affected too [343][344].
- Feb 2026: two regressions (`substring` errors on non-ASCII text; a standby halting
  with "could not access status of transaction") led to an out-of-cycle release on
  Feb 26 [405]. Feb 2025: a libpq regression; the advice was to wait for the next
  round if not exposed [354]. Unplanned releases are rare [296].

**Policy:** security volume is high, so patch fast. May 2026 fixed 11 CVEs, with more
reaching back to PG14 than usual [417]. Mixed minors can bite: a replica on
14.23/15.18/16.14 replaying WAL from a primary on older minors could deadlock on the
multixact-offset SLRU; 17+ was unaffected [425].

## 2. Choosing a major method

- **dump/restore:** simplest, slowest, per database [80]. 4 TB would take days [212].
  Use `-j`; for small tables pipe `pg_dump` into `pg_restore` [154]. Indexes are
  rebuilt, so collation is safe [231].
- **`pg_upgrade`:** fastest in place, but link mode forfeits the old cluster and
  replicas need extra work [144][80].
- **Logical replication (LR):** shortest downtime and safest fallback, with more
  caveats [80].
- **Trigger-based tools** (Slony, Bucardo): avoid. Slony development stopped [221];
  one team's Bucardo attempt failed on locking and they took hours of RDS downtime
  instead [116][21].
- The host's drift: `pg_upgrade --link` as default [144][185]; later LR only [297]; by
  2025 no `pg_upgrade` "in about a year or two" [360]; still true in 2026 [439].
- `pg_upgrade` still suits fleets of small clusters that tolerate brief downtime [360].

## 3. pg_upgrade

**Mechanism**
- It restores only the schema, then copies data files unchanged [344]. Modes: copy,
  clone, `copy_file_range`, link [360]. Needs both versions' binaries [341].
- Link mode: the old cluster is unusable once the new one starts [144]; starting it
  anyway corrupts the shared files [229]. Keep a standby or old cluster as fallback
  [144][132]. Run `pg_upgrade --check` first (standard option).

**Downtime and speed**
- Link: seconds for multi-TB [144]; the host reports under 2 minutes of conversion for
  databases larger than 4 TB [212]. CloudNativePG: 2 TB (PG13→17, "I believe") in 33 s;
  time follows table count, not size [360].
- PG18: carries over statistics, speeds up many-object clusters, parallel checks with
  `--jobs`, new `--swap` [385]. On 17→18, swap took 11.4 s against a little over 13 s
  for link [360].
- PG19 widens multixact offsets to 64 bits, so `pg_upgrade` must convert and takes
  longer [416]. Full 64-bit XIDs would rewrite all data and end link mode [417][265].

**Afterwards**
- Pre-18, stats are lost: `vacuumdb` analyze-only with `--jobs` ≈ cores [339]
  (`--analyze-in-stages` is the standard staged variant). On PG18, extended stats are
  not carried; run `vacuumdb --all --analyze-only --missing-stats-only` [371]. PG18's
  `pg_dump --statistics-only` can pull stats from an older server (shown on PG16) [410].
- Index format gains need `REINDEX`: PG12 [92]; PG13 dedup took one index from 408 MB
  to 126 MB [117].
- PG15 public schema: `REVOKE CREATE ON SCHEMA public FROM PUBLIC` and
  `ALTER SCHEMA public OWNER TO pg_database_owner` [231].
- Before PG17, `pg_upgrade` destroyed logical slots, so recreate them before writes
  resume [174]. PG17 keeps them [318][335].

## 4. Logical replication cutover (blue/green)

The host prefers it for a shorter cutover than link, no collation reindex, a fresh
compact copy, free testing and an easy fallback [297][301][88].

**Prepare**
- Needs `wal_level=logical`, slots and senders [370]. It works per database [88].
- Every table needs a PK or replica identity (`FULL` as last resort) [88][48][129].
- Add tables individually. With `FOR ALL TABLES`, a new table stops replication and
  extension tables get pulled in [129][358]. Refresh the subscription after adding
  tables [372].
- **Freeze DDL:** LR does not carry schema changes [341][88]. Copy roles and schema
  with `pg_dumpall --globals-only` and `pg_dump --schema-only` [88].
- **Large objects** move manually [88].
- Keep the subscriber write-free: a colliding insert stops replication and WAL piles
  up on the publisher [137]. Stalled slots also grow WAL [88].
- One upgrade (databases with 200,000+ tables) hit `max_wal_senders` of 50 with a slot
  per table, so tables went across in batches [269].

**Initial copy**
- Without indexes or constraints, 300M rows loaded in 10 min plus 20 min for the PK
  [371]. The host keeps PKs during the initial load and adds other indexes at the end
  [371][269].
- A long copy holds a snapshot and the xmin horizon. One migration's initial copy ran
  for hours beside an 11 TB table and a job queue: 7M queued rows, 100% CPU on 24
  cores [419]. A migration service's long transactions blocked autovacuum and caused
  downtime [212].
- Big tables: several publisher/subscriber pairs (row filters on PG15+) that don't
  cross FK boundaries [360][295]; 10 parallel streams on a multi-TB table [295].
  Alternative: create the slot on a replication connection, dump using its snapshot,
  then create the subscription with `copy_data=false`, `create_slot=false`,
  `enabled=false`, and enable it [296]. `pg_export_snapshot` works too [406].
- The initial sync is the slowest step. PG17 `pg_createsubscriber` converts a physical
  replica to logical [331]; RDS snapshots seeded the copy at Instacart [275].

**Sequences**
- Not replicated [48]. The host pasted a one-liner [392]; pglogical forced a sync of PK
  sequences before switching [150].
- PG19: one-time sync at `CREATE SUBSCRIPTION`, on refresh, or with
  `ALTER SUBSCRIPTION ... REFRESH SEQUENCES`. It is not continuous, so run it as a late
  cutover step [423][424].

**Verify**
- `pg_subscription_rel.srsubstate = 'r'` for every table, then your own checks [88].
- Compare table and index counts, row counts and known queries; a data-diff tool can
  help [319]. Confirm zero lag [174].

**Cut over**
- Order: pause writers (`pg_hba.conf` or firewall [174]; PgBouncer pause/resume [346];
  a per-app pause of about 5 s [384]) → zero lag → sync sequences → repoint → drop the
  subscription [88][382].
- Script everything, including non-database services (Ansible) [375]. Keep the old
  cluster read-only until satisfied [88].

**Roll back** (the managed clouds give none, see §5)
- Stop traffic and drop connections. Create slots on the new cluster so reverse LR
  keeps the old one current. To fail back: stop traffic, confirm zero lag, copy
  sequences, repoint [382]. Another team took a short outage before the switch to
  start reverse replication [372].

**After:** `ANALYZE` everything, including partitioned parents, which autovacuum never
touches. One team analyzed only the leaves and saw slow queries until the parents were
done; the host runs a full-database analyze [382]. PG18's stats transfer does not help
here [410].

## 5. Managed clouds and operators

- **RDS in place:** 20–30 min of downtime [346]. **Aurora in place:** 15+ min [351].
- **RDS/Aurora blue/green** is LR underneath [291]. Downtime is under a minute but not
  guaranteed [346][351]. Limits: active replication slots block it [351]; no RDS Proxy,
  no existing subscriptions, no built-in rollback [354]; no documented Postgres
  rollback [371]. Add your own rollback (§4).
- **DIY LR on RDS** with a snapshot to seed the copy: a 13-step runbook [354].
  Medplum chose it, using PgBouncer to pause traffic [346].
- **CloudNativePG** offers in-place (`pg_upgrade`) and LR upgrades [360][346].

## 6. Replicas and clusters

- Core has no cluster-aware upgrade [221]. Upgrade the primary with `pg_upgrade`, then
  each replica with the rsync procedure in the docs [156]. Link mode can cost you the
  standbys [80]; this is the main `pg_upgrade` pain point [108].
- PG17 path: stop standby B, run `pg_createsubscriber`, `pg_upgrade` B, start it,
  build physical replica C from B, move writes A→B, retire A [335].
- Upgrade the subscriber first; disable the subscription before stopping the publisher
  [335]. After a failover, disable, repoint and re-enable the subscription [337].
- A new replica on a different OS than its primary hits collation issues [183].

## 7. Collation, glibc and ICU

- **Mechanism:** the sort order changes under the index. Symptoms: rows missing from
  index scans, unique violations, duplicate PKs [145][359][397].
- **Triggers:** an OS upgrade (the 2018 glibc change; Ubuntu 18.04→20.04) [412][269];
  ICU version changes (ICU 73) [288]; moving files by `pg_upgrade` or base backup [231].
- **Scope:** text indexes not on a binary collation, plus indexes with collatable
  expressions (`upper`, `length`) or predicates. Integers and dates are fine
  [397][145]. Find damage with `amcheck` [183]; fix with `REINDEX CONCURRENTLY`, which
  adds downtime [397][231].
- **Duplicate-PK repair:** disable index scans, find and delete duplicates, reindex
  [359].
- **PG18:** full-text-search indexes use the default provider, not libc; reindex them
  [385].
- **Avoid it:** LR or dump/restore [307][397]; ICU collations upgraded on your own
  schedule [231]; the PG17 built-in `C.UTF-8` provider [323][406]. Built-in is fast and
  OS-independent [344][323]; create databases from `template0` [323]. Add `COLLATE`
  per column for linguistic order [406].

## 8. Extensions, tools, PG19 notes

- Validate extensions on every major upgrade [221]; PostGIS needs its own upgrade
  [238].
- Patch pgvector (0.8.2 fixed a parallel-HNSW CVE) and PgBouncer (1.26 fixed 3 CVEs,
  two reachable by unauthenticated clients) [419][436][438].
- PG19 (RC1 out; GA planned Nov 19) [439]: JIT off by default; standard-conforming
  strings always on, so use the newest `pg_dump` across versions; default
  `max_locks_per_transaction` doubled 64 → 128, so recheck tuned values [433].

## 9. Performance after upgrading

- Planner regressions are real: `ORDER BY ... LIMIT` choosing the ordering index over a
  selective filter after 13/14→16/17 (adding `+ 0` to the sort column steers it)
  [383][366]. Incremental sort took one query from seconds to 10 minutes on 11→15;
  `enable_incremental_sort` and `enable_presorted_aggregate` are the escape hatches
  [336].
- Test by running your top ~100 queries with costs and buffers before and after
  [330].

## 10. Case studies

| Case | Method | Detail |
| --- | --- | --- |
| GitLab, 9.6→11 | `pg_upgrade --link`, Ansible | 12 × 96-core hosts; 30-minute target; 9.6 kept as fallback [132] |
| Retool, 4 TB, 9.6→13 (Azure) | LR via Warp | wanted ~1 h; dump/restore = days [212] |
| Gadget, 7 TB on PG13 | LR per app | 5 s pauses, max 4 s, p95 250 ms; 300 GB control plane then upgraded by LR [384] |
| Instant (Aurora) | LR | in-place 15+ min; blue/green blocked by slots [351] |
| Host's 2025 clients | LR, blue/green | 4 DBs, mean 13 TB, median 8 TB [374]; a 10 TB active DB split across publishers [360] |

## 11. End of life

Five major versions are supported [221]. Observed dates: 9.6 Nov 11 2021 [178]; 10 Nov
10 2022 [215]; 11 Nov 9 2023 [277]; 12 Nov 14 2024 [316]; 13 Nov 13 2025 [380]; 14 Nov 12
[417]. Managed clouds retire versions too (RDS 9.6) [188].

## How the advice moved

| Topic | Older | Current |
| --- | --- | --- |
| Default method | `pg_upgrade --link` [144][185] | logical replication; `pg_upgrade` for fleets [360][439] |
| LR for upgrades | new and uncharted [39]; pglogical for pre-10 [21] | native LR; PG17 keeps slots, adds failover slots and `pg_createsubscriber` [335] |
| Sequences | hand-rolled or pglogical [150][392] | PG19 `REFRESH SEQUENCES` [423] |
| Planner stats | lost; re-analyze [111] | PG18 keeps them; extended stats via `--missing-stats-only` [385][371] |
| Fastest in-place | `--link` [144] | `--swap` on PG18 [360] |
| Collation | reindex after OS change [145] | avoid via LR or the built-in provider [397][323] |
| Trigger tools | Bucardo/Slony tried [24][116] | avoid [21][221] |
| Blue/green | LR under another name [291] | usable with your own rollback [382] |
