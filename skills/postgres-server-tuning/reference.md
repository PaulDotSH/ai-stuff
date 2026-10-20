# Postgres server and hardware tuning

Distilled from *Scaling Postgres* (Creston Jamison) episode transcripts on
instance-level configuration and hardware. `[NNN]` is an episode number. Most
numbers come from third-party blog posts and vendor benchmarks discussed on the
show, so read them as "this happened on one workload". Where advice changed, the
newest take wins. Per-query tuning, autovacuum knobs and connection limits live
in `postgres-query-tuning`, `postgres-autovacuum` and `postgres-connection-pooling`.

## Checklist

- **Few settings matter.** Config changes usually give 20-50% [305]. Get
  `shared_buffers`, `random_page_cost`, checkpoints and `work_mem` right, then
  measure.
- **Memory:** `shared_buffers` about 25% of RAM, then verify. Keep global
  `work_mem` modest and raise it per role, session or function.
- **Checkpoints:** `checkpoint_timeout` 15-30 min, `checkpoint_completion_target`
  0.9, `max_wal_size` big enough that time, not WAL volume, triggers checkpoints.
  Log checkpoints.
- **WAL:** turn on `wal_compression`. Leave `synchronous_commit` on unless you
  accept losing recent commits.
- **Storage:** local NVMe beats network volumes by a wide margin; budget for
  durability if you use it.
- **Linux:** huge pages on for big `shared_buffers`; `vm.overcommit_memory=2`
  on a dedicated host.
- **PG18+:** keep `io_method = worker`; try `io_uring` only after benchmarking.
- **Test before and after** every OS-level change.

## 1. shared_buffers and effective_cache_size

**The 25% rule is a starting point**
- 25% of memory is "the accepted value" [320]. Other guides say 15-25% [280], or
  the smaller of 50% of RAM or 10 GB [335]. A vendor ran 60%, which the host
  called crazy [295].
- EDB tests on enterprise SSDs: OLTP throughput was flat up to about a quarter
  of RAM, then declined. When the working set fit, it rose linearly to about 25%
  and levelled off. Warehouse workloads did best at the 128 MB default, within a
  few percent [239].
- On magnetic disks, OLTP kept improving up to half of RAM [236]. The host's
  rule: size it to how often you re-read the same data [239].
- Too small is a cliff: one TPC-C run fell from 7,082 TPS to 500 at 2 MB [377].

**Verify instead of guessing** [320]
- With `pg_buffercache`, a pool that is not fully used after a long uptime is
  too big. A full pool holding a small fraction of hot tables is too small.
- Aim for a cache hit ratio around 98-99% [280].
- `pg_prewarm` reloads the cache after a restart [086].

**effective_cache_size** is a planner hint, not an allocation. More OS cache
makes index scans more likely [091]. Guidance ranges from 50% [029] to 75% [335]
of RAM.

**Rank of importance:** `shared_buffers` first, `random_page_cost` second [305].
For SSD/NVMe, set `random_page_cost` to 1.1 [361][376]. One write-up derived
1.05 from a 99% cache hit rate [213]. Older advice said 1 for SSDs [104].

## 2. work_mem and memory multiplication

**It is a per-node budget, not per query or connection**
- Each sort or hash node can take `work_mem`, parallel workers multiply it, and
  the memory is not released until the whole operation ends [408].
- Worst-case estimate: connections x `work_mem` x about 5 nodes [321]. An
  inverse "conservative" rule is `shared_buffers` / connections / 5 [432].
- Hash operations can use multiples of it [321]. `hash_mem_multiplier` appears
  once, raised modestly for a queue workload [376].
- A 2 TB machine was OOM-killed with `work_mem` at 2 MB. One backend held 557 MB,
  and `pg_log_backend_memory_contexts` found it [408].

**Setting it**
- Default 4 MB. Start around 16 MB and raise it as temp files appear in the log
  [017].
- Enable `log_temp_files` and compare sizes to current `work_mem` [432][321]. Or
  take `temp_bytes / temp_files` from `pg_stat_database` and add that to the
  setting [432].
