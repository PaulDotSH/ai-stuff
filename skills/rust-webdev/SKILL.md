---
name: rust-webdev
description: >
  Build and review Rust web backends the house way: axum 0.8 on tokio,
  tower-http middleware, a thiserror AppError that maps to HTTP, config
  from env into a plain struct, diesel-async on Postgres through an async
  pool (deadpool or bb8) with timeouts, sqlx/SQLite for small apps,
  sailfish templates, tracing, router-level integration tests, and
  a `--profile production` deploy build (fat LTO, target-cpu). Requires
  checking the latest crate versions and their docs instead of versions
  from memory. Steers toward plain data plus free functions and small
  modules instead of OOP layering, and toward performance: no blocking on
  the runtime, spawn_blocking for CPU work, few clones, sized pools. Use
  when creating, extending, refactoring, or reviewing a Rust HTTP service,
  API, or server-rendered site (axum, actix-web, diesel, sqlx, tokio).
---

# Rust web development

A playbook for Rust web services. Code templates (pool, error, config, test
harness) are in [reference.md](reference.md). For schema and migration
design, use `postgres-schema-design` and `postgres-safe-migrations`. For pool
sizing on the database side, use `postgres-connection-pooling`.

If the project already has conventions, follow them over this file. Read
`Cargo.toml`, `src/lib.rs`, the error module and one route module before
writing code.

## Check current versions and docs first

The Rust ecosystem moves fast, and your training data lags behind it. Models
reliably reach for old major versions and old APIs, for example axum 0.6
`:id` paths, `#[async_trait]` extractors, `Server::bind`, or pre-2.0 diesel.
Every version in this file, including the templates, was current when it was
written and may be out of date now.

- **Before adding or upgrading a dependency, look up its latest version.** Run
  `cargo search <crate> --limit 1` or `cargo info <crate>`, or check
  crates.io. Add it with `cargo add <crate>` (add `-F feature` as needed) so
  Cargo writes the current version. Don't type a version from memory.
- **Read the docs for the version you actually use** before writing code
  against an API you haven't checked in this session. Use docs.rs at that
  exact version, the crate's CHANGELOG and examples, and the source in
  `~/.cargo/registry/src/` after a build. Read the migration guide when a
  major version is involved.
- If code from memory fails to compile against the current version, read the
  current docs. Don't downgrade the crate to fit the code.
- Use the newest stable toolchain and edition (`edition = "2024"` or later)
  for new projects. Run `rustup update` and `cargo update` before large
  changes. Check a release's changelog before using a feature you think is
  new.

## Default stack

Pick these unless the project already chose otherwise:

| Concern        | Choice |
| -------------- | ------ |
| Runtime        | `tokio` (multi-thread; `#[tokio::main(worker_threads = 2)]` for small single-purpose services) |
| HTTP           | `axum` 0.8 (`/{id}` path syntax, `axum::serve`, native async extractors without `async_trait`) |
| Middleware     | `tower-http`: trace, timeout, body limit, compression, CORS, set-header, request-id, `ServeDir` |
| Postgres       | `diesel` + `diesel-async` with the pure-Rust `tokio-postgres` backend (no libpq), pooled by `deadpool` or `bb8` |
| SQLite / small | `sqlx` with `runtime-tokio` + `sqlite` + `migrate` |
| Templates      | `sailfish` (+ `sailfish-minify`) for server-rendered HTML |
| Errors         | `thiserror` for the app and library error enums; `anyhow` only in `main`, CLIs, and tiny binaries |
| Logging        | `tracing` + `tracing-subscriber` with `EnvFilter` |
| HTTP client    | `reqwest` with `default-features = false` and rustls |
| IDs and time   | `uuid` v7 (sortable) for public IDs; `time` or `chrono` with serde |
| Passwords      | `argon2` (Argon2id), hashed on the blocking pool |
| Rate limits    | `governor` / `tower_governor`; Redis when there are several instances |
| Caches         | `moka` for in-process TTL caches |
| Config         | `dotenvy` + env vars into a typed struct; TOML file when config is nested |

Don't add a crate for what the standard library or an existing dependency
already does. Set `default-features = false` and list the features you use on
heavy crates (`reqwest`, `diesel`, `tokio` in libraries, `rustls`).

