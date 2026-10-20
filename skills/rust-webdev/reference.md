# Rust web development: templates

These are starting shapes for the patterns in [SKILL.md](SKILL.md). Adapt the
names to the project. The APIs shown match axum 0.8, tower-http 0.7, diesel
2.3, diesel-async 0.9, deadpool 0.13, and bb8 0.9, which were current when
this file was written. Look up the latest versions (`cargo search`,
`cargo add`) and read their docs before copying. Don't pin these numbers.

## Cargo.toml (Postgres service)

```toml
[dependencies]
axum = { version = "0.8", features = ["macros"] }
tokio = { version = "1", features = ["rt-multi-thread", "macros", "net", "signal", "time", "sync", "fs"] }
tower-http = { version = "0.7", features = ["trace", "cors", "limit", "timeout", "compression-gzip", "set-header", "fs"] }
diesel = { version = "2.3", default-features = false, features = ["postgres_backend", "uuid", "time", "serde_json"] }
# Pick ONE pool feature: "deadpool" or "bb8".
diesel-async = { version = "0.9", features = ["postgres", "deadpool", "migrations"] }
deadpool = { version = "0.13", default-features = false, features = ["managed", "rt_tokio_1"] }
diesel_migrations = { version = "2.3", default-features = false }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
thiserror = "2"
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["env-filter"] }
dotenvy = "0.15"
uuid = { version = "1", features = ["v7", "serde"] }

[dev-dependencies]
tower = { version = "0.5", features = ["util"] }
http-body-util = "0.1"
```

## db/mod.rs: deadpool

```rust
use std::time::Duration;

use diesel::ConnectionError;
use diesel_async::pooled_connection::deadpool::{Object, Pool};
use diesel_async::pooled_connection::{AsyncDieselConnectionManager, ManagerConfig};
use diesel_async::{AsyncConnection, AsyncMigrationHarness, AsyncPgConnection, SimpleAsyncConnection};
use diesel_migrations::{EmbeddedMigrations, MigrationHarness, embed_migrations};

pub type DbPool = Pool<AsyncPgConnection>;
pub type DbConn = Object<AsyncPgConnection>;
type BoxError = Box<dyn std::error::Error + Send + Sync>;

pub const MIGRATIONS: EmbeddedMigrations = embed_migrations!("migrations");

pub fn pool(url: &str, size: usize, statement_timeout: Duration) -> Result<DbPool, BoxError> {
    let mut config = ManagerConfig::<AsyncPgConnection>::default();
    // Runs once per new connection: cap every statement server-side.
    config.custom_setup = Box::new(move |url| {
        let url = url.to_owned();
        Box::pin(async move {
            let mut conn = AsyncPgConnection::establish(&url).await?;
            conn.batch_execute(&format!(
                "SET statement_timeout = {}; SET idle_in_transaction_session_timeout = 10000;",
                statement_timeout.as_millis()
            ))
            .await
            .map_err(ConnectionError::CouldntSetupConfiguration)?;
            Ok(conn)
        })
    });
    let manager = AsyncDieselConnectionManager::new_with_config(url, config);
    Ok(Pool::builder(manager)
        .max_size(size)
        .wait_timeout(Some(Duration::from_secs(5))) // fail fast instead of queueing forever
        .create_timeout(Some(Duration::from_secs(5)))
        .runtime(deadpool::Runtime::Tokio1) // required for the timeouts above
        .build()?)
}

/// Dedicated connection with no statement timeout, so long index builds can finish.
/// The harness uses `block_in_place`, so this needs the multi-threaded runtime.
pub async fn migrate(url: &str) -> Result<(), BoxError> {
    let conn = AsyncPgConnection::establish(url).await?;
    let mut harness = AsyncMigrationHarness::new(conn);
    for version in harness.run_pending_migrations(MIGRATIONS)? {
        tracing::info!(%version, "applied migration");
    }
    Ok(())
}
```

## db/mod.rs: bb8 variant

Enable `diesel-async/bb8` instead of `deadpool`. `setup()` returns the same
`ManagerConfig` with `custom_setup` as above.

```rust
use diesel_async::pooled_connection::bb8::{Pool, PooledConnection, RunError};

pub type DbPool = Pool<AsyncPgConnection>;
pub type DbConn<'a> = PooledConnection<'a, AsyncPgConnection>; // or pool.get_owned() for 'static

pub async fn pool(url: &str, size: u32) -> Result<DbPool, BoxError> {
    let manager = AsyncDieselConnectionManager::<AsyncPgConnection>::new_with_config(url, setup());
    Ok(Pool::builder()
        .max_size(size)
        .connection_timeout(Duration::from_secs(5))
        .build(manager)
        .await?)
}

// error.rs
impl From<RunError> for AppError {
    fn from(err: RunError) -> Self {
        Self::internal(format!("db pool: {err}"))
    }
}
```

## error.rs

