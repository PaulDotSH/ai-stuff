# Postgres connections and pooling

Distilled from the connection and pooler coverage in *Scaling Postgres*
(Creston Jamison, 2018–2026), from auto-generated transcripts. `[NNN]` is an
episode number. Most numbers come from third-party blog posts and vendor
benchmarks discussed on the show, so read them as "this happened on one
workload", not as constants. Where advice changed, the newest take wins.

## Checklist

- **Pool by default.** Use a pooler once you are past a few hundred
  connections; one dos-and-don'ts list says "more than 500" [363]. Run application
  traffic in transaction mode.
- **Cap `max_connections` low.** About 10x vCPUs at most, per the Postgres.FM
  guidance retold on the show [396]. Thousands on
  a managed default (RDS ships about 5,000) is a smell [355][396].
- **Size the server side small.** Tens to low hundreds of backend connections
  behind thousands of clients [320][018][409]. Waiting in the pooler queue
  beats overloading the backend [154].
- **Know what transaction mode breaks.** `SET`, `search_path`, session-level
  advisory locks, temp tables, `LISTEN`, SQL-level `PREPARE` [135][222][315][349].
- **Prepared statements:** PgBouncer 1.21+ with `max_prepared_statements`,
  protocol-level only [287].
- **Keep a direct or session-mode path** for migrations and `LISTEN/NOTIFY`
  workers [396][315].
- **Watch the pooler.** `cl_waiting` explains latency Postgres never sees
  [332]. PgBouncer is single-threaded; scale with more instances on one port
  [163][242].
- **Patch the pooler.** Versions before 1.26 have CVEs reachable by
  unauthenticated clients [436][438].
- **Set timeouts end to end:** `idle_in_transaction_session_timeout`,
  `statement_timeout` per role, TCP keepalives, and
  `client_connection_check_interval` [398][259][189][403].
- **Add jitter** to fleet restarts and reconnect loops [404].

## 1. Why Postgres needs a pooler

**Process per connection.** Every connection forks a backend, and all of them
touch shared memory and shared structures [237][360]. A pooler can hand out
connections without that model [237].

**Connect/disconnect is the expensive part.**
- A `select 1` pgbench run went from about 295,000 TPS with persistent
  connections to about 3,700 TPS with a new connection per query, a 98.7% drop
  [237].
- Session-mode PgBouncer with connection overhead included held over 750 TPS
  against 200 or less direct (about 4x) [279]. Without connection overhead,
  direct was up to about 3x faster at 50–150 connections [279]. Pooling mostly
  wins when clients reconnect.
- Azure measured about 4x throughput and 40% lower connection latency with
  PgBouncer [051].
- Planning cost on partitioned tables stabilizes on a persistent connection
  but not on a new one, so reconnect churn hurts [337].
- The first query on a new connection can be slow because extensions such as
  PostGIS load lazily; `session_preload_libraries` helps [299].

**Idle connections are not free.**
- 100 idle connections cost about 150 MB of freeable memory. The memory was not
  returned until the connection closed, and `DISCARD ALL` didn't help. CPU
  rose with count [147].
- With 1,000 idle connections added, pgbench TPS dropped 8.7% (read/write),
  18.2% (select-only) and 46% (large-query custom run, 378 to 206 TPS). Behind
  a PgBouncer limited to 200 server connections, 1,000 idle clients cost
  nothing [147].

**Snapshot scalability.**
- Cause: `GetSnapshotData` work scales with connection count, even idle ones.
  One active plus 10,000 idle connections about halved throughput [135][138].
- The PG14 patch gave 2–3x at 10,000+ connections and kept TPS flat as idle
  count grew [138].
- An independent re-run saw 1,500 TPS fall to a third somewhere between 10,000
  and 15,000 connections, and concluded not to set `max_connections` above
  need [181].
- PG14 beta held connections better than 13.3, but less than the original
  patch post suggested [166].

**Cache bloat.** One connection touching many objects grows its caches. 20,000
tables took a backend's cache from 4 KB to 221 MB. Fixes: a pooler or
`idle_session_timeout` to reap backends [424]. Recycling pooled connections by
age also helps [135].