## Database: async pool when performance matters

When throughput or latency matters, use `diesel-async` on `tokio` with an async
connection pool. Never use sync `diesel` with `r2d2` inside handlers: each
query would block a runtime worker.

- **deadpool** (`diesel-async` feature `deadpool`): the builder is sync and
  connections are `Object<AsyncPgConnection>`. Set `.wait_timeout(...)` and
  `.create_timeout(...)`, which need `.runtime(deadpool::Runtime::Tokio1)`.
- **bb8** (feature `bb8`): the builder is async (`.build(manager).await`).
  Set `.max_size(n)` and `.connection_timeout(...)`. Use `pool.get_owned()`
  when the connection must outlive the borrow, for example when it moves into
  a spawned task.
- Both take a `ManagerConfig` whose `custom_setup` runs once per new
  connection. Use it to `SET statement_timeout` and
  `idle_in_transaction_session_timeout`, and to attach a slow-query
  `Instrumentation`.

Pool rules:

1. **Bound everything.** Set `max_size` from config, with a default around
   10–20. Every request needs a wait timeout so it fails fast instead of
   queueing forever. Set a server-side `statement_timeout` on every pooled
   connection too.
2. **Size the pool for the database, not for the request rate.** Total
   connections = instances × `max_size`. Keep that under Postgres
   `max_connections` minus headroom. A few connections per database core is
   usually the sweet spot. Put PgBouncer in front if you need more.
3. **Hold a connection for as short a time as possible.** Get it, run the
   queries, and drop it before any slow I/O such as HTTP calls, email, or
   hashing. Never hold one across an external `await` you don't control.
4. **Run migrations on a dedicated connection at startup** (embedded with
   `embed_migrations!`), not through the pool with its statement timeout.
5. Give background workers either a separate small pool or a share of the
   main pool's budget, so they can't starve requests.

Query style:

- Use typed Diesel DSL for normal CRUD. Use `diesel::sql_query` with
  `.bind::<SqlType, _>()` and `#[derive(QueryableByName)]` rows when a CTE,
  `UPDATE ... FROM`, `FOR UPDATE SKIP LOCKED`, or `RETURNING` reads better as
  SQL. Always bind values; never `format!` user input into SQL.
- Prefer one set-based statement over a loop of queries. Do conditional
  writes in the `WHERE` clause and check the affected row count, instead of
  read-check-write races.
- When a write affects 0 rows, re-load the row only on that path to explain
  why (not found, forbidden, or conflict).
- Map `diesel::result::Error::NotFound` to 404 and unique violations to 409 in
  one `From` impl, not in every handler.
- Store small enums as `SMALLINT` with `ToSql`/`FromSql` impls (or `strum`
  for string forms), not as free-form text.

## Project layout

Single crate for one service:

```
src/
  main.rs        # tracing init, config, migrate, pool, serve, graceful shutdown; nothing else
  lib.rs         # pub mods, AppState, `pub fn app(state) -> Router` (tests call this)
  config.rs      # Config::from_env(), defaults, ConfigError
  error.rs       # AppError, AppResult<T>, IntoResponse, From impls
  db/mod.rs      # pool(), migrate(), DbPool/DbConn aliases, SQL helpers
  db/schema.rs   # generated by diesel CLI
  routes/mod.rs  # router() that nests each feature's router
  routes/<feature>.rs
  <domain>.rs    # rules with no HTTP in them: validation, scoring, state transitions
  worker.rs      # background loops
tests/
  common/mod.rs  # TestApp harness
  <feature>.rs
```

Workspace once the service has several deployables or a large domain:
`crates/shared` (config, error, ids), `crates/db`, `crates/core` (domain,
transport-agnostic), `crates/web` (the composition root: router, middleware,
templates), `crates/worker`. Domain crates never import axum. Put shared
versions in `[workspace.dependencies]` and reference them with
`{ workspace = true }`.

- **Organize by feature, not by layer.** `routes/stories.rs` owns its router,
  request and response types, and handlers. `stories.rs` owns the rules.
  Don't create `controllers/`, `services/`, and `repositories/` trees that
  split one feature across three folders.