```rust
use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use diesel::result::{DatabaseErrorKind, Error as DieselError};

pub type AppResult<T> = Result<T, AppError>;

/// Client-facing variants carry a message for the user; `Internal` is logged and hidden.
#[derive(Debug, thiserror::Error)]
pub enum AppError {
    #[error("{0}")]
    BadRequest(String),
    #[error("Sign in to continue.")]
    Unauthorized,
    #[error("{0}")]
    Forbidden(String),
    #[error("Not found.")]
    NotFound,
    #[error("{0}")]
    Conflict(String),
    #[error("internal error: {0}")]
    Internal(String),
}

impl AppError {
    pub fn internal(err: impl std::fmt::Display) -> Self {
        Self::Internal(err.to_string())
    }

    fn status(&self) -> StatusCode {
        match self {
            Self::BadRequest(_) => StatusCode::BAD_REQUEST,
            Self::Unauthorized => StatusCode::UNAUTHORIZED,
            Self::Forbidden(_) => StatusCode::FORBIDDEN,
            Self::NotFound => StatusCode::NOT_FOUND,
            Self::Conflict(_) => StatusCode::CONFLICT,
            Self::Internal(_) => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let message = match &self {
            Self::Internal(detail) => {
                tracing::error!(%detail, "request failed");
                "Something went wrong. Try again in a moment.".to_owned()
            }
            other => other.to_string(),
        };
        (self.status(), Json(serde_json::json!({ "error": message }))).into_response()
    }
}

impl From<DieselError> for AppError {
    fn from(err: DieselError) -> Self {
        match err {
            DieselError::NotFound => Self::NotFound,
            DieselError::DatabaseError(DatabaseErrorKind::UniqueViolation, _) => {
                Self::Conflict("That already exists.".into())
            }
            other => Self::internal(other),
        }
    }
}

impl From<diesel_async::pooled_connection::deadpool::PoolError> for AppError {
    fn from(err: diesel_async::pooled_connection::deadpool::PoolError) -> Self {
        Self::internal(format!("db pool: {err}"))
    }
}

impl From<tokio::task::JoinError> for AppError {
    fn from(err: tokio::task::JoinError) -> Self {
        Self::internal(err)
    }
}
```

## config.rs

```rust
use std::env;
use std::net::SocketAddr;

#[derive(Clone, Debug)]
pub struct Config {
    pub database_url: String,
    pub pool_size: usize,
    pub bind_addr: SocketAddr,
    pub public_origin: String,
    pub cookie_secure: bool,
    /// Off only in tests.
    pub rate_limit: bool,
    pub run_worker: bool,
}

#[derive(Debug, thiserror::Error)]
#[error("config: {0}")]
pub struct ConfigError(String);

impl Config {
    pub fn from_env() -> Result<Self, ConfigError> {
        Ok(Self {
            database_url: env::var("DATABASE_URL")
                .map_err(|_| ConfigError("DATABASE_URL is not set".into()))?,
            pool_size: parse("DATABASE_POOL_SIZE", "16")?,
            bind_addr: parse("BIND_ADDR", "127.0.0.1:8080")?,
            public_origin: var_or("PUBLIC_ORIGIN", "http://localhost:5173"),
            cookie_secure: parse("COOKIE_SECURE", "true")?,
            rate_limit: parse("RATE_LIMIT", "true")?,
            run_worker: parse("RUN_WORKER", "true")?,
        })
    }
}

fn var_or(key: &str, default: &str) -> String {
    env::var(key).unwrap_or_else(|_| default.to_owned())
}

fn parse<T: std::str::FromStr>(key: &str, default: &str) -> Result<T, ConfigError> {
    let raw = var_or(key, default);
    raw.parse().map_err(|_| ConfigError(format!("{key}={raw:?} is not valid")))
}
```

## lib.rs and main.rs

```rust
// lib.rs
#[derive(Clone)]
pub struct AppState {
    pub db: db::DbPool,
    pub config: Arc<Config>,
}

/// The whole HTTP app. Separate from `main` so tests can drive it.
pub fn app(state: AppState) -> Router {
    routes::router()
        .layer(SetResponseHeaderLayer::overriding(
            header::X_CONTENT_TYPE_OPTIONS,
            HeaderValue::from_static("nosniff"),
        ))
        .layer(RequestBodyLimitLayer::new(64 * 1024))
        .layer(TimeoutLayer::with_status_code(StatusCode::REQUEST_TIMEOUT, Duration::from_secs(10)))
        .layer(CompressionLayer::new())
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

// main.rs
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    dotenvy::dotenv().ok();
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| "my_app=info,tower_http=info".into()),
        )
        .init();

    let config = Config::from_env()?;
    db::migrate(&config.database_url).await?;
    let pool = db::pool(&config.database_url, config.pool_size, Duration::from_secs(5))?;
    let state = AppState { db: pool, config: Arc::new(config) };
    if state.config.run_worker {
        worker::spawn(state.clone());
    }

    let listener = TcpListener::bind(state.config.bind_addr).await?;
    tracing::info!(addr = %state.config.bind_addr, "listening");
    axum::serve(listener, app(state))
        .with_graceful_shutdown(async { tokio::signal::ctrl_c().await.ok(); })
        .await?;
    Ok(())
}
```

