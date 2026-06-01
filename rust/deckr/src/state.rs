use std::collections::{BTreeMap, BTreeSet};
use std::env::{self, VarError};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;

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

#[cfg(test)]
mod tests {
    use std::ffi::OsString;

    use super::*;

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