- Each feature exposes `pub fn router() -> Router<AppState>`, and
  `routes::router()` nests them. Apply route-group layers such as strict rate
  limits on auth where the group is built.
- Keep `main.rs` thin. Everything a test needs must be reachable from
  `lib.rs`.
- For unit tests next to the code, use `#[cfg(test)] mod tests;` with a
  sibling `foo/tests.rs` file once they grow past a screenful.

## Code shape: data and functions, not object hierarchies

- **Plain data plus free functions.** A handler is an `async fn` that takes
  extractors and returns `AppResult<impl IntoResponse>`. Domain logic is
  `fn validate_body(&str) -> AppResult<String>`, not
  `StoryValidator::new().validate()`.
- **No manager, service, or repository objects by default.** Don't create a
  `UserService` that wraps a `UserRepository` that wraps the pool. A function
  that takes `&mut AsyncPgConnection` is the repository.
- **Add a trait only when there are two real implementations today,** for
  example a Redis session store with a Postgres fallback, or a real payment
  provider and a sandbox one. Use `Arc<dyn Trait>` in state for those. Don't
  add a trait just to mock in tests: test against a real database.
- **No trait hierarchies, builder-for-everything, or getters and setters on
  plain structs.** Use public fields on data types. Use `impl` blocks only for
  real invariants or convenience constructors.
- **Keep functions short and modules small.** A function does one thing and
  fits on a screen. Split a module when it has two reasons to change, not
  when it reaches some line count.
- Use enums and `match` for states and roles. Use `let ... else` for early
  returns. Make illegal states unrepresentable instead of checking them in
  three places.
- Name request and response structs after the operation (`Edit`, `NewStory`,
  `StoryView`). Derive only what you use.
- Write comments that explain *why*: a trade-off, an invariant, a security
  reason. Skip comments that restate the code.

## Shared state

- One `#[derive(Clone)] struct AppState` with cheap-to-clone fields: the pool
  (already `Arc` inside), `Arc<Config>`, caches, clients (`reqwest::Client` is
  `Arc` inside), and `Arc<dyn Trait>` for swappable backends. Pass it with
  `.with_state(state)` and extract it with `State(state)`.
- Optional integrations (SMTP, Stripe, OAuth, Redis) are `Option<...>` in
  config and state. Log at startup whether each one is on, and degrade
  gracefully when it's off: write mail to the log, fall back to Postgres
  sessions.
- Use `std::sync::Mutex` for short critical sections that never cross an
  `.await`. Use `tokio::sync::Mutex` only when the lock must be held across an
  `.await`. Prefer `moka`, `DashMap`, or message passing over a global lock on
  hot paths.

## Errors

- One `AppError` enum, derived with `thiserror`, plus
  `type AppResult<T> = Result<T, AppError>`.
- Client-facing variants carry a message written for the user: `BadRequest`,
  `Unauthorized`, `Forbidden`, `NotFound`, `Conflict`, `Validation`, and
  `TooManyRequests`. `Internal(String)` is logged with `tracing::error!` and
  replaced with a generic message in the response.
- `impl IntoResponse for AppError` maps each variant to a status code and a
  JSON body `{ "error": message }`, or to an HTML error page for
  server-rendered sites.
- Write `From` impls for diesel errors, pool errors (`deadpool::PoolError` or
  `bb8::RunError`), and `JoinError`, so handlers just use `?`. Add an
  `AppError::internal(impl Display)` helper for everything else.
- In a workspace, each layer has its own error (`DbError`), and that error
  converts into `AppError` at the boundary.
