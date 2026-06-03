use std::collections::{BTreeMap, BTreeSet};
use std::env::{self, VarError};
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_channel::mpsc::{unbounded, UnboundedSender};
use futures_core::Stream;
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::task::JoinHandle;

use crate::{Error, Result};

pub const DEFAULT_STATE_TTL_SECONDS: u64 = 30;
pub const DEFAULT_STATE_RENEWAL_INTERVAL_SECONDS: u64 = 5;
pub const DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS: u64 = 15;
pub const DEFAULT_STATE_RECONCILE_SECONDS: u64 = 300;
pub const CONCORD_TOKEN_REFRESH_SECONDS_ENV: &str = "DECKR_CONCORD_TOKEN_REFRESH_SECONDS";
pub const STATE_RECONCILE_SECONDS_ENV: &str = "DECKR_STATE_RECONCILE_SECONDS";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StateMaintenancePolicy {
    pub renewal_interval: Duration,
    pub concord_token_refresh_interval: Duration,
    pub reconcile_interval: Duration,
}

impl StateMaintenancePolicy {
    pub fn from_env() -> Result<Self> {
        Self::from_env_results(
            env::var(CONCORD_TOKEN_REFRESH_SECONDS_ENV),
            env::var(STATE_RECONCILE_SECONDS_ENV),
        )
    }

    fn from_env_results(
        concord_token_refresh_env: std::result::Result<String, VarError>,
        reconcile_env: std::result::Result<String, VarError>,
    ) -> Result<Self> {
        Ok(Self {
            renewal_interval: Duration::from_secs(DEFAULT_STATE_RENEWAL_INTERVAL_SECONDS),
            concord_token_refresh_interval: interval_from_env_result(
                CONCORD_TOKEN_REFRESH_SECONDS_ENV,
                DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
                concord_token_refresh_env,
            )?,
            reconcile_interval: interval_from_env_result(
                STATE_RECONCILE_SECONDS_ENV,
                DEFAULT_STATE_RECONCILE_SECONDS,
                reconcile_env,
            )?,
        })
    }

    #[cfg(test)]
    fn from_reconcile_env_result(
        reconcile_env: std::result::Result<String, VarError>,
    ) -> Result<Self> {
        Self::from_env_results(Err(VarError::NotPresent), reconcile_env)
    }
}

impl Default for StateMaintenancePolicy {
    fn default() -> Self {
        Self {
            renewal_interval: Duration::from_secs(DEFAULT_STATE_RENEWAL_INTERVAL_SECONDS),
            concord_token_refresh_interval: Duration::from_secs(
                DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
            ),
            reconcile_interval: Duration::from_secs(DEFAULT_STATE_RECONCILE_SECONDS),
        }
    }
}

fn interval_from_env_result(
    env_name: &str,
    default_seconds: u64,
    env_value: std::result::Result<String, VarError>,
) -> Result<Duration> {
    match env_value {
        Ok(value) => interval_from_env_value(env_name, default_seconds, &value),
        Err(VarError::NotPresent) => Ok(Duration::from_secs(default_seconds)),
        Err(VarError::NotUnicode(_)) => {
            Err(Error::Invalid(format!("{env_name} must be valid Unicode")))
        }
    }
}

fn interval_from_env_value(env_name: &str, default_seconds: u64, value: &str) -> Result<Duration> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Ok(Duration::from_secs(default_seconds));
    }
    let seconds = trimmed.parse::<u64>().map_err(|_| {
        Error::Invalid(format!(
            "{env_name} must be a positive integer number of seconds"
        ))
    })?;
    if seconds == 0 {
        return Err(Error::Invalid(format!(
            "{env_name} must be greater than zero"
        )));
    }
    Ok(Duration::from_secs(seconds))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StateStorePolicy {
    pub broker_ttl_seconds: Option<u64>,
    pub allow_write_ttl: bool,
    pub description: String,
}

impl StateStorePolicy {
    pub fn ttl(seconds: u64, description: impl Into<String>) -> Result<Self> {
        if seconds == 0 {
            return Err(Error::Invalid(
                "broker_ttl_seconds must be greater than zero".to_string(),
            ));
        }
        Ok(Self {
            broker_ttl_seconds: Some(seconds),
            allow_write_ttl: true,
            description: description.into(),
        })
    }

