use std::collections::BTreeMap;
use std::fmt::Display;
use std::time::Duration;

use async_nats::jetstream::kv::{Config as KvConfig, Entry, Operation, Store, Watch, WatcherError};
use async_nats::jetstream::Context as JetStreamContext;
use async_nats::{HeaderMap, Message, Subscriber};
use futures_util::future::{select, Either};
use futures_util::{pin_mut, StreamExt, TryStreamExt};
use serde_json::Value;

use crate::beacon::{beacon_advertisement_store_policy, DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME};
use crate::concord::{
    concord_contract_store_policy, concord_token_store_policy, DEFAULT_CONCORD_CONTRACT_STORE_NAME,
    DEFAULT_CONCORD_TOKEN_STORE_NAME,
};
use crate::keys::concord_contracts_prefix;
use crate::lanes::{headers_for, validate_subject_hint, DeckrMessage, HARDWARE_MESSAGES_LANE};
use crate::state::{StateEntry, StateStore, StateStorePolicy};
use crate::{Error, Result};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConcordStateChangeSource {
    Contracts,
    Tokens,
}

impl ConcordStateChangeSource {
    pub fn reason(self) -> &'static str {
        match self {
            Self::Contracts => "contract watch",
            Self::Tokens => "token watch",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConcordStateChange {
    pub source: ConcordStateChangeSource,
    pub key: String,
}

pub struct ConcordStateChangeStream {
    contracts: Watch,
    tokens: Watch,
}

impl ConcordStateChangeStream {
    pub async fn next(&mut self) -> Result<ConcordStateChange> {
        let contracts = self.contracts.next();
        let tokens = self.tokens.next();
        pin_mut!(contracts);
        pin_mut!(tokens);

        match select(contracts, tokens).await {
            Either::Left((entry, _)) => {
                map_concord_watch_entry(ConcordStateChangeSource::Contracts, entry)
            }
            Either::Right((entry, _)) => {
                map_concord_watch_entry(ConcordStateChangeSource::Tokens, entry)
            }
        }
    }
}

fn map_concord_watch_entry(
    source: ConcordStateChangeSource,
    entry: Option<std::result::Result<Entry, WatcherError>>,
) -> Result<ConcordStateChange> {
    match entry {
        Some(Ok(entry)) => Ok(ConcordStateChange {
            source,
            key: entry.key,
        }),
        Some(Err(error)) => Err(Error::StateUnavailable(format!(
            "watching Concord state via {}: {error}",
            source.reason()
        ))),
        None => Err(Error::StateUnavailable(format!(
            "Concord state watch ended via {}",
            source.reason()
        ))),
    }
}

#[derive(Debug, Clone)]
pub struct NatsDeckrRuntime {
    client: async_nats::Client,
    beacon_advertisements: NatsStateStore,
    concord_contracts: NatsStateStore,
    concord_tokens: NatsStateStore,
}

impl NatsDeckrRuntime {
    pub async fn connect(url: &str) -> Result<Self> {
        Self::connect_with_buckets(
            url,
            DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME,
            DEFAULT_CONCORD_CONTRACT_STORE_NAME,
            DEFAULT_CONCORD_TOKEN_STORE_NAME,
        )
        .await
    }

    pub async fn connect_with_buckets(
        url: &str,
        beacon_advertisement_bucket: &str,
        concord_contract_bucket: &str,
        concord_token_bucket: &str,
    ) -> Result<Self> {
        let client = async_nats::connect(url).await.map_err(|error| {
            Error::StateUnavailable(format!("connecting to NATS {url}: {error}"))
        })?;
        let jetstream = async_nats::jetstream::new(client.clone());
        let beacon_advertisements = open_state_bucket(
            &jetstream,
            beacon_advertisement_bucket,
            beacon_advertisement_store_policy(),
        )
        .await?;
        let concord_contracts = open_state_bucket(
            &jetstream,
            concord_contract_bucket,
            concord_contract_store_policy(),
        )
        .await?;
        let concord_tokens = open_state_bucket(
            &jetstream,
            concord_token_bucket,
            concord_token_store_policy(),
        )
        .await?;
        Ok(Self {
            client,
            beacon_advertisements,
            concord_contracts,
            concord_tokens,
        })
    }

    pub fn beacon_advertisements(&self) -> &NatsStateStore {
        &self.beacon_advertisements
    }

    pub fn concord_contracts(&self) -> &NatsStateStore {
        &self.concord_contracts
    }

    pub fn concord_tokens(&self) -> &NatsStateStore {
        &self.concord_tokens
    }

    pub async fn publish(&self, message: &DeckrMessage) -> Result<()> {
        let subject = crate::lanes::subject_for(message)?;
        self.client
            .publish_with_headers(
                subject,
                nats_headers_for(message),
                serde_json::to_vec(message)?.into(),
            )
            .await
            .map_err(|error| Error::StateUnavailable(format!("publishing lane message: {error}")))
    }

    pub async fn subscribe_hardware_messages(&self) -> Result<Subscriber> {
        self.client
            .subscribe(format!(
                "{}.{}.>",
                crate::lanes::LANE_SUBJECT_PREFIX,
                crate::keys::encode_key_token(HARDWARE_MESSAGES_LANE)
            ))
            .await
            .map_err(|error| {
                Error::StateUnavailable(format!("subscribing to hardware_messages: {error}"))
            })
    }

    pub fn message_from_nats(&self, message: Message) -> Result<DeckrMessage> {
        let envelope = DeckrMessage::from_bytes(&message.payload)?;
        validate_subject_hint(message.subject.as_str(), &envelope)?;
        if let Some(headers) = message.headers.as_ref() {
            validate_nats_headers(headers, &envelope)?;
        }
        Ok(envelope)
    }

    pub async fn watch_concord_changes(&self) -> Result<ConcordStateChangeStream> {
        let watch_key = format!("{}>", concord_contracts_prefix());
        let contracts = self
            .concord_contracts
            .kv
            .watch(&watch_key)
            .await
            .map_err(|error| {
                Error::StateUnavailable(format!("watching Concord contract state: {error}"))
            })?;
        let tokens = self
            .concord_tokens
            .kv
            .watch(&watch_key)
            .await
            .map_err(|error| {
                Error::StateUnavailable(format!("watching Concord token state: {error}"))
            })?;
        Ok(ConcordStateChangeStream { contracts, tokens })
    }
}

#[derive(Debug, Clone)]
pub struct NatsStateStore {
    kv: Store,
    policy: StateStorePolicy,
}

impl NatsStateStore {
    pub fn policy(&self) -> &StateStorePolicy {
        &self.policy
    }

    fn validate_ttl(&self, ttl: Option<u64>) -> Result<()> {
        if !self.policy.allow_write_ttl {
            if ttl.is_none() {
                return Ok(());
            }
            return Err(Error::Invalid(format!(
                "NATS {} does not use write TTL",
                self.policy.description
            )));
        }
        if let Some(ttl) = ttl {
            if Some(ttl) != self.policy.broker_ttl_seconds {
                return Err(Error::Invalid(format!(
                    "NATS current state uses broker TTL {:?}; per-key TTL {ttl} is not supported",
                    self.policy.broker_ttl_seconds
                )));
            }
        }
        Ok(())
    }
}

impl StateStore for NatsStateStore {
    async fn get(&self, key: &str) -> Result<Option<StateEntry>> {
        let Some(entry) = self.kv.entry(key.to_string()).await.map_err(|error| {
            Error::StateUnavailable(format!("reading state key {key}: {error}"))
        })?
        else {
            return Ok(None);
        };
        if entry.operation != Operation::Put {
            return Ok(None);
        }
        Ok(Some(StateEntry {
            key: key.to_string(),
            value: serde_json::from_slice(&entry.value)?,
            revision: entry.revision,
        }))
    }

    async fn items(&self, prefix: &str) -> Result<Vec<StateEntry>> {
        let mut keys = match self.kv.keys().await {
            Ok(keys) => keys,
            Err(error) if is_no_keys_error(&error) => return Ok(Vec::new()),
            Err(error) => {
                return Err(Error::StateUnavailable(format!(
                    "listing state keys: {error}"
                )))
            }
        };
        let mut entries = Vec::new();
        while let Some(key) = match keys.try_next().await {
            Ok(key) => key,
            Err(error) if is_no_keys_error(&error) => None,
            Err(error) => {
                return Err(Error::StateUnavailable(format!(
                    "reading state key list: {error}"
                )))
            }
        } {
            if !key.starts_with(prefix) {
                continue;
            }
            if let Some(entry) = self.get(&key).await? {
                entries.push(entry);
            }
        }
        Ok(entries)
    }

    async fn put(&self, key: &str, value: Value, ttl: Option<u64>) -> Result<StateEntry> {
        self.validate_ttl(ttl)?;
        let revision = self
            .kv
            .put(key, serde_json::to_vec(&value)?.into())
            .await
            .map_err(|error| {
                Error::StateUnavailable(format!("putting state key {key}: {error}"))
            })?;
        Ok(StateEntry {
            key: key.to_string(),
            value,
            revision,
        })
    }

    async fn create(&self, key: &str, value: Value, ttl: Option<u64>) -> Result<StateEntry> {
        self.validate_ttl(ttl)?;
        let result = self
            .kv
            .create(key, serde_json::to_vec(&value)?.into())
            .await;
        let revision = result.map_err(|error| {
            if is_revision_conflict(&error) {
                Error::StateConflict(format!("state key {key:?} already exists"))
            } else {
                Error::StateUnavailable(format!("creating state key {key}: {error}"))
            }
        })?;
        Ok(StateEntry {
            key: key.to_string(),
            value,
            revision,
        })
    }

    async fn update(
        &self,
        key: &str,
        value: Value,
        revision: u64,
        ttl: Option<u64>,
    ) -> Result<StateEntry> {
        self.validate_ttl(ttl)?;
        let result = self
            .kv
            .update(key, serde_json::to_vec(&value)?.into(), revision)
            .await;
        let new_revision = result.map_err(|error| {
            if is_revision_conflict(&error) {
                Error::StateConflict(format!("state key {key:?} revision changed"))
            } else {
                Error::StateUnavailable(format!("updating state key {key}: {error}"))
            }
        })?;
        Ok(StateEntry {
            key: key.to_string(),
            value,
            revision: new_revision,
        })
    }

    async fn delete(&self, key: &str, revision: Option<u64>) -> Result<()> {
        self.kv
            .delete_expect_revision(key, revision)
            .await
            .map_err(|error| {
                if is_revision_conflict(&error) {
                    Error::StateConflict(format!("state key {key:?} revision changed"))
                } else {
                    Error::StateUnavailable(format!("deleting state key {key}: {error}"))
                }
            })
    }
}

pub fn nats_headers_for(message: &DeckrMessage) -> HeaderMap {
    let mut headers = HeaderMap::new();
    for (key, value) in headers_for(message) {
        headers.insert(key.as_str(), value.as_str());
    }
    headers
}

pub fn validate_nats_headers(headers: &HeaderMap, message: &DeckrMessage) -> Result<()> {
    let mut map = BTreeMap::new();
    for key in [
        "Deckr-Message-Id",
        "Deckr-Message-Type",
        "Deckr-Sender",
        "Deckr-Sender-Session",
        "Deckr-Recipient",
        "Deckr-Recipient-Session",
        "Deckr-In-Reply-To",
    ] {
        if let Some(value) = headers.get(key) {
            map.insert(key.to_string(), value.as_str().to_string());
        }
    }
    crate::lanes::validate_headers(&map, message)
}

async fn open_state_bucket(
    jetstream: &JetStreamContext,
    bucket: &str,
    policy: StateStorePolicy,
) -> Result<NatsStateStore> {
    match jetstream.get_key_value(bucket).await {
        Ok(store) => {
            validate_bucket(&store, bucket, &policy).await?;
            Ok(NatsStateStore { kv: store, policy })
        }
        Err(get_error) => {
            let created = jetstream
                .create_key_value(KvConfig {
                    bucket: bucket.to_string(),
                    history: 1,
                    max_age: policy
                        .broker_ttl_seconds
                        .map(Duration::from_secs)
                        .unwrap_or(Duration::ZERO),
                    limit_markers: policy.broker_ttl_seconds.map(Duration::from_secs),
                    ..Default::default()
                })
                .await;
            match created {
                Ok(store) => {
                    validate_bucket(&store, bucket, &policy).await?;
                    Ok(NatsStateStore { kv: store, policy })
                }
                Err(create_error) => {
                    let store = jetstream.get_key_value(bucket).await.map_err(|error| {
                        Error::StateUnavailable(format!(
                            "opening NATS KV bucket {bucket}; initial open failed with {get_error}; create failed with {create_error}; final open failed with {error}"
                        ))
                    })?;
                    validate_bucket(&store, bucket, &policy).await?;
                    Ok(NatsStateStore { kv: store, policy })
                }
            }
        }
    }
}

async fn validate_bucket(store: &Store, bucket: &str, policy: &StateStorePolicy) -> Result<()> {
    let status = store.status().await.map_err(|error| {
        Error::StateUnavailable(format!("inspecting NATS KV bucket {bucket}: {error}"))
    })?;
    if status.history() != 1 {
        return Err(Error::StateUnavailable(format!(
            "NATS KV bucket {bucket} has history {}; expected 1",
            status.history()
        )));
    }
    let expected_max_age = policy
        .broker_ttl_seconds
        .map(Duration::from_secs)
        .unwrap_or(Duration::ZERO);
    if status.max_age() != expected_max_age {
        return Err(Error::StateUnavailable(format!(
            "NATS KV bucket {bucket} has broker TTL {:?}; expected {:?}",
            status.max_age(),
            expected_max_age
        )));
    }
    Ok(())
}

fn is_no_keys_error(error: &impl Display) -> bool {
    let message = error.to_string().to_lowercase();
    message.contains("no keys") || message.contains("no messages")
}

fn is_revision_conflict(error: &impl Display) -> bool {
    let message = error.to_string().to_lowercase();
    message.contains("wrong last sequence")
        || message.contains("sequence mismatch")
        || message.contains("wrong last")
        || message.contains("expected")
        || message.contains("already exists")
}