- No `unwrap()` or `expect()` on request paths. `expect` is fine at startup
  for invariants, with a message that names the fix ("PUBLIC_ORIGIN must be a
  valid header value").

## Config

- A plain `Config` struct filled by `Config::from_env()`. Call
  `dotenvy::dotenv().ok()` first. Use small `var_or` and `parse::<T>` helpers
  that return a `ConfigError` naming the bad key and value.
- Every value has a safe default, except secrets and `DATABASE_URL`, which
  are required.
- Validate at startup: parse lists, check that related keys come together
  (a webhook secret is required when the payment key is set), and fail before
  binding the port.
- Store the config as `Arc<Config>` in state. Tests build a `Config` literal
  directly and don't touch the environment.
- Ship `.env.example`. Never commit `.env` or secrets.

## Async and performance

1. **Never block a runtime worker.** Run password hashing, image or SVG
   rendering, compression, big JSON or regex work, and sync file or DB APIs
   in `tokio::task::spawn_blocking`. Move owned data in, `.await` the
   `JoinHandle`, and convert `JoinError` with `?`. For long-running CPU jobs,
   use a dedicated thread pool or a `rayon` pool behind a channel.
2. **Use `tokio::fs` or `tokio::io` in handlers.** Stream large bodies with
   `ReaderStream` or `Body::from_stream` instead of reading them into memory.
3. **Avoid needless clones and allocations.** Borrow (`&str`, `&[T]`) in
   function parameters. Take ownership only when you store or move the value.
   Clone the `Arc`, not the data. Use `Cow` when a value is only sometimes
   modified. Use `Vec::with_capacity` when the size is known. Use
   `format_args!` and tracing fields instead of building `String`s you only
   log. Use `Bytes` for bodies you pass through.
4. **Run independent awaits concurrently** with `tokio::join!` or
   `try_join!`. For fan-out, use `futures::stream::iter(..).buffer_unordered(n)`
   with an explicit limit, never an unbounded `join_all` over user input.
5. **Bound every external call.** Put timeouts on the `reqwest::Client`, the
   request (`TimeoutLayer`), the body (`RequestBodyLimitLayer`, 64 KiB
   default; raise it per upload route), the DB statement, and pool waits.
6. **Use `LazyLock` or `OnceLock`** for compiled regexes, templates, and
   dummy hashes built once per process.
7. **Run background work as a `tokio::spawn` loop** on an
   `interval(...)` with `MissedTickBehavior::Skip`. Each job is a set-based,
   idempotent statement (with `FOR UPDATE SKIP LOCKED` when it claims rows),
   so several instances can run it safely. Log failures and keep looping.
8. **Measure before you optimize.** Use `criterion` benches for hot paths,
   and a `release-with-debug` profile for flamegraphs.

## Middleware and security

Build the stack in `app()`, from innermost to outermost: routes, then
security headers (`X-Content-Type-Options: nosniff`, `Referrer-Policy`, CSP
for HTML), body limit, timeout, compression, CORS (an explicit origin from
config, never `*` with credentials), and `TraceLayer` last.

- Sessions: a random 256-bit token in an `HttpOnly`, `SameSite=Lax` cookie,
  marked `Secure` when configured. Store only its SHA-256 hash. Cache
  validated sessions briefly in `moka`, and invalidate the cache on logout,
  ban, role change, and password change.
- Use an `AuthUser` extractor (`FromRequestParts`), and implement
  `OptionalFromRequestParts` for routes where login is optional. Put
  permission checks on it as methods (`user.require_writer()?`).
- Make login cost the same whether or not the account exists: verify a dummy
  hash for unknown users. Rate-limit auth routes more strictly than the rest.
- Compare secrets and HMACs in constant time (`subtle`, `hmac::verify_slice`).
- Trust `X-Forwarded-For` only behind a proxy you configured
  (`TRUST_PROXY`).
- Never log bind values, tokens, passwords, or full emails. Slow-query logs
  print the SQL text only.

## Observability

- Initialize `tracing_subscriber::fmt()` with
  `EnvFilter::try_from_default_env()`, falling back to
  `"<crate>=info,tower_http=info"`. Use JSON output in production if logs are
  shipped somewhere.
- Use structured fields (`tracing::info!(addr = %bind, "listening")`), not
  interpolated strings.
- Add a `/health` route that checks the database (`SELECT 1`) and returns
  `204`.
- Use `axum::serve(...).with_graceful_shutdown(signal)`. Use
  `into_make_service_with_connect_info::<SocketAddr>()` when handlers need
  the client IP.

## Testing

- **Integration tests drive the real router.** `lib.rs` exposes `app(state)`,
  and tests call `router.oneshot(request)` with `tower::ServiceExt`, then read
  the body with `http_body_util::BodyExt`. No HTTP server and no ports.
- **Give each test its own real database.** The harness creates
  `test_<uuid>`, migrates it, builds state from a `Config` literal (rate
  limits off, worker off, external APIs pointed at a dead port or a local fake
  server), and drops the database with `WITH (FORCE)` at the end. For SQLite,
  use a temp file or `:memory:`.
- Keep the helpers in `tests/common/mod.rs`: `TestApp::new()`, `new_with(|cfg|
  ...)`, `call(TestRequest)`, `sql("...")` for setup, and `register()` /
  `confirmed()` for auth. Test helpers are free functions that tests compose.
- Fake time by updating timestamps in SQL and then calling the worker
  function directly. Don't sleep.
- Assert the status first and print the body on failure:
  `assert_eq!(res.status, StatusCode::CREATED, "{:?}", res.json)`.
- Unit-test pure domain functions without a database. Gate tests that need
  external tools behind a cargo feature (`browser-tests = []`) so the default
  suite runs anywhere.

## Build profiles

```toml
[profile.release]
opt-level = 3
lto = "fat"
codegen-units = 1
panic = "abort"      # drop this if you rely on catch-panic middleware or unwinding
strip = "symbols"

[profile.dev.package."*"]
opt-level = 2        # fast deps in debug builds; your crate stays quick to compile

# Deployment builds: `cargo build --profile production`
[profile.production]
inherits = "release"
lto = "fat"
codegen-units = 1
opt-level = 3
strip = "symbols"
```

### Production builds

Every web project gets a `[profile.production]` like the one above, and
deployment builds use it with the CPU target set to the machine that will run
the binary:

```bash
RUSTFLAGS="-C target-cpu=native" cargo build --profile production
```

- The binary lands in `target/production/<name>`, not `target/release/`.
  Update the deploy scripts, Dockerfiles, and systemd units to match.
- `target-cpu=native` tunes for the CPU of the machine that runs the build.
  Build on the deploy host itself, or on identical hardware. When you build on
  a CI runner or another machine, name the server's CPU instead (for example
  `-C target-cpu=x86-64-v3`, or the exact model from `rustc --print
  target-cpus`). A binary built for a newer CPU crashes with "illegal
  instruction" on an older one.
