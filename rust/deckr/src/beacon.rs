use std::collections::BTreeMap;

use chrono::{SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

use crate::endpoint::EndpointAddress;
use crate::keys::{
    beacon_advertisement_key as make_beacon_advertisement_key, beacon_feature_prefix,
    parse_beacon_advertisement_key,
};
use crate::state::{MaterializedStateStore, StateEntry, StateStore, StateStorePolicy};
use crate::{Error, Result};

pub const BEACON_ADVERTISEMENT_SCHEMA_ID: &str = "dev.deckr.beacon.advertisement.v1";
pub const DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME: &str = "deckr_beacon_advertisement_v1";
pub const DEFAULT_BEACON_TTL_SECONDS: u64 = 30;
pub const BEACON_ADVERTISEMENT_PREFIX: &str = "advertisements.";

pub fn beacon_advertisement_store_policy() -> StateStorePolicy {
    StateStorePolicy::ttl(DEFAULT_BEACON_TTL_SECONDS, "Beacon advertisement state")
        .expect("default Beacon TTL should be valid")
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BeaconProtocol {
    pub namespace: String,
    pub version: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct AdvertisementRecord {
    #[serde(default = "advertisement_schema_id", rename = "schema")]
    pub schema_id: String,
    pub advertisement_id: String,
    pub feature_id: String,
    pub advertiser: EndpointAddress,
    pub endpoint: EndpointAddress,
    pub session_id: String,
    pub refresh_seq: u64,
    pub ttl_seconds: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub protocol: Option<BeaconProtocol>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub operations: Vec<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub labels: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub hints: BTreeMap<String, Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub payload: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub updated_at: Option<String>,
}

impl AdvertisementRecord {
    pub fn from_value(value: Value) -> Result<Self> {
        let record: Self = serde_json::from_value(value)?;
        record.validate()?;
        Ok(record)
    }

    pub fn to_value(&self) -> Result<Value> {
        self.validate()?;
        Ok(serde_json::to_value(self)?)
    }

    pub fn validate(&self) -> Result<()> {
        require_text(&self.schema_id, "Beacon schema")?;
        if self.schema_id != BEACON_ADVERTISEMENT_SCHEMA_ID {
            return Err(Error::Invalid(format!(
                "Beacon advertisement schema must be {BEACON_ADVERTISEMENT_SCHEMA_ID}"
            )));
        }
        require_text(&self.advertisement_id, "Beacon advertisement identity")?;
        require_text(&self.feature_id, "Beacon advertisement identity")?;
        require_text(&self.session_id, "Beacon advertisement identity")?;
        if self.refresh_seq == 0 {
            return Err(Error::Invalid(
                "refreshSeq must be greater than zero".to_string(),
            ));
        }
        if self.ttl_seconds == 0 {
            return Err(Error::Invalid(
                "ttlSeconds must be greater than zero".to_string(),
            ));
        }
        for operation in &self.operations {
            require_text(operation, "Beacon operation")?;
        }
        for (key, value) in &self.labels {
            require_text(key, "Beacon label key")?;
            require_text(value, "Beacon label value")?;
        }
        if let Some(payload) = &self.payload {
            if !payload.is_object() {
                return Err(Error::Invalid(
                    "Beacon advertisement payload must be a JSON object".to_string(),
                ));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct AdvertisementHandle {
    pub key: String,
    pub advertisement_id: String,
    pub feature_id: String,
    pub advertiser: EndpointAddress,
    pub endpoint: EndpointAddress,
    pub session_id: String,
    pub revision: u64,
    pub refresh_seq: u64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Candidate {
    pub key: String,
    pub advertisement: AdvertisementRecord,
    pub revision: u64,
}

#[derive(Debug, Clone)]
pub struct BeaconAdvertiser<S: StateStore> {
    state: S,
    advertisement_id: String,
    feature_id: String,
    advertiser: EndpointAddress,
    endpoint: EndpointAddress,
    session_id: String,
    ttl_seconds: u64,
    operations: Vec<String>,
    labels: BTreeMap<String, String>,
    hints: BTreeMap<String, Value>,
    payload: Option<Value>,
}

#[derive(Clone)]
pub struct Beacon<S: StateStore> {
    advertisements: MaterializedStateStore<S>,
}

impl<S: StateStore> Beacon<S> {
    pub async fn start(state: S) -> Result<Self> {
        Ok(Self {
            advertisements: MaterializedStateStore::start(state, BEACON_ADVERTISEMENT_PREFIX)
                .await?,
        })
    }

    pub fn materialized_store(&self) -> &MaterializedStateStore<S> {
        &self.advertisements
    }

    pub fn candidate_by_key(&self, key: &str) -> Result<Option<Candidate>> {
        Ok(self
            .advertisements
            .get_cached(key)?
            .and_then(candidate_from_entry))
    }

    pub fn candidates(&self, feature_id: &str) -> Result<Vec<Candidate>> {
        let prefix = beacon_feature_prefix(feature_id);
        sorted_candidates(
            self.advertisements
                .items_cached(&prefix)?
                .into_iter()
                .filter_map(candidate_from_entry)
                .filter(|candidate| candidate.advertisement.feature_id == feature_id),
        )
    }

    pub fn candidates_for_endpoint(
        &self,
        feature_id: &str,
        advertiser: &EndpointAddress,
        endpoint: &EndpointAddress,
    ) -> Result<Vec<Candidate>> {
        sorted_candidates(
            self.candidates(feature_id)?
                .into_iter()
                .filter(|candidate| {
                    &candidate.advertisement.advertiser == advertiser
                        && &candidate.advertisement.endpoint == endpoint
                }),
        )
    }
}

impl<S: StateStore> BeaconAdvertiser<S> {
    pub fn new(
        state: S,
        feature_id: impl Into<String>,
        endpoint: EndpointAddress,
        session_id: impl Into<String>,
    ) -> Self {
        Self {
            state,
            advertisement_id: Uuid::new_v4().to_string(),
            feature_id: feature_id.into(),
            advertiser: endpoint.clone(),
            endpoint,
            session_id: session_id.into(),
            ttl_seconds: DEFAULT_BEACON_TTL_SECONDS,
            operations: Vec::new(),
            labels: BTreeMap::new(),
            hints: BTreeMap::new(),
            payload: None,
        }
    }

    pub fn advertisement_id(mut self, advertisement_id: impl Into<String>) -> Self {
        self.advertisement_id = advertisement_id.into();
        self
    }

    pub fn payload(mut self, payload: Value) -> Self {
        self.payload = Some(payload);
        self
    }

    pub fn labels(mut self, labels: BTreeMap<String, String>) -> Self {
        self.labels = labels;
        self
    }

    pub fn ttl_seconds(mut self, ttl_seconds: u64) -> Self {
        self.ttl_seconds = ttl_seconds;
        self
    }

    pub async fn publish_or_refresh(
        &self,
        current_handle: Option<&AdvertisementHandle>,
    ) -> Result<AdvertisementHandle> {
        if let Some(handle) = current_handle {
            if let Ok(handle) = self.refresh(handle).await {
                return Ok(handle);
            }
        }
        self.publish().await
    }

    pub async fn publish(&self) -> Result<AdvertisementHandle> {
        let record = self.record(1, None);
        let key = make_beacon_advertisement_key(&record.feature_id, &record.advertisement_id);
        let value = record.to_value()?;
        let entry = match self
            .state
            .create(&key, value.clone(), Some(record.ttl_seconds))
            .await
        {
            Ok(entry) => entry,
            Err(Error::StateConflict(_)) => {
                let current = self.state.get(&key).await?;
                let Some(current) = current else {
                    return self
                        .state
                        .create(&key, value, Some(record.ttl_seconds))
                        .await
                        .map(|entry| advertisement_handle(key, &record, entry.revision));
                };
                let current_record = AdvertisementRecord::from_value(current.value)?;
                if !advertisement_matches(&current_record, &record) {
                    return Err(Error::StateConflict(format!(
                        "Beacon advertisement {key:?} already exists for a different owner"
                    )));
                }
                let refreshed =
                    self.record(current_record.refresh_seq + 1, current_record.created_at);
                self.state
                    .update(
                        &key,
                        refreshed.to_value()?,
                        current.revision,
                        Some(refreshed.ttl_seconds),
                    )
                    .await?
            }
            Err(error) => return Err(error),
        };
        let record = AdvertisementRecord::from_value(entry.value.clone())?;
        Ok(advertisement_handle(key, &record, entry.revision))
    }

    pub async fn refresh(&self, handle: &AdvertisementHandle) -> Result<AdvertisementHandle> {
        let Some(current) = self.state.get(&handle.key).await? else {
            return Err(Error::StateConflict(format!(
                "Beacon advertisement {:?} is missing",
                handle.key
            )));
        };
        let current_record = AdvertisementRecord::from_value(current.value)?;
        if !advertisement_matches_handle(&current_record, handle) {
            return Err(Error::StateConflict(
                "Beacon advertisement changed owner".to_string(),
            ));
        }
        let refreshed = self.record(
            current_record.refresh_seq + 1,
            current_record.created_at.or_else(|| Some(now())),
        );
        let entry = self
            .state
            .update(
                &handle.key,
                refreshed.to_value()?,
                current.revision,
                Some(refreshed.ttl_seconds),
            )
            .await?;
        Ok(advertisement_handle(
            handle.key.clone(),
            &refreshed,
            entry.revision,
        ))
    }

    pub async fn withdraw(&self, handle: &AdvertisementHandle) -> Result<()> {
        let Some(current) = self.state.get(&handle.key).await? else {
            return Ok(());
        };
        let current_record = AdvertisementRecord::from_value(current.value)?;
        if !advertisement_matches_handle(&current_record, handle) {
            return Err(Error::StateConflict(
                "Beacon advertisement changed owner".to_string(),
            ));
        }
        self.state.delete(&handle.key, Some(current.revision)).await
    }

    fn record(&self, refresh_seq: u64, created_at: Option<String>) -> AdvertisementRecord {
        let now = now();
        AdvertisementRecord {
            schema_id: BEACON_ADVERTISEMENT_SCHEMA_ID.to_string(),
            advertisement_id: self.advertisement_id.clone(),
            feature_id: self.feature_id.clone(),
            advertiser: self.advertiser.clone(),
            endpoint: self.endpoint.clone(),
            session_id: self.session_id.clone(),
            refresh_seq,
            ttl_seconds: self.ttl_seconds,
            protocol: None,
            operations: self.operations.clone(),
            labels: self.labels.clone(),
            hints: self.hints.clone(),
            payload: self.payload.clone(),
            created_at: created_at.or_else(|| Some(now.clone())),
            updated_at: Some(now),
        }
    }
}

pub fn candidate_from_entry(entry: StateEntry) -> Option<Candidate> {
    let advertisement = AdvertisementRecord::from_value(entry.value).ok()?;
    let (feature_id, advertisement_id) = parse_beacon_advertisement_key(&entry.key)?;
    if feature_id != advertisement.feature_id || advertisement_id != advertisement.advertisement_id
    {
        return None;
    }
    Some(Candidate {
        key: entry.key,
        advertisement,
        revision: entry.revision,
    })
}

pub async fn find_candidates<S: StateStore>(state: &S, feature_id: &str) -> Result<Vec<Candidate>> {
    let prefix = beacon_feature_prefix(feature_id);
    sorted_candidates(
        state
            .items(&prefix)
            .await?
            .into_iter()
            .filter_map(candidate_from_entry)
            .filter(|candidate| candidate.advertisement.feature_id == feature_id),
    )
}

fn sorted_candidates(candidates: impl IntoIterator<Item = Candidate>) -> Result<Vec<Candidate>> {
    let mut candidates = candidates.into_iter().collect::<Vec<_>>();
    candidates.sort_by(|left, right| left.key.cmp(&right.key));
    Ok(candidates)
}

pub fn beacon_advertisement_key(feature_id: &str, advertisement_id: &str) -> String {
    make_beacon_advertisement_key(feature_id, advertisement_id)
}

fn advertisement_schema_id() -> String {
    BEACON_ADVERTISEMENT_SCHEMA_ID.to_string()
}

fn advertisement_handle(
    key: String,
    record: &AdvertisementRecord,
    revision: u64,
) -> AdvertisementHandle {
    AdvertisementHandle {
        key,
        advertisement_id: record.advertisement_id.clone(),
        feature_id: record.feature_id.clone(),
        advertiser: record.advertiser.clone(),
        endpoint: record.endpoint.clone(),
        session_id: record.session_id.clone(),
        revision,
        refresh_seq: record.refresh_seq,
    }
}

fn advertisement_matches(left: &AdvertisementRecord, right: &AdvertisementRecord) -> bool {
    left.advertisement_id == right.advertisement_id
        && left.feature_id == right.feature_id
        && left.advertiser == right.advertiser
        && left.endpoint == right.endpoint
        && left.session_id == right.session_id
}

fn advertisement_matches_handle(
    record: &AdvertisementRecord,
    handle: &AdvertisementHandle,
) -> bool {
    record.advertisement_id == handle.advertisement_id
        && record.feature_id == handle.feature_id
        && record.advertiser == handle.advertiser
        && record.endpoint == handle.endpoint
        && record.session_id == handle.session_id
}

fn require_text(value: &str, field_name: &str) -> Result<()> {
    if value.trim() != value || value.is_empty() {
        return Err(Error::Invalid(format!(
            "{field_name} must be non-empty with no leading or trailing whitespace"
        )));
    }
    Ok(())
}

fn now() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}
