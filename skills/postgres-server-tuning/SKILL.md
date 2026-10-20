---
name: postgres-server-tuning
description: >
  Configure a PostgreSQL instance and its host for performance:
  shared_buffers, effective_cache_size, work_mem (and how it multiplies
  across nodes and connections), maintenance memory, checkpoints and
  max_wal_size, WAL compression and synchronous_commit, storage and I/O
  (NVMe vs network volumes, PG18 async I/O), huge pages, Linux overcommit
  and THP, filesystems, and cloud/ARM sizing. Use when setting up or
  reviewing postgresql.conf, sizing hardware, or investigating
  instance-wide slowness, checkpoint spikes, or OOM kills.
---

# Postgres server and hardware tuning

A playbook distilled from *Scaling Postgres*. Evidence and benchmarks are in
[reference.md](reference.md). Related skills:

- Single-query fixes: `postgres-query-tuning`.
- Autovacuum knobs: `postgres-autovacuum`.
- Connection limits: `postgres-connection-pooling`.

## Principles

- **Only a few settings matter.** Config changes typically give tens of
  percent, not multiples. Queries, indexes and schema give the multiples.
  Get the basics right, measure, then stop.
- **Change one thing at a time,** with a before/after benchmark of your own
  workload. That applies to OS changes too.

## Starting points

```conf
shared_buffers = '25% of RAM'          # then verify with cache hit / pg_buffercache
effective_cache_size = '50-75% of RAM' # planner hint only, allocates nothing
work_mem = '16MB'                      # modest globally; raise per role/session for analytics
maintenance_work_mem = '1GB'           # index builds, vacuum (see autovacuum_work_mem)
random_page_cost = 1.1                 # SSD/NVMe
checkpoint_timeout = '15min'           # 15-30 min
checkpoint_completion_target = 0.9
max_wal_size = '...'                   # large enough that checkpoints are triggered by time, not WAL volume
wal_compression = on                   # lz4/zstd where available
log_checkpoints = on
```

These are rule-of-thumb defaults, uncited unless the reference says
otherwise. Verify each against your own workload.

## Rules

- **`work_mem` multiplies.** It applies per sort or hash node, per parallel
  worker, per connection. A high global value plus many connections leads to
  OOM. Keep it modest globally; raise it with
  `ALTER ROLE analytics SET work_mem = ...` or `SET LOCAL`.
- **Checkpoints**
  - If `checkpoints_req` dominates `checkpoints_timed`, raise `max_wal_size`.
  - Frequent checkpoints multiply full-page writes, because the first touch of
    each page after a checkpoint writes the whole page.
- **`synchronous_commit = off`** trades the last few hundred milliseconds of
  commits on a crash for throughput. That's acceptable per transaction for
  low-value writes, never as a blanket default for important data.
- **Storage**
  - Local NVMe has far lower latency than network-attached volumes (EBS-style).
  - With network storage, latency, not throughput, usually limits
    single-session speed. Budget IOPS and durability, and keep replicas.
- **PG18 asynchronous I/O.** Keep the default `io_method = worker`. Benchmark
  before switching to `io_uring`. Tune `effective_io_concurrency` /
  `maintenance_io_concurrency` for your storage.
- **Linux**
  - Turn on huge pages when `shared_buffers` is large (`huge_pages = try` or
    `on`, plus `vm.nr_hugepages`).
  - Disable transparent huge pages.
  - On a dedicated host, use `vm.overcommit_memory = 2` with a sane ratio, so
    the OOM killer doesn't take the postmaster.
- **Network.** App-to-database round-trip latency adds to every query. Keep
  them in the same zone, and batch or pipeline chatty access.

## Review checklist

- [ ] Memory: `shared_buffers`, `work_mem` × connections × nodes fits RAM,
      maintenance memory sized.
- [ ] Checkpoints are time-driven and logged, and `max_wal_size` fits the peak
      WAL rate.
- [ ] Storage latency is measured, and `random_page_cost` reflects SSDs.
- [ ] Huge pages and overcommit are set on dedicated hosts, and THP is off.
- [ ] Every change has a before/after benchmark.