**`max_connections` sizing.**
- Default is 100 with 3 for superusers [110]. PG16 adds `reserved_connections`
  (default 0) via `pg_use_reserved_connections`; 100 - 3 - 10 leaves 87 [291].
- Each connection can use `work_mem` or more, so memory is a real ceiling
  [110].
- One benchmark peaked at 300–500 concurrent connections and fell off after
  700 [070].
- Tribal rule: a few hundred, not more than 500 [070]. Postgres.FM guidance: about
  10x vCPUs (32 CPUs, 320 connections) [396].
- The host has never recommended thousands, and a system at 2,000 connections
  showed lock-manager conflicts [355].
- Pool-size formula from PG14 `pg_stat_database` session stats: connections <
  max(cores, parallel IO limit) / session busy ratio x average parallelism
  [154]. The inputs are loose.
- Look for idle sessions first. One team cut average connections from 2,700 to
  just over 1,000 by changing application behavior [264].
- `too many clients already` errors and load spikes are further reasons to
  queue in a pooler [154].

## 2. Pool sizing and placement

- **Ratios:** 5–10x clients per server connection [237]. 10–20 app connections
  per server connection [271]. 1,000+ clients down to 40–50 [320]. 10,000
  clients to 10–100 server connections, depending on query speed [018].
- **Example PgBouncer sizing** (PlanetScale): 50 Postgres `max_connections`,
  500 `max_client_conn`, `default_pool_size` 30, `max_user_connections` 30,
  `max_db_connections` 40, leaving 10 for administration [409].
- **Queueing helps.** On a 2-vCPU server, 20 direct connections gave load
  average about 17 and a 17 s SSH login. A PgBouncer pool of 4 gave 1.73 and
  0.5 s. Throughput was 5% better with queuing and better again in transaction
  mode [154]. "Slow connections down to make the database faster" [202].
- **Pool exhaustion hides latency.** `pg_stat_statements` looked fast while the
  APM saw long waits, because clients sat in PgBouncer's `cl_waiting` [332].
  Size the pool against concurrency, then watch waits.
- **Placement:** a local PgBouncer per node on 6432, a separate one for the
  primary, a separate one for replicas, or one per app server [409]. OpenAI
  ran multiple PgBouncer pods per replica [402].
- **App-side pools** also matter: reuse connections [352], set size limits and
  export metrics [084]. Add server-side pooling at scale [161]. Latency
  of a simple task grew as active connections rose from 1 toward 1,000 [035].
- **Replica routing** beyond the pooler (HAProxy, read/write split) is out of
  scope; see [195][033].

## 3. PgBouncer pool modes and what breaks

**Modes** [269][409][237]
- **Session:** 1:1 with a server connection. Little benefit beyond faster
  connects, and no memory savings [320].
- **Transaction:** the mainstream choice; many clients per server connection
  [237][095].
- **Statement:** the most aggressive; almost unused, because multi-statement
  transactions are impossible [269][409].

**What breaks in transaction mode**
- **Session state:** prepared statements, temp tables and `SET` [135].
- **Tenancy:** `SET search_path` can mix tenant data across clients [136]. A
  session variable for RLS forces session mode [226].
- **Advisory locks:** `pg_advisory_lock` is session-scoped; use
  `pg_advisory_xact_lock` [222].
- **Temp tables** mean session use [349]. Pooler-side pinning also happens in
  RDS Proxy for `SET`, temp tables, some advisory locks and queries over about
  16 KB [431].
- **`LISTEN/NOTIFY`** needs a dedicated session connection [315][425].
- **Migrations:** connect directly or as a separate user, with `lock_timeout`
  and retries, not through the pooler [396].

**Prepared statements**
- Older advice: behind PgBouncer you couldn't use them, and Rails had to turn
  them off, losing the benefit [039][240][335].
- PgCat added them in session and transaction mode: about 15% over simple and
  30% over extended protocol in pgbench [271].
