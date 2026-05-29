use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{Error, Result};

pub const DEFAULT_STATE_TTL_SECONDS: u64 = 30;
pub const DEFAULT_STATE_RENEWAL_INTERVAL_SECONDS: u64 = 5;

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

#[derive(Debug, Clone, Default)]
pub struct MemoryStateStore {
    inner: Arc<Mutex<MemoryStateInner>>,
}

#[derive(Debug, Default)]
struct MemoryStateInner {
    entries: BTreeMap<String, StateEntry>,
    revision: u64,
}

impl MemoryStateStore {
    pub fn new() -> Self {
        Self::default()
    }

    fn next_revision(inner: &mut MemoryStateInner) -> u64 {
        inner.revision += 1;
        inner.revision
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
        inner.entries.remove(key);
        inner.revision += 1;
        Ok(())
    }
}