- Raise per role, session or function [263][257][334]; `ALTER FUNCTION ... SET
  work_mem` stopped 150 GB/day of temp spills [437].
- OLAP boxes with few connections can go far higher: 20% of RAM in one post
  [104]. 4 MB to 1 GB cut one sort from 6.5 s to 2.7 s [027].
- Don't set it blind. The host keeps the default or a small bump and measures
  [263].

**OOM kills: lower it, don't raise it** [398]
- A slight bump took modelled usage from 38 GB to 76 GB. Lower `work_mem` and
  `maintenance_work_mem`, and use per-session overrides for reporting.
- Other levers: fewer hash joins (add indexes), fewer parallel workers, or more
  RAM [329]. Many tables can also bloat a backend's cache context [424].

## 3. maintenance_work_mem and autovacuum_work_mem

- Try 1 GB, and remember each autovacuum worker may take that much: 3 workers x
  10 GB is 30 GB [048]. 1 GB is normal for mid-sized servers, and PG17 removed the
  1 GB cap for vacuum [320].
- Too small forces repeated index passes: a 100M-row test hit 64 MB 9 times on
  PG16 but needed one pass on PG17 [311].
- PG18 can OOM when `maintenance_work_mem` is too high. Set
  `autovacuum_work_mem` to about 0.5 GB [420].
- More is not always faster. CREATE INDEX got slower from 64 MB up to 16 GB,
  probably because of L3 cache effects [391].

## 4. Checkpoints and full-page writes

**Mechanism** [403][258]
- The first change to a page after a checkpoint writes the whole page to WAL,
  which makes a TPS "sawtooth". Frequent checkpoints mean more WAL and more
  I/O.
- Same pgbench run: 77 GB of WAL with a low `max_wal_size`, 5.3 GB with 150 GB
  and a 1-day timeout [104]. Another: 12 GB at 5 min vs 2 GB at 1 hour [403].
- Spiral: heavy writes cause checkpoints, full-page images, more WAL and more
  autovacuum [150]. Random UUID keys can produce more WAL than the database
  size in 24-48 hours [350].

**Settings** (default timeout is 5 min, completion target was 0.5 [151])
- 15 min, 0.9, `wal_compression`, `log_checkpoints`, `max_wal_size` 16 GB [258].
  Others use 15-30 min [325][424], 30-60 min [325], or 1 hour [403]. The host's
  sweet spot is 15-30 min [325]. A client ran 25 min [259].
- The 0.9 target is the usual production value [403][413]. It was expected to be
  hard-coded in PG14 [151].
- `min_wal_size` at a fifth to a tenth of max keeps segments around [258].
- Crash recovery takes a fraction of the timeout, replicas cover failover, and
  short timeouts add full-page writes [424][403]. Counterpoint: keep 5 min if
  recovery time objective matters [423], or cap `max_wal_size` [259].