- **PgBouncer 1.21** tracks protocol-level named prepared statements in
  transaction and statement mode [287].
  - `max_prepared_statements` defaults to 0 (off); 100 is a reasonable start.
  - It does not handle SQL-level `PREPARE`/`EXECUTE`/`DEALLOCATE ALL`/`DISCARD
    ALL` [287].
  - Synthetic throughput gains were 15%–250%. Tracking costs CPU on a
    single-threaded process, so be careful if it is already pegged [287].
  - A 14-way self-join: planning 170 ms vs execution about 12 ms; after
    `PREPARE`, planning fell to about 0.2 ms [288].
- 1.22 added handling of `DISCARD ALL` and `DEALLOCATE ALL` [301]. 1.22.1 fixed
  a bug introduced in 1.21 in `COPY FROM STDIN` handling [306].
- libpq could not send protocol-level Close until PG17, so many drivers send a
  manual `DEALLOCATE`; a Perl DBD option skipped it. Errors look like
  `prepared statement ... does not exist` [288].
- Many drivers prepare silently [250]. Prepared statements cut planning and
  lock-manager contention [306][291][335]. With partitions, generic plans can
  cost memory; one fix was `plan_cache_mode = force_custom_plan` [267].

## 4. PgBouncer config, auth, operations

**Auth**
- `userlist.txt` with `auth_file` is simplest [046].
- `auth_user` + `auth_query`: a role with a `SECURITY DEFINER` function over
  `pg_shadow`, revoked from `PUBLIC` [046][040].
- PAM, LDAP and a forced user exist [119]. `auth_type = hba` uses
  `pg_hba`-style rules [046].
- SCRAM arrived in 1.11 [079]. 1.14 allows encrypted SCRAM secrets for
  server login [118]. User maps for cert/peer came in 1.23 [323]. LDAP and
  client-side direct TLS came later [392].
- A pooler can face clients with relaxed TLS and use `verify-full` to the
  server [237].

**Security patching** [363][395][417][436][438]
- 1.24.1 fixed `VALID UNTIL` expiry not being honored (CVE-2025-2291) [363].
- 1.25.1: unauthenticated SQL during auth via a malicious search_path
  parameter, only with several settings enabled [395].
- 1.25.2: four CVEs; the host links the spike to AI-driven bug hunting [417].
- 1.26: three CVEs, two reachable by unauthenticated clients [436]. Don't
  expose it to the internet [436]. Pooler upgrades get forgotten [438].

**Lifecycle**
- 1.23: SIGTERM now waits for clients to disconnect; a second SIGTERM or
  `QUIT` shuts down immediately [323].
- 1.20 deprecates `-R` online restart; `so_reuseport` is the suggested route
  [276]. 1.16
  reloads TLS without restart [178].
- `PAUSE`/`RESUME` enables cutovers and upgrades [346][369][011]. Use `PAUSE`
  when everything must switch together, `KILL` in emergencies [047]. 1.19 adds
  `SHOW STATE` [265].
- Cancel requests can reach the wrong PgBouncer. Since 1.19, `peer_id` and a
  `peers` section forward them [331].
- A server restart surfaced "server closed the connection unexpectedly" in
  session mode but not transaction mode [090]. A pooler adds latency, an extra
  failure point, and an auth layer [086][118].

**Single-threaded: use more processes**
- One core caps it: 100% of one core at 50 clients in a benchmark [303].
- Run several instances on separate ports [108]. Better, share one port with
  `so_reuseport`; systemd templates, and socket activation (1.14) give
  per-instance admin ports [127][163][242].
- That also supports read/write routing and DoS resilience [163].

**Monitoring**
- Query `SHOW POOLS`/`SHOW STATS` via SQL with `pgbouncer_fdw` [097]. Alert on
  `cl_waiting` [332].
- Use USE/RED for PgBouncer [032]. 1.8 added transaction-time and
  query-count stats, enough to compute idle-in-transaction share [035].
- `pgscv` and Prometheus exporters cover PgBouncer [168][143].

## 5. Alternatives

- **No default in core.** brandur argues a pooler should ship with Postgres
  [430]; an internal pooler is not even on the hackers' table [412].
- **PgCat** (Instacart): multi-threaded, load balancing, replica failover,
  PgBouncer-compatible admin commands, rolls back misbehaving clients instead
  of dropping them [257]. Run on production workloads for five months by then
  [257].
- **Supavisor:** load balancing, prepared statements and query cancellation
  at 1.0 [295].