## A feature module

```rust
// routes/notes.rs
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/", post(create))
        .route("/{id}", get(show).patch(edit))
}

#[derive(Deserialize)]
pub struct NewNote {
    body: String,
}

async fn create(
    State(state): State<AppState>,
    user: AuthUser,
    Json(input): Json<NewNote>,
) -> AppResult<(StatusCode, Json<serde_json::Value>)> {
    let body = notes::validate_body(&input.body)?; // pure rule, no HTTP or DB
    let mut conn = state.db.get().await?;
    let id = Uuid::now_v7();
    diesel::insert_into(schema::notes::table)
        .values((schema::notes::id.eq(id), schema::notes::author_id.eq(user.id), schema::notes::body.eq(&body)))
        .execute(&mut conn)
        .await?;
    Ok((StatusCode::CREATED, Json(json!({ "id": id }))))
}

/// Conditional write: the WHERE clause is the permission check. Explain only on failure.
async fn edit(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    user: AuthUser,
    Json(input): Json<NewNote>,
) -> AppResult<StatusCode> {
    let body = notes::validate_body(&input.body)?;
    let mut conn = state.db.get().await?;
    let updated = diesel::update(
        schema::notes::table.filter(schema::notes::id.eq(id)).filter(schema::notes::author_id.eq(user.id)),
    )
    .set(schema::notes::body.eq(&body))
    .execute(&mut conn)
    .await?;
    if updated == 1 {
        return Ok(StatusCode::NO_CONTENT);
    }
    let exists = diesel::select(diesel::dsl::exists(schema::notes::table.filter(schema::notes::id.eq(id))))
        .get_result::<bool>(&mut conn)
        .await?;
    Err(if exists { AppError::Forbidden("You can only edit your own notes.".into()) } else { AppError::NotFound })
}
```

## Extractor, blocking work, worker

```rust
impl FromRequestParts<AppState> for AuthUser {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, state: &AppState) -> Result<Self, Self::Rejection> {
        let jar = CookieJar::from_headers(&parts.headers);
        let token = jar.get(SESSION_COOKIE).ok_or(AppError::Unauthorized)?.value();
        load_session(state, &hash_token(token)).await?.ok_or(AppError::Unauthorized)
    }
}

/// Argon2 takes tens of ms of CPU: keep it off the runtime workers.
pub async fn hash_password(password: String) -> AppResult<String> {
    tokio::task::spawn_blocking(move || argon2_hash(&password)).await?
}

pub fn spawn(state: AppState) {
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs(30));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tick.tick().await;
            if let Err(err) = run_once(&state).await {
                tracing::warn!(error = %err, "worker pass failed");
            }
        }
    });
}
```

## tests/common/mod.rs

```rust
pub struct TestApp {
    pub router: Router,
    pub state: AppState,
    admin_url: String,
    db_name: String,
}

impl TestApp {
    /// Fresh, migrated database per test. Needs DATABASE_URL for a role with CREATEDB.
    pub async fn new() -> Self {
        dotenvy::dotenv().ok();
        let admin_url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set for tests");
        let db_name = format!("test_{}", Uuid::now_v7().simple());
        let mut admin = AsyncPgConnection::establish(&admin_url).await.unwrap();
        admin.batch_execute(&format!("CREATE DATABASE {db_name}")).await.unwrap();

        let (base, _) = admin_url.rsplit_once('/').unwrap();
        let config = Config {
            database_url: format!("{base}/{db_name}"),
            pool_size: 4,
            bind_addr: "127.0.0.1:0".parse().unwrap(),
            public_origin: "http://localhost:5173".into(),
            cookie_secure: false,
            rate_limit: false,
            run_worker: false,
        };
        db::migrate(&config.database_url).await.unwrap();
        let pool = db::pool(&config.database_url, config.pool_size, Duration::from_secs(5)).unwrap();
        let state = AppState { db: pool, config: Arc::new(config) };
        Self { router: app(state.clone()), state, admin_url, db_name }
    }

    pub async fn call(&self, req: Request<Body>) -> (StatusCode, serde_json::Value) {
        let res = self.router.clone().oneshot(req).await.unwrap();
        let status = res.status();
        let bytes = res.into_body().collect().await.unwrap().to_bytes();
        (status, serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null))
    }

    /// Call at the end of each test.
    pub async fn cleanup(self) {
        let Self { router, state, admin_url, db_name } = self;
        drop(router);
        state.db.close();
        drop(state);
        let mut admin = AsyncPgConnection::establish(&admin_url).await.unwrap();
        admin
            .batch_execute(&format!("DROP DATABASE IF EXISTS {db_name} WITH (FORCE)"))
            .await
            .unwrap();
    }
}
```