- Set the flag in the deploy command or script, not in a global
  `.cargo/config.toml`. It shouldn't affect dev builds, tests, or binaries
  built for other machines.
- **When you create or set up a project, add these rules to its agent
  instructions file** (`AGENTS.md`, or whatever the repo uses; with
  `CLAUDE.md` importing it), so every later session deploys the same way:

  ```markdown
  ## Deployment builds

  - Build for deployment with `RUSTFLAGS="-C target-cpu=native" cargo build --profile production`.
    Never deploy a `--release` or debug build.
  - `production` inherits `release` and adds fat LTO, `codegen-units = 1`, and
    stripped symbols. The binary is at `target/production/<name>`.
  - `target-cpu=native` targets the build machine's CPU. Build on the deploy
    host, or replace `native` with the server's CPU (for example `x86-64-v3`).
  ```

Pin the toolchain in `rust-toolchain.toml` with `rustfmt` and `clippy`. Keep
`cargo fmt` and `cargo clippy --all-targets -- -D warnings` clean.

## Checklist before you finish

- [ ] Handlers are short `async fn`s returning `AppResult`, and rules live in
      HTTP-free functions.
- [ ] No new service, manager, or repository object or trait without two
      real implementations.
- [ ] Nothing blocking or CPU-heavy runs on a runtime worker, and
      `spawn_blocking` is used where needed.
- [ ] Pool and client calls have size limits and timeouts, and no connection
      is held across slow I/O.
- [ ] SQL is bound, set-based where possible, and conditional writes check
      rows affected.
- [ ] Errors go through `AppError`. Internal details are logged, never
      returned to the client.
- [ ] No secrets or bind values in logs. New config has a default or fails
      at startup.
- [ ] No gratuitous `.clone()`, `.to_string()`, or `collect()` on hot paths.
- [ ] An integration test through `app()` covers the new route, including
      the failure statuses.
- [ ] New or changed dependencies are on their latest version, added with
      `cargo add`, and the code matches that version's docs.
- [ ] The project has `[profile.production]`, and its agent file says to
      deploy with `--profile production` and `target-cpu`.
- [ ] `cargo fmt`, `cargo clippy --all-targets`, and `cargo test` pass.