- **Benchmark (Tembo)** [303]: PgBouncer wins at 10–25 clients, PgCat at
  50–100 and up to 2,500. PgBouncer fell from about 45,000 to about 28,000
  TPS; Supavisor peaked near 21,000. Supavisor used about 7 cores at 100
  clients, PgCat up to 6 at 1,250. Treat vendor benchmarks sceptically [303].
- **PgDog** (PgCat's author): Rust, multi-threaded, parses queries to route
  reads, shards, replays `SET` on the right connection, supports
  `LISTEN/NOTIFY` [360][425]. `LISTEN/NOTIFY` is acked immediately with
  at-most-once delivery, so use a durable mechanism if loss matters [378]. AGPL
  [360]. Author's PgBouncer and RDS Proxy comparisons are partisan [365][431].
- **Odyssey** (Yandex) [094], **pgagroal** [116], **Pgpool-II** [079][009],
  **RDS Proxy** [431]. Pgpool-II has more features but needs tuning;
  PgBouncer works out of the box [079].
- **Custom builds:** Figma's PgKeeper (PgBouncer is single-threaded, couldn't
  prioritize traffic) [416]; Cloudflare's PgBouncer fork [231]; Multigres'
  gateway/pooler split with per-user pools, no mode choice, and reserved/admin
  tiers [418]; Neki's per-instance sidecar [435].

## 6. Timeouts and dead connections

- Set `idle_in_transaction_session_timeout` and `statement_timeout` [398]. Put
  `statement_timeout` on roles, not globally, so DBA sessions aren't hit [259].
- `idle_session_timeout` (PG14) can kill healthy idle pooled connections.
  Prefer TCP keepalives for detecting broken ones [189]. It does help reap
  idle backends with large caches [424].
- `client_connection_check_interval` lets the server stop a query when the
  client has gone [407]. It does nothing if the network is down, so pair with
  keepalives [407].
- A Rails app saw 15-minute stalls because Linux retransmits unacknowledged
  packets up to 15 times with exponential backoff, so default kernel
  settings can hide 15-minute delays behind 5–15 s timeouts. The database
  `statement_timeout` and PgBouncer `query_wait_timeout` never fired [212].

## 7. Retry storms and reconnects

- A blocked-lock brownout filled `max_connections` at 100 and PgBouncer
  client waits. Setting `client_connection_check_interval` to 15 s still
  saturated; 2 s peaked at 30 connections; 500 ms kept client connections low
  and `CLOSE_WAIT` near zero. Enable PgBouncer peering for cancels [403].
- A fleet of EC2 instances starting together stalled the postmaster. Fix: jitter
  and fewer parallel query bursts [404].
- A failover drops connections and the app must reconnect. libpq multi-host
  connection strings help [152][409].
- Under replica timeouts, don't redirect all reads to the primary; that burns
  its connections [433].

## 8. Million-connection benchmarks

- **Supavisor:** 1,000,000 client connections onto 400 backend connections,
  about 20,000 QPS at 250,000 and 500,000 clients, scaling linearly. A second
  node relayed the other 500,000. Each client ran one query every 50 s, and
  queries paid about 2 ms extra over PgBouncer [278].
- **PgDog** claims hundreds of thousands of clients on a few server
  connections [360].

## How the advice moved

| Topic | Older | Current |
| --- | --- | --- |
| When to pool | at 200–300 connections [006][046] | past about 500 [363]; `max_connections` about 10x vCPUs [396] |
| Large `max_connections` | "few hundred, not more than 500" [070] | PG14 scaling helps [138][166] but don't set it above need [181][355] |
| Prepared statements via pooler | not possible [039][135] | PgBouncer 1.21 [287]; 1.22 `DISCARD ALL` [301] |
| Multi-core | multiple ports [108][242] | `so_reuseport` [163]; or PgCat/Supavisor/PgDog [303][425] |
| Dead-client cleanup | `idle_session_timeout` / keepalives [189] | `client_connection_check_interval` too [403][407] |
| Pooler patching | occasional crash fixes [268] | CVE wave, upgrade to 1.26+ [417][436] |
| Pooler role | cut connection count [006] | queue, route, shard [154][360][418] |