    pub fn persistent(description: impl Into<String>) -> Self {
        Self {
            broker_ttl_seconds: None,
            allow_write_ttl: false,
            description: description.into(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StateEntry {
    pub key: String,
    pub value: Value,
    pub revision: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum StateOperation {
    Put,
    Delete,
    Expire,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StateChange {
    pub operation: StateOperation,
    pub key: String,
    pub entry: Option<StateEntry>,
}

pub type StateWatchStream = Pin<Box<dyn Stream<Item = Result<StateChange>> + Send + 'static>>;

#[derive(Debug, Clone, PartialEq)]
pub struct PrefixObservation {
    pub entries: Vec<StateEntry>,
    pub confirmed_missing: BTreeSet<String>,
}

#[allow(async_fn_in_trait)]
pub trait StateStore: Clone + Send + Sync + 'static {
    async fn get(&self, key: &str) -> Result<Option<StateEntry>>;
    async fn items(&self, prefix: &str) -> Result<Vec<StateEntry>>;
    async fn put(&self, key: &str, value: Value, ttl: Option<u64>) -> Result<StateEntry>;
    async fn create(&self, key: &str, value: Value, ttl: Option<u64>) -> Result<StateEntry>;
    async fn update(
        &self,
        key: &str,
        value: Value,
        revision: u64,
        ttl: Option<u64>,
    ) -> Result<StateEntry>;
    async fn delete(&self, key: &str, revision: Option<u64>) -> Result<()>;
    async fn watch(&self, prefix: &str) -> Result<StateWatchStream>;
}

pub async fn observe_prefix_current<S: StateStore>(
    state: &S,
    prefix: &str,
    known_keys: impl IntoIterator<Item = String>,
) -> Result<PrefixObservation> {
    let mut observed = state
        .items(prefix)
        .await?
        .into_iter()
        .map(|entry| (entry.key.clone(), entry))
        .collect::<BTreeMap<_, _>>();
    let mut confirmed_missing = BTreeSet::new();
    for key in known_keys {
        if !key.starts_with(prefix) || observed.contains_key(&key) {
            continue;
        }
        if let Some(entry) = state.get(&key).await? {
            observed.insert(key, entry);
        } else {
            confirmed_missing.insert(key);
        }
    }
    Ok(PrefixObservation {
        entries: observed.into_values().collect(),
        confirmed_missing,
    })
}

const MAX_MATERIALIZED_SUBSCRIBERS: usize = 128;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum MaterializedStateStatus {
    Starting,
    Ready,
    Stale,
    Closed,
}

#[derive(Clone)]
pub struct MaterializedStateStore<S: StateStore> {
    state: S,
    prefix: String,
    inner: Arc<Mutex<MaterializedStateInner>>,
    watch_task: Arc<Mutex<Option<JoinHandle<()>>>>,
}

#[derive(Debug)]
struct MaterializedStateInner {
    status: MaterializedStateStatus,
    entries: BTreeMap<String, StateEntry>,
    subscribers: Vec<UnboundedSender<Result<StateChange>>>,
}

impl<S: StateStore> MaterializedStateStore<S> {
    pub async fn start(state: S, prefix: impl Into<String>) -> Result<Self> {
        let prefix = prefix.into();
        let entries = state
            .items(&prefix)
            .await?
            .into_iter()
            .map(|entry| (entry.key.clone(), entry))
            .collect::<BTreeMap<_, _>>();
        let mut watch = state.watch(&prefix).await?;
        let inner = Arc::new(Mutex::new(MaterializedStateInner {
            status: MaterializedStateStatus::Ready,
            entries,
            subscribers: Vec::new(),
        }));
        let watch_inner = inner.clone();
        let watch_prefix = prefix.clone();
        let watch_task = tokio::spawn(async move {
            while let Some(change) = watch.next().await {
                let change = match change {
                    Ok(change) => change,
                    Err(error) => {
                        let mut inner = watch_inner
                            .lock()
                            .expect("materialized state mutex poisoned");
                        inner.status = MaterializedStateStatus::Stale;
                        inner.notify(StateChange {
                            operation: StateOperation::Delete,
                            key: format!("{watch_prefix}<watch-error>"),
                            entry: Some(StateEntry {
                                key: "<error>".to_string(),
                                value: Value::String(error.to_string()),
                                revision: 0,
                            }),
                        });
                        return;
                    }
                };
                let mut inner = watch_inner
                    .lock()
                    .expect("materialized state mutex poisoned");
                inner.apply_change(change);
            }
            watch_inner
                .lock()
                .expect("materialized state mutex poisoned")
                .status = MaterializedStateStatus::Closed;
        });
        Ok(Self {
            state,
            prefix,
            inner,
            watch_task: Arc::new(Mutex::new(Some(watch_task))),
        })
    }

    pub fn status(&self) -> MaterializedStateStatus {
        self.inner
            .lock()
            .expect("materialized state mutex poisoned")
            .status
    }

    pub fn close(&self) {
        if let Some(task) = self
            .watch_task
            .lock()
            .expect("materialized watch task mutex poisoned")
            .take()
        {
            task.abort();
        }
        self.inner
            .lock()
            .expect("materialized state mutex poisoned")
            .status = MaterializedStateStatus::Closed;
    }

    pub fn get_cached(&self, key: &str) -> Result<Option<StateEntry>> {
        let inner = self
            .inner
            .lock()
            .expect("materialized state mutex poisoned");
        check_materialized_status(inner.status)?;
        Ok(inner.entries.get(key).cloned())
    }

    pub fn items_cached(&self, prefix: &str) -> Result<Vec<StateEntry>> {
        let inner = self
            .inner
            .lock()
            .expect("materialized state mutex poisoned");
        check_materialized_status(inner.status)?;
        Ok(inner
            .entries
            .iter()
            .filter_map(|(key, entry)| key.starts_with(prefix).then_some(entry.clone()))
            .collect())
    }

    pub fn cached_keys(&self) -> Result<BTreeSet<String>> {
        let inner = self
            .inner
            .lock()
            .expect("materialized state mutex poisoned");
        check_materialized_status(inner.status)?;
        Ok(inner.entries.keys().cloned().collect())
    }

    pub fn subscribe_cached(&self) -> StateWatchStream {
        let (sender, receiver) = unbounded();
        let mut inner = self
            .inner
            .lock()
            .expect("materialized state mutex poisoned");
        if inner.subscribers.len() >= MAX_MATERIALIZED_SUBSCRIBERS {
            inner.subscribers.remove(0);
        }
        inner.subscribers.push(sender);
        Box::pin(receiver)
    }

    pub async fn reconcile_snapshot(
        &self,
        known_keys: impl IntoIterator<Item = String>,
    ) -> Result<PrefixObservation> {
        let observation = observe_prefix_current(&self.state, &self.prefix, known_keys).await?;
        let mut inner = self
            .inner
            .lock()
            .expect("materialized state mutex poisoned");
        for key in &observation.confirmed_missing {
            inner.entries.remove(key);
        }
        for entry in &observation.entries {
            inner.entries.insert(entry.key.clone(), entry.clone());
        }
        inner.status = MaterializedStateStatus::Ready;
        Ok(observation)
    }

    pub async fn get_exact(&self, key: &str) -> Result<Option<StateEntry>> {
        self.state.get(key).await
    }

    pub async fn items_exact(&self, prefix: &str) -> Result<Vec<StateEntry>> {
        self.state.items(prefix).await
    }

    pub async fn put(&self, key: &str, value: Value, ttl: Option<u64>) -> Result<StateEntry> {
        let entry = self.state.put(key, value, ttl).await?;
        self.apply_entry(entry.clone());
        Ok(entry)
    }

    pub async fn create(&self, key: &str, value: Value, ttl: Option<u64>) -> Result<StateEntry> {
        let entry = self.state.create(key, value, ttl).await?;
        self.apply_entry(entry.clone());
        Ok(entry)
    }

    pub async fn update(
        &self,
        key: &str,
        value: Value,
        revision: u64,
        ttl: Option<u64>,
    ) -> Result<StateEntry> {
        let entry = self.state.update(key, value, revision, ttl).await?;
        self.apply_entry(entry.clone());
        Ok(entry)
    }

    pub async fn delete(&self, key: &str, revision: Option<u64>) -> Result<()> {
        self.state.delete(key, revision).await?;
        let mut inner = self
            .inner
            .lock()
            .expect("materialized state mutex poisoned");
        inner.apply_change(StateChange {
            operation: StateOperation::Delete,
            key: key.to_string(),
            entry: None,
        });
        Ok(())
    }

    fn apply_entry(&self, entry: StateEntry) {
        self.inner
            .lock()
            .expect("materialized state mutex poisoned")
            .apply_change(StateChange {
                operation: StateOperation::Put,
                key: entry.key.clone(),
                entry: Some(entry),
            });
    }
}

impl MaterializedStateInner {
    fn apply_change(&mut self, change: StateChange) {
        match change.operation {
            StateOperation::Put => {
                if let Some(entry) = &change.entry {
                    self.entries.insert(change.key.clone(), entry.clone());
                }
            }
            StateOperation::Delete | StateOperation::Expire => {
                self.entries.remove(&change.key);
            }
        }
        self.notify(change);
    }

    fn notify(&mut self, change: StateChange) {
        self.subscribers
            .retain(|subscriber| subscriber.unbounded_send(Ok(change.clone())).is_ok());
    }
}

fn check_materialized_status(status: MaterializedStateStatus) -> Result<()> {
    match status {
        MaterializedStateStatus::Ready => Ok(()),
        MaterializedStateStatus::Starting => Err(Error::MaterializedViewStale(
            "materialized state is still starting".to_string(),
        )),
        MaterializedStateStatus::Stale => Err(Error::MaterializedViewStale(
            "materialized state watch is stale".to_string(),
        )),
        MaterializedStateStatus::Closed => Err(Error::Closed(
            "materialized state store is closed".to_string(),
        )),
    }
}

#[derive(Debug, Clone, Default)]
pub struct MemoryStateStore {
    inner: Arc<Mutex<MemoryStateInner>>,
}

#[derive(Debug, Default)]
struct MemoryStateInner {
    entries: BTreeMap<String, StateEntry>,
    revision: u64,
    watchers: Vec<MemoryStateWatcher>,
}

#[derive(Debug)]
struct MemoryStateWatcher {
    prefix: String,
    sender: UnboundedSender<Result<StateChange>>,
}

impl MemoryStateStore {
    pub fn new() -> Self {
        Self::default()
    }

    fn next_revision(inner: &mut MemoryStateInner) -> u64 {
        inner.revision += 1;
        inner.revision
    }

    fn notify_watchers(inner: &mut MemoryStateInner, change: StateChange) {
        inner.watchers.retain(|watcher| {
            if !change.key.starts_with(&watcher.prefix) {
                return true;
            }
            watcher.sender.unbounded_send(Ok(change.clone())).is_ok()
        });
    }
}

impl StateStore for MemoryStateStore {
    async fn get(&self, key: &str) -> Result<Option<StateEntry>> {
        Ok(self
            .inner
            .lock()
            .expect("memory state mutex poisoned")
            .entries
            .get(key)
            .cloned())
    }

    async fn items(&self, prefix: &str) -> Result<Vec<StateEntry>> {
        Ok(self
            .inner
            .lock()
            .expect("memory state mutex poisoned")
            .entries
            .iter()
            .filter_map(|(key, entry)| key.starts_with(prefix).then_some(entry.clone()))
            .collect())
    }

    async fn put(&self, key: &str, value: Value, _ttl: Option<u64>) -> Result<StateEntry> {
        let mut inner = self.inner.lock().expect("memory state mutex poisoned");
        let entry = StateEntry {
            key: key.to_string(),
            value,
            revision: Self::next_revision(&mut inner),
        };
        inner.entries.insert(key.to_string(), entry.clone());
        Self::notify_watchers(
            &mut inner,
            StateChange {
                operation: StateOperation::Put,
                key: key.to_string(),
                entry: Some(entry.clone()),
            },
        );
        Ok(entry)
    }

    async fn create(&self, key: &str, value: Value, _ttl: Option<u64>) -> Result<StateEntry> {
        let mut inner = self.inner.lock().expect("memory state mutex poisoned");
        if inner.entries.contains_key(key) {
            return Err(Error::StateConflict(format!(
                "state key {key:?} already exists"
            )));
        }
        let entry = StateEntry {
            key: key.to_string(),
            value,
            revision: Self::next_revision(&mut inner),
        };
        inner.entries.insert(key.to_string(), entry.clone());
        Self::notify_watchers(
            &mut inner,
            StateChange {
                operation: StateOperation::Put,
                key: key.to_string(),
                entry: Some(entry.clone()),
            },
        );
        Ok(entry)
    }

    async fn update(
        &self,
        key: &str,
        value: Value,
        revision: u64,
        _ttl: Option<u64>,
    ) -> Result<StateEntry> {
        let mut inner = self.inner.lock().expect("memory state mutex poisoned");
        let Some(current) = inner.entries.get(key) else {
            return Err(Error::StateConflict(format!(
                "state key {key:?} is missing"
            )));
        };
        if current.revision != revision {
            return Err(Error::StateConflict(format!(
                "state key {key:?} revision changed"
            )));
        }
        let entry = StateEntry {
            key: key.to_string(),
            value,
            revision: Self::next_revision(&mut inner),
        };
        inner.entries.insert(key.to_string(), entry.clone());
        Self::notify_watchers(
            &mut inner,
            StateChange {
                operation: StateOperation::Put,
                key: key.to_string(),
                entry: Some(entry.clone()),
            },
        );
        Ok(entry)
    }

    async fn delete(&self, key: &str, revision: Option<u64>) -> Result<()> {
        let mut inner = self.inner.lock().expect("memory state mutex poisoned");
        if let Some(expected) = revision {
            let Some(current) = inner.entries.get(key) else {
                return Err(Error::StateConflict(format!(
                    "state key {key:?} is missing"
                )));
            };
            if current.revision != expected {
                return Err(Error::StateConflict(format!(
                    "state key {key:?} revision changed"
                )));
            }
        }
        let removed = inner.entries.remove(key);
        inner.revision += 1;
        if removed.is_some() {
            Self::notify_watchers(
                &mut inner,
                StateChange {
                    operation: StateOperation::Delete,
                    key: key.to_string(),
                    entry: None,
                },
            );
        }
        Ok(())
    }

    async fn watch(&self, prefix: &str) -> Result<StateWatchStream> {
        let (sender, receiver) = unbounded();
        self.inner
            .lock()
            .expect("memory state mutex poisoned")
            .watchers
            .push(MemoryStateWatcher {
                prefix: prefix.to_string(),
                sender,
            });
        Ok(Box::pin(receiver))
    }
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;

    use futures_util::StreamExt;
    use serde_json::json;

    use super::*;

    #[tokio::test]
    async fn memory_state_watch_emits_prefix_filtered_changes() {
        let state = MemoryStateStore::new();
        let mut watch = state.watch("matched.").await.unwrap();

        state
            .put("other.key", json!({"ignored": true}), None)
            .await
            .unwrap();
        let created = state
            .create("matched.key", json!({"value": 1}), None)
            .await
            .unwrap();
        let change = watch.next().await.unwrap().unwrap();
        assert_eq!(change.operation, StateOperation::Put);
        assert_eq!(change.key, "matched.key");
        assert_eq!(change.entry, Some(created.clone()));

        let updated = state
            .update("matched.key", json!({"value": 2}), created.revision, None)
            .await
            .unwrap();
        let change = watch.next().await.unwrap().unwrap();
        assert_eq!(change.operation, StateOperation::Put);
        assert_eq!(change.entry, Some(updated.clone()));

        state
            .delete("matched.key", Some(updated.revision))
            .await
            .unwrap();
        let change = watch.next().await.unwrap().unwrap();
        assert_eq!(change.operation, StateOperation::Delete);
        assert_eq!(change.key, "matched.key");
        assert_eq!(change.entry, None);
    }

    #[tokio::test]
    async fn materialized_state_store_caches_watch_and_exact_writes() {
        let state = MemoryStateStore::new();
        state
            .put("matched.initial", json!({"value": 1}), None)
            .await
            .unwrap();
        let materialized = MaterializedStateStore::start(state.clone(), "matched.")
            .await
            .unwrap();

        assert_eq!(materialized.status(), MaterializedStateStatus::Ready);
        assert_eq!(
            materialized
                .get_cached("matched.initial")
                .unwrap()
                .unwrap()
                .value,
            json!({"value": 1})
        );

        let mut cached_watch = materialized.subscribe_cached();
        let created = materialized
            .create("matched.created", json!({"value": 2}), None)
            .await
            .unwrap();
        assert_eq!(
            materialized
                .get_cached("matched.created")
                .unwrap()
                .unwrap()
                .revision,
            created.revision
        );
        let change = cached_watch.next().await.unwrap().unwrap();
        assert_eq!(change.operation, StateOperation::Put);
        assert_eq!(change.key, "matched.created");

        state
            .put("matched.external", json!({"value": 3}), None)
            .await
            .unwrap();
        tokio::time::timeout(Duration::from_secs(1), async {
            loop {
                if materialized
                    .get_cached("matched.external")
                    .unwrap()
                    .is_some()
                {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();

        materialized
            .delete("matched.created", Some(created.revision))
            .await
            .unwrap();
        assert!(materialized
            .get_cached("matched.created")
            .unwrap()
            .is_none());

        materialized.close();
        assert!(matches!(
            materialized.get_cached("matched.initial"),
            Err(Error::Closed(_))
        ));
    }

    #[test]
    fn default_state_maintenance_policy_uses_shared_defaults() {
        let policy = StateMaintenancePolicy::default();

        assert_eq!(
            policy.renewal_interval,
            Duration::from_secs(DEFAULT_STATE_RENEWAL_INTERVAL_SECONDS)
        );
        assert_eq!(
            policy.concord_token_refresh_interval,
            Duration::from_secs(DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS)
        );
        assert_eq!(
            policy.reconcile_interval,
            Duration::from_secs(DEFAULT_STATE_RECONCILE_SECONDS)
        );
    }

    #[test]
    fn concord_token_refresh_env_absent_or_blank_uses_default() {
        for env_value in [
            Err(VarError::NotPresent),
            Ok(String::new()),
            Ok("  ".to_string()),
        ] {
            let policy =
                StateMaintenancePolicy::from_env_results(env_value, Err(VarError::NotPresent))
                    .unwrap();
            assert_eq!(
                policy.concord_token_refresh_interval,
                Duration::from_secs(DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS)
            );
        }
    }

    #[test]
    fn concord_token_refresh_env_accepts_positive_seconds() {
        let policy = StateMaintenancePolicy::from_env_results(
            Ok("  20  ".to_string()),
            Err(VarError::NotPresent),
        )
        .unwrap();

        assert_eq!(
            policy.concord_token_refresh_interval,
            Duration::from_secs(20)
        );
    }

    #[test]
    fn concord_token_refresh_env_rejects_zero() {
        let error = StateMaintenancePolicy::from_env_results(
            Ok("0".to_string()),
            Err(VarError::NotPresent),
        )
        .expect_err("zero token refresh interval should fail");

        assert_invalid(error, "greater than zero");
    }

    #[test]
    fn state_reconcile_env_absent_or_blank_uses_default() {
        for env_value in [
            Err(VarError::NotPresent),
            Ok(String::new()),
            Ok("  ".to_string()),
        ] {
            let policy = StateMaintenancePolicy::from_reconcile_env_result(env_value).unwrap();
            assert_eq!(
                policy.reconcile_interval,
                Duration::from_secs(DEFAULT_STATE_RECONCILE_SECONDS)
            );
        }
    }

    #[test]
    fn state_reconcile_env_accepts_positive_seconds() {
        let policy =
            StateMaintenancePolicy::from_reconcile_env_result(Ok("  45  ".to_string())).unwrap();

        assert_eq!(policy.reconcile_interval, Duration::from_secs(45));
    }

    #[test]
    fn state_reconcile_env_rejects_zero() {
        let error = StateMaintenancePolicy::from_reconcile_env_result(Ok("0".to_string()))
            .expect_err("zero reconcile interval should fail");

        assert_invalid(error, "greater than zero");
    }

    #[test]
    fn state_reconcile_env_rejects_malformed_values() {
        for value in ["abc", "-1", "1.5"] {
            let error = StateMaintenancePolicy::from_reconcile_env_result(Ok(value.to_string()))
                .expect_err("malformed reconcile interval should fail");
            assert_invalid(error, "positive integer");
        }
    }

    #[test]
    fn state_reconcile_env_rejects_non_unicode_values() {
        let error = StateMaintenancePolicy::from_reconcile_env_result(Err(VarError::NotUnicode(
            OsString::from("not unicode"),
        )))
        .expect_err("non-unicode reconcile interval should fail");

        assert_invalid(error, "valid Unicode");
    }

    fn assert_invalid(error: Error, expected: &str) {
        match error {
            Error::Invalid(message) => assert!(
                message.contains(expected),
                "expected {message:?} to contain {expected:?}"
            ),
            other => panic!("expected invalid error, got {other:?}"),
        }
    }
}