**Sizing `max_wal_size`** (the host's method)
- Set `checkpoint_warning` about 5 min below the timeout (10 for 15, 25 for 30).
  A warning in the log means WAL volume forced an early checkpoint [258][325].
  Raise `max_wal_size` until warnings stop [207].
- A pgbench run with 1 GB had a forced checkpoint that tanked TPS, while 4 GB
  did not. Too large only costs disk and replay time [413].
- `shared_buffers` tuning beat stock by 3.5x, `max_wal_size` by 1.5x [207].

**Monitoring**
- `log_checkpoints` is on by default since PG15 [238]. `pg_stat_checkpointer`
  (PG17) or `pg_stat_bgwriter` before it shows checkpoint counts [413].
- Alert on duration. With a 5 min timeout, healthy runs span 20 s to nearly 5
  min. Spikes to 50-60 min are a problem [423]. A stalled checkpointer once
  exhausted inodes with 27 million files [423]. A PG16.8 bug stalled it on a
  fsync queue near 1 GB [424].
- Before a restart or shutdown, run a manual `CHECKPOINT` once or twice
  [286][317].

## 5. WAL settings

- **`wal_compression`:** recommended [258]. It costs a little CPU for large disk
  savings [101]. Choosing the algorithm was added later [239]; the transcripts
  give no lz4/zstd comparison.
- **Write amplification:** one example turned 40 bytes in into 340 out. Batch
  with COPY, drop unused indexes, enable compression, and raise `max_wal_size` and
  timeout. Measure with `pg_current_wal_lsn` and `pg_stat_wal` [435].
- **`synchronous_commit`:** on returns after flush. Off risks losing recent
  commits [365]. The host wouldn't turn it off [029].
- **`commit_delay`:** batches commits into one flush. On a disk throttled to 1,000
  IOPS, 1 ms took 1,500 TPS to 2,700. It adds latency and is hard to tune [351].
- **Separate WAL and data disks** if needed, on SSD/NVMe [121]. A multi-AZ
  cluster stayed fast writing WAL to local NVMe [320].
- **`wal_level = logical`** logs more. PG19 raises it only when a slot exists,
  at the cost of a forced checkpoint [417].
- **Replicas replay in one process,** so they can lag, especially with ZFS
  compression [354][342]. PG15 added I/O-advice prefetch
  (`maintenance_io_concurrency`) for replay, Linux with ext4/XFS only [241].

## 6. Storage, latency and I/O

**NVMe vs network volumes**
- Half-TB pgbench: local NVMe 16,000 TPS vs gp3 (3,000 IOPS) 1,700. Median
  latency 36.9 ms to 4 ms, page reads 19 ms to 0.3 ms, fsync 11 ms to 1.5 ms
  [433].
- PlanetScale (vendor, biased): gp3 3,000, gp3 10,000 and io2 16,000 IOPS vs about
  300,000 on NVMe. All cost near $500/month except io2 at about $1,500 [388].
  A migration example fell from 9 ms to 4 ms at p99 [373].
- Network storage gives snapshots and easy resize. Local gives IOPS and low
  latency but is easy to run out of [382][375]. Enable `track_io_timing` [382].
- For ephemeral NVMe: a primary plus two or more standbys with quorum sync
  replication across AZs, WAL shipped to object storage, and backups [433].
- Striping 16 EBS volumes reached about 30,000 TPS and cut latency from about
  16 ms to near 1 ms [364].
- Linux dirty-buffer saturation on a gp2 volume made TPS erratic [212].

**PG18 async I/O** (reads only [384])
- `io_method`: `worker` (default, 3 workers), `io_uring` (Linux 5.1+) or `sync`
  [385]. Helps sequential scans, bitmap heap scans and vacuum, not index scans
  [384]. Expect 2-3x on some loads [365].
- Gains are biggest on high-latency storage. On io2, sync matched PG17 and
  `io_uring` roughly tripled a scan [365].
- It often disappears elsewhere. On NVMe there was no difference, and at 50
  connections PG17 and PG18 matched. On network volumes `worker` beat `io_uring`
  [388]. Cybertec's 1 TB test: 4:48 `io_uring` vs 6:08 `sync` [385].
- Tuning: stay on `worker` and set workers to about 25% of cores [385].
- `pg_basebackup` went from 300-500 Mbit/s to about 1 GB/s on PG18 [392].
- PG19 scales workers between `io_min_workers` and `io_max_workers` [414].
  Direct I/O is debug-only [386].
- `effective_io_concurrency` is barely covered [365]. Replay prefetch uses
  `maintenance_io_concurrency` [241].

**Network latency**
- A co-located app on directly attached NVMe has near-zero DB latency, while
  Aurora/AlloyDB-style storage ships WAL around and adds some [298].
- Measure baseline with `SELECT 1` from the app's region [373].

## 7. Linux: huge pages, overcommit, THP

**Huge pages**
- One setup had 25 GB of page tables without huge pages vs 62 MB with them
  [187]. EDB saw CPU fall from 51% to 15% [192].
- Recipe: with the server stopped, run `postgres -C
  shared_memory_size_in_huge_pages` (PG15+) to size the pool, set `huge_pages`
  (default `try`) to `on`, restart [267]. 14+ may need extra for
  `min_dynamic_shared_memory` [267].
- With about 100 GB of buffers and no huge pages, a Linux 7 kernel showed
  regressions on cold start [413]. Huge pages gave 20% when the postmaster
  hit its limit at about 1,400 connections/s [404]. Containers need host access
  [413] and OCI-spec support [285].
- If startup fails, try `huge_pages = off` [303].

**THP:** most advice is to disable it [044][168]. The evidence was mixed [054].

**Overcommit and OOM** [327]
- `vm.overcommit_memory=2` with `overcommit_ratio` (default 50) or
  `overcommit_kbytes`, because a killed backend forces crash recovery.
- It affects every container on the host, so dedicate the machine.
- `kill -9` on a backend likewise forces a restart [289].
- Other OS tweaks: `noatime` [335], swap and dirty-page sysctls, TCP timestamps
  off [168].

## 8. Filesystems

- **ext4** is the fastest default [237]. btrfs had problems. ZFS came close, was
  half as fast on small selects in some tests, and gave steadier throughput
  because full-page writes can be off [237].
- **ZFS** gives instant snapshots and cache devices [212]. Aligning recordsize to
  8 KB nearly doubled performance, slightly passing ext4 [344]. One shop kept
  128 KB and built Postgres with 32 KB blocks, and LZ4 beat zstd [364]. LZ4 gave
  3-5x compression [131].
- ZFS with zstd: load 267 s vs 382 s on ext4, reads 621 TPS vs 850 [342]. The
  single replay process can lag on compressed ZFS [327][354][300].
- PG18 `file_copy_method = clone` (XFS reflinks, ZFS, APFS) cut a 6 GB template
  copy from 1:07 to about 200 ms [398].

## 9. Cloud, ARM, big CPUs and benchmarks

- Self-managed EC2 plus ZFS doubled write throughput against RDS and Aurora
  [300]. AI workloads push people toward BYOC for IOPS [400].
- Graviton2 was about 25% cheaper and 15% faster in one in-memory test, with the
  gap narrowing at more threads [149].
- Huge CPUs hit NUMA: remote memory 2-3x slower [354].
- `max_worker_processes` about 25% of vCPUs or higher [324]. Default
  `max_parallel_workers_per_gather` is 2, so raise it for OLAP [176].
- "Tuned" ClickBench was only `shared_buffers` 8 GB, 16 workers, 8 per gather
  and `max_wal_size` 32 GB [284].

## 10. Tools and workflow

- No pgtune-style calculator is named. Guides list the same set: `shared_buffers`,
  `effective_cache_size`, `work_mem` [117], `maintenance_work_mem` [109]. Cloud
  hosts scale these with instance size [375].
- Baseline: `random_page_cost` 1.1, memory settings, then observability [361].
- Use `conf.d` includes or `ALTER SYSTEM`; the last setting wins [094].
- Log checkpoints, temp files, lock waits and slow queries [276].

## How the advice moved

| Topic | Older | Current |
| --- | --- | --- |
| shared_buffers | 25% of RAM [019] | 25% start, verify [320][239] |
| checkpoint_timeout | 5 min default; 15-30 min [151][325] | 15-30 min; hour [403]; keep 5 [423] |
| completion target | 0.5 default [151] | 0.9 [151][403] |
| Storage | EBS default; some run NVMe [032] | NVMe push [433][388]; durability risk |
| Async I/O | future [214] | PG18 worker default [385]; PG19 auto workers [414] |
| Huge pages | mixed [054] | enable for big buffers [192][413] |
| ZFS | for compression [022] | viable, align recordsize, replica lag [344][342] |
| work_mem | per-connection [017] | per-node, per-role; lower on OOM [398] |
| maintenance_work_mem | 1 GB [048] | PG17 uncapped [320]; PG18 half GB [420] |
| random_page_cost | 1 on SSD [104] | 1.1 [361], 1.05 [213] |
