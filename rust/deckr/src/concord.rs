use std::collections::{BTreeMap, BTreeSet};

use chrono::{SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

use crate::canonical_json::canonical_json_hash_value;
use crate::endpoint::EndpointAddress;
use crate::keys::{
    concord_contract_key as make_concord_contract_key, concord_contracts_prefix,
    concord_participant_token_key as make_concord_participant_token_key,
    parse_concord_contract_key,
};
use crate::state::{StateStore, StateStorePolicy};
use crate::{Error, Result};

pub const CONCORD_CONTRACT_SCHEMA_ID: &str = "dev.deckr.concord.contract.v1";
pub const CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID: &str = "dev.deckr.concord.participant-token.v1";
pub const DEFAULT_CONCORD_CONTRACT_STORE_NAME: &str = "deckr_concord_contract_v1";
pub const DEFAULT_CONCORD_TOKEN_STORE_NAME: &str = "deckr_concord_token_v1";
pub const DEFAULT_CONCORD_TOKEN_TTL_SECONDS: u64 = 30;

pub fn concord_contract_store_policy() -> StateStorePolicy {
    StateStorePolicy::persistent("Concord contract state")
}

pub fn concord_token_store_policy() -> StateStorePolicy {
    StateStorePolicy::ttl(
        DEFAULT_CONCORD_TOKEN_TTL_SECONDS,
        "Concord participant token state",
    )
    .expect("default Concord token TTL should be valid")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ContractState {
    Open,
    Cancelled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ContractValidityStatus {
    Valid,
    NotYetFulfilled,
    Cancelled,
    MissingContract,
    InvalidContract,
    InvalidToken,
    MissingToken,
    GenerationMismatch,
    SessionMismatch,
    TermsHashMismatch,
    Unavailable,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ContractPointer {
    pub contract_id: String,
    pub generation: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct TokenObservation {
    pub generation: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refresh_seq: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revision: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token_hash: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ContractRecord {
    #[serde(default = "contract_schema_id", rename = "schema")]
    pub schema_id: String,
    pub contract_id: String,
    pub generation: u64,
    pub participants: Vec<EndpointAddress>,
    pub attached_participants: Vec<EndpointAddress>,
    #[serde(default = "default_contract_state")]
    pub state: ContractState,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub profile: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub terms_hash: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub terms: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub created_by: Option<EndpointAddress>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cancelled_by: Option<EndpointAddress>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cancelled_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cancel_revision: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cancel_reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub supersedes: Option<ContractPointer>,
}

impl ContractRecord {
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
        if self.schema_id != CONCORD_CONTRACT_SCHEMA_ID {
            return Err(Error::Invalid(format!(
                "Concord contract schema must be {CONCORD_CONTRACT_SCHEMA_ID}"
            )));
        }
        require_text(&self.contract_id, "contract id")?;
        if self.generation == 0 {
            return Err(Error::Invalid(
                "generation must be greater than zero".to_string(),
            ));
        }
        validate_endpoint_list(&self.participants, true, "Concord contract participants")?;
        validate_endpoint_list(
            &self.attached_participants,
            false,
            "Concord attached participants",
        )?;
        let participants = self.participants.iter().collect::<BTreeSet<_>>();
        if self
            .attached_participants
            .iter()
            .any(|participant| !participants.contains(participant))
        {
            return Err(Error::Invalid(
                "attachedParticipants must be a subset of participants".to_string(),
            ));
        }
        if let Some(profile) = &self.profile {
            require_text(profile, "Concord contract field")?;
        }
        if let Some(terms_hash) = &self.terms_hash {
            require_text(terms_hash, "Concord contract field")?;
        }
        if let Some(cancel_reason) = &self.cancel_reason {
            require_text(cancel_reason, "Concord contract field")?;
        }
        if let Some(terms) = &self.terms {
            let expected = canonical_json_hash_value(terms)?;
            if self.terms_hash.as_deref() != Some(expected.as_str()) {
                return Err(Error::Invalid(
                    "Concord contract termsHash does not match terms".to_string(),
                ));
            }
            if let (Some(profile), Some(term_profile)) =
                (&self.profile, terms.get("profile").and_then(Value::as_str))
            {
                if profile != term_profile {
                    return Err(Error::Invalid(
                        "Concord contract profile must match terms.profile".to_string(),
                    ));
                }
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ParticipantTokenRecord {
    #[serde(default = "participant_token_schema_id", rename = "schema")]
    pub schema_id: String,
    pub contract_id: String,
    pub generation: u64,
    pub participant: EndpointAddress,
    pub session_id: String,
    pub token_id: String,
    pub refresh_seq: u64,
    pub ttl_seconds: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub terms_hash: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub contract_hash: Option<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub observed: BTreeMap<String, TokenObservation>,
}

impl ParticipantTokenRecord {
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
        if self.schema_id != CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID {
            return Err(Error::Invalid(format!(
                "Concord participant token schema must be {CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID}"
            )));
        }
        require_text(&self.contract_id, "Concord participant token identity")?;
        require_text(&self.session_id, "Concord participant token identity")?;
        require_text(&self.token_id, "Concord participant token identity")?;
        if self.generation == 0 {
            return Err(Error::Invalid(
                "generation must be greater than zero".to_string(),
            ));
        }
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
        if let Some(terms_hash) = &self.terms_hash {
            require_text(terms_hash, "Concord participant token field")?;
        }
        if let Some(contract_hash) = &self.contract_hash {
            require_text(contract_hash, "Concord participant token field")?;
        }
        for observation in self.observed.values() {
            if observation.generation == 0 {
                return Err(Error::Invalid(
                    "token observation generation must be greater than zero".to_string(),
                ));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ContractHandle {
    pub key: String,
    pub contract_id: String,
    pub generation: u64,
    pub participants: Vec<EndpointAddress>,
    pub attached_participants: Vec<EndpointAddress>,
    pub revision: u64,
    pub state: ContractState,
    pub profile: Option<String>,
    pub terms_hash: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ParticipantHandle {
    pub key: String,
    pub contract_id: String,
    pub generation: u64,
    pub participant: EndpointAddress,
    pub session_id: String,
    pub token_id: String,
    pub revision: u64,
    pub refresh_seq: u64,
    pub ttl_seconds: u64,
    pub terms_hash: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ContractValidity {
    pub status: ContractValidityStatus,
    pub contract: Option<ContractRecord>,
    pub tokens: BTreeMap<String, ParticipantTokenRecord>,
    pub reason: Option<String>,
}

impl ContractValidity {
    pub fn valid(&self) -> bool {
        self.status == ContractValidityStatus::Valid
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ConcordManagedContract {
    pub contract: ContractHandle,
    pub record: ContractRecord,
    pub validity: ContractValidity,
    pub token: Option<ParticipantHandle>,
}

#[derive(Debug, Clone)]
pub struct ConcordParticipantLease {
    pub contract: ContractHandle,
    pub participant: EndpointAddress,
    pub session_id: String,
    token: Option<ParticipantHandle>,
    closed: bool,
}

impl ConcordParticipantLease {
    pub fn new(
        contract: ContractHandle,
        participant: EndpointAddress,
        session_id: String,
    ) -> Result<Self> {
        require_text(&session_id, "Concord session id")?;
        Ok(Self {
            contract,
            participant,
            session_id,
            token: None,
            closed: false,
        })
    }

    pub fn token(&self) -> Option<&ParticipantHandle> {
        self.token.as_ref()
    }

    pub fn close(&mut self) {
        self.closed = true;
        self.token = None;
    }

    pub fn adopt(&mut self, token: ParticipantHandle) -> Result<()> {
        if token.contract_id != self.contract.contract_id {
            return Err(Error::Invalid(
                "participant token belongs to a different contract".to_string(),
            ));
        }
        if token.generation != self.contract.generation {
            return Err(Error::Invalid(
                "participant token belongs to a different generation".to_string(),
            ));
        }
        if token.participant != self.participant {
            return Err(Error::Invalid(
                "participant token belongs to a different participant".to_string(),
            ));
        }
        if token.session_id != self.session_id {
            return Err(Error::Invalid(
                "participant token belongs to a different session".to_string(),
            ));
        }
        self.token = Some(token);
        Ok(())
    }

    pub async fn attach_or_refresh<C: StateStore, T: StateStore>(
        &mut self,
        concord: &ConcordCoordinator<C, T>,
    ) -> Result<ParticipantHandle> {
        if self.closed {
            return Err(Error::StateConflict(
                "Concord participant lease is closed".to_string(),
            ));
        }
        if let Some(token) = self.token.clone() {
            match concord.refresh(&token).await {
                Ok(refreshed) => {
                    self.token = Some(refreshed.clone());
                    return Ok(refreshed);
                }
                Err(error) => {
                    self.token = None;
                    if is_terminal_participant_conflict(&error) {
                        self.closed = true;
                    }
                    return Err(error);
                }
            }
        }

        match concord
            .attach(&self.contract, &self.participant, &self.session_id, None)
            .await
        {
            Ok(token) => {
                self.token = Some(token.clone());
                Ok(token)
            }
            Err(error) => {
                if is_terminal_participant_conflict(&error) {
                    self.closed = true;
                }
                Err(error)
            }
        }
    }
}

#[derive(Debug, Clone)]
pub struct ConcordCoordinator<C: StateStore, T: StateStore> {
    contract_state: C,
    token_state: T,
    token_ttl_seconds: u64,
}

impl<C: StateStore, T: StateStore> ConcordCoordinator<C, T> {
    pub fn new(contract_state: C, token_state: T) -> Self {
        Self {
            contract_state,
            token_state,
            token_ttl_seconds: DEFAULT_CONCORD_TOKEN_TTL_SECONDS,
        }
    }

    pub fn token_state(&self) -> &T {
        &self.token_state
    }

    pub async fn create_contract(
        &self,
        mut participants: Vec<EndpointAddress>,
        contract_id: Option<String>,
        generation: u64,
        profile: Option<String>,
        terms: Option<Value>,
        created_by: Option<EndpointAddress>,
    ) -> Result<ContractHandle> {
        participants.sort();
        participants.dedup();
        let terms_hash = terms.as_ref().map(canonical_json_hash_value).transpose()?;
        let record = ContractRecord {
            schema_id: CONCORD_CONTRACT_SCHEMA_ID.to_string(),
            contract_id: contract_id.unwrap_or_else(|| Uuid::new_v4().to_string()),
            generation,
            participants,
            attached_participants: Vec::new(),
            state: ContractState::Open,
            profile,
            terms_hash,
            terms,
            created_by,
            created_at: Some(now()),
            cancelled_by: None,
            cancelled_at: None,
            cancel_revision: None,
            cancel_reason: None,
            supersedes: None,
        };
        let key = make_concord_contract_key(&record.contract_id, record.generation);
        let entry = self
            .contract_state
            .create(&key, record.to_value()?, None)
            .await?;
        Ok(contract_handle(key, &record, entry.revision))
    }

    pub async fn find_contracts(&self, profile: Option<&str>) -> Result<Vec<ContractHandle>> {
        let mut contracts = Vec::new();
        for entry in self
            .contract_state
            .items(concord_contracts_prefix())
            .await?
        {
            let Some((contract_id, generation)) = parse_concord_contract_key(&entry.key) else {
                continue;
            };
            let Ok(record) = ContractRecord::from_value(entry.value) else {
                continue;
            };
            if record.contract_id != contract_id || record.generation != generation {
                continue;
            }
            if profile.is_some_and(|profile| record.profile.as_deref() != Some(profile)) {
                continue;
            }
            contracts.push(contract_handle(entry.key, &record, entry.revision));
        }
        contracts.sort_by(|left, right| left.key.cmp(&right.key));
        Ok(contracts)
    }

    pub async fn contract_record(&self, handle: &ContractHandle) -> Result<Option<ContractRecord>> {
        let Some(entry) = self.contract_state.get(&handle.key).await? else {
            return Ok(None);
        };
        Ok(Some(ContractRecord::from_value(entry.value)?))
    }

    pub async fn participant_token(
        &self,
        contract: &ContractHandle,
        participant: &EndpointAddress,
    ) -> Result<Option<ParticipantHandle>> {
        let token_key = make_concord_participant_token_key(
            &contract.contract_id,
            contract.generation,
            participant,
        );
        let Some(entry) = self.token_state.get(&token_key).await? else {
            return Ok(None);
        };
        let token = ParticipantTokenRecord::from_value(entry.value)?;
        if token.contract_id != contract.contract_id
            || token.generation != contract.generation
            || &token.participant != participant
        {
            return Ok(None);
        }
        Ok(Some(participant_handle(token_key, &token, entry.revision)))
    }

    pub async fn attach(
        &self,
        contract: &ContractHandle,
        participant: &EndpointAddress,
        session_id: &str,
        token_id: Option<String>,
    ) -> Result<ParticipantHandle> {
        let Some(current) = self.contract_state.get(&contract.key).await? else {
            return Err(Error::StateConflict(format!(
                "Concord contract {:?} is missing",
                contract.key
            )));
        };
        let record = ContractRecord::from_value(current.value)?;
        if record.state == ContractState::Cancelled {
            return Err(Error::StateConflict(format!(
                "Concord contract {:?} is cancelled",
                contract.key
            )));
        }
        if !record.participants.contains(participant) {
            return Err(Error::Invalid(
                "participant is not named by the Concord contract".to_string(),
            ));
        }
        if record.attached_participants.contains(participant) {
            return Err(Error::StateConflict(
                "Concord participant is already attached".to_string(),
            ));
        }
        let requested_token_id = token_id.clone();
        let token = ParticipantTokenRecord {
            schema_id: CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID.to_string(),
            contract_id: record.contract_id.clone(),
            generation: record.generation,
            participant: participant.clone(),
            session_id: require_text(session_id, "Concord session id")?.to_string(),
            token_id: token_id.unwrap_or_else(|| Uuid::new_v4().to_string()),
            refresh_seq: 1,
            ttl_seconds: self.token_ttl_seconds,
            terms_hash: record.terms_hash.clone(),
            contract_hash: None,
            observed: BTreeMap::new(),
        };
        let token_key =
            make_concord_participant_token_key(&record.contract_id, record.generation, participant);
        let (token, token_entry) = match self
            .token_state
            .create(&token_key, token.to_value()?, Some(token.ttl_seconds))
            .await
        {
            Ok(entry) => (token, entry),
            Err(Error::StateConflict(_)) => {
                let Some(entry) = self.token_state.get(&token_key).await? else {
                    return Err(Error::StateConflict(
                        "Concord participant token changed during attach".to_string(),
                    ));
                };
                let existing = ParticipantTokenRecord::from_value(entry.value.clone())?;
                if !token_matches_attach_request(
                    &existing,
                    &record,
                    participant,
                    session_id,
                    requested_token_id.as_deref(),
                ) {
                    return Err(Error::StateConflict(
                        "Concord participant token already exists".to_string(),
                    ));
                }
                (existing, entry)
            }
            Err(error) => return Err(error),
        };

        self.mark_participant_attached(&contract.key, participant)
            .await?;
        Ok(participant_handle(token_key, &token, token_entry.revision))
    }

    async fn mark_participant_attached(
        &self,
        contract_key: &str,
        participant: &EndpointAddress,
    ) -> Result<()> {
        loop {
            let Some(current) = self.contract_state.get(contract_key).await? else {
                return Err(Error::StateConflict(format!(
                    "Concord contract {contract_key:?} is missing"
                )));
            };
            let mut record = ContractRecord::from_value(current.value)?;
            if record.state == ContractState::Cancelled {
                return Err(Error::StateConflict(format!(
                    "Concord contract {contract_key:?} is cancelled"
                )));
            }
            if !record.participants.contains(participant) {
                return Err(Error::StateConflict(
                    "participant is not named by the Concord contract".to_string(),
                ));
            }
            if record.attached_participants.contains(participant) {
                return Ok(());
            }
            record.attached_participants.push(participant.clone());
            record.attached_participants.sort();
            record.attached_participants.dedup();
            match self
                .contract_state
                .update(contract_key, record.to_value()?, current.revision, None)
                .await
            {
                Ok(_) => return Ok(()),
                Err(Error::StateConflict(_)) => continue,
                Err(error) => return Err(error),
            }
        }
    }

    pub async fn refresh(&self, handle: &ParticipantHandle) -> Result<ParticipantHandle> {
        let contract_key = make_concord_contract_key(&handle.contract_id, handle.generation);
        let Some(contract_entry) = self.contract_state.get(&contract_key).await? else {
            return Err(Error::StateConflict(
                "Concord contract is missing".to_string(),
            ));
        };
        let contract = ContractRecord::from_value(contract_entry.value)?;
        if contract.state == ContractState::Cancelled {
            return Err(Error::StateConflict(
                "Concord contract is cancelled".to_string(),
            ));
        }
        let Some(token_entry) = self.token_state.get(&handle.key).await? else {
            return Err(Error::StateConflict(
                "Concord participant token is missing".to_string(),
            ));
        };
        let mut token = ParticipantTokenRecord::from_value(token_entry.value)?;
        if !token_matches_handle(&token, handle) {
            return Err(Error::StateConflict(
                "Concord participant token changed owner".to_string(),
            ));
        }
        token.refresh_seq += 1;
        let entry = match self
            .token_state
            .update(
                &handle.key,
                token.to_value()?,
                token_entry.revision,
                Some(token.ttl_seconds),
            )
            .await
        {
            Ok(entry) => entry,
            Err(error) if is_state_revision_conflict(&error) => {
                let Some(latest_entry) = self.token_state.get(&handle.key).await? else {
                    return Err(Error::StateConflict(
                        "Concord participant token is missing".to_string(),
                    ));
                };
                let latest = ParticipantTokenRecord::from_value(latest_entry.value)?;
                if !token_matches_handle(&latest, handle) {
                    return Err(Error::StateConflict(
                        "Concord participant token changed owner".to_string(),
                    ));
                }
                return Ok(participant_handle(
                    handle.key.clone(),
                    &latest,
                    latest_entry.revision,
                ));
            }
            Err(error) => return Err(error),
        };
        Ok(participant_handle(
            handle.key.clone(),
            &token,
            entry.revision,
        ))
    }

    pub async fn cancel(
        &self,
        contract: &ContractHandle,
        participant: &EndpointAddress,
        reason: Option<String>,
    ) -> Result<bool> {
        let Some(current) = self.contract_state.get(&contract.key).await? else {
            return Ok(false);
        };
        let mut record = ContractRecord::from_value(current.value)?;
        if record.state == ContractState::Cancelled {
            return Ok(false);
        }
        if !record.participants.contains(participant) {
            return Err(Error::Invalid(
                "participant is not named by the Concord contract".to_string(),
            ));
        }
        record.state = ContractState::Cancelled;
        record.cancelled_by = Some(participant.clone());
        record.cancelled_at = Some(now());
        record.cancel_revision = Some(current.revision);
        record.cancel_reason = reason;
        self.contract_state
            .update(&contract.key, record.to_value()?, current.revision, None)
            .await?;
        Ok(true)
    }

    pub async fn validate(
        &self,
        contract: &ContractHandle,
        current_sessions: Option<&BTreeMap<String, String>>,
    ) -> ContractValidity {
        let contract_entry = match self.contract_state.get(&contract.key).await {
            Ok(entry) => entry,
            Err(_) => return validity(ContractValidityStatus::Unavailable, None, None),
        };
        let Some(contract_entry) = contract_entry else {
            return validity(ContractValidityStatus::MissingContract, None, None);
        };
        let record = match ContractRecord::from_value(contract_entry.value) {
            Ok(record) => record,
            Err(error) => {
                return ContractValidity {
                    status: ContractValidityStatus::InvalidContract,
                    contract: None,
                    tokens: BTreeMap::new(),
                    reason: Some(error.to_string()),
                }
            }
        };
        if record.state == ContractState::Cancelled {
            return validity(
                ContractValidityStatus::Cancelled,
                Some(record),
                None::<String>,
            );
        }
        let attached = record
            .attached_participants
            .iter()
            .map(ToString::to_string)
            .collect::<BTreeSet<_>>();
        let mut pending_participant = None::<String>;
        let mut tokens = BTreeMap::new();
        for participant in &record.participants {
            let participant_key = participant.to_string();
            let token_key = make_concord_participant_token_key(
                &record.contract_id,
                record.generation,
                participant,
            );
            let token_entry = match self.token_state.get(&token_key).await {
                Ok(entry) => entry,
                Err(_) => {
                    return ContractValidity {
                        status: ContractValidityStatus::Unavailable,
                        contract: Some(record),
                        tokens,
                        reason: None,
                    }
                }
            };
            let Some(token_entry) = token_entry else {
                if attached.contains(&participant_key) {
                    return ContractValidity {
                        status: ContractValidityStatus::MissingToken,
                        contract: Some(record),
                        tokens,
                        reason: Some(participant_key),
                    };
                }
                pending_participant.get_or_insert(participant_key);
                continue;
            };
            let token = match ParticipantTokenRecord::from_value(token_entry.value) {
                Ok(token) => token,
                Err(error) => {
                    return ContractValidity {
                        status: ContractValidityStatus::InvalidToken,
                        contract: Some(record),
                        tokens,
                        reason: Some(error.to_string()),
                    }
                }
            };
            if let Some(status) =
                token_validity_status(&token, &record, participant, current_sessions)
            {
                tokens.insert(participant_key, token);
                return ContractValidity {
                    status,
                    contract: Some(record),
                    tokens,
                    reason: None,
                };
            }
            tokens.insert(participant_key.clone(), token);
            if !attached.contains(&participant_key) {
                pending_participant.get_or_insert(participant_key);
            }
        }
        if let Some(participant) = pending_participant {
            return ContractValidity {
                status: ContractValidityStatus::NotYetFulfilled,
                contract: Some(record),
                tokens,
                reason: Some(participant),
            };
        }
        ContractValidity {
            status: ContractValidityStatus::Valid,
            contract: Some(record),
            tokens,
            reason: None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct ConcordParticipantManager<C: StateStore, T: StateStore> {
    concord: ConcordCoordinator<C, T>,
    pub participant: EndpointAddress,
    pub session_id: String,
    pub profile: Option<String>,
    managed: BTreeMap<String, ConcordManagedContract>,
    leases: BTreeMap<String, ConcordParticipantLease>,
}

impl<C: StateStore, T: StateStore> ConcordParticipantManager<C, T> {
    pub fn new(
        concord: ConcordCoordinator<C, T>,
        participant: EndpointAddress,
        session_id: String,
    ) -> Result<Self> {
        require_text(&session_id, "Concord session id")?;
        Ok(Self {
            concord,
            participant,
            session_id,
            profile: None,
            managed: BTreeMap::new(),
            leases: BTreeMap::new(),
        })
    }

    pub fn profile(mut self, profile: impl Into<String>) -> Self {
        self.profile = Some(profile.into());
        self
    }

    pub fn managed_contracts(&self) -> Vec<ConcordManagedContract> {
        self.managed.values().cloned().collect()
    }

    pub fn release(&mut self, contract_key: &str) {
        if let Some(mut lease) = self.leases.remove(contract_key) {
            lease.close();
        }
        self.managed.remove(contract_key);
    }

    pub async fn reconcile<F>(
        &mut self,
        mut accept_contract: F,
        current_sessions: Option<&BTreeMap<String, String>>,
    ) -> Result<Vec<ConcordManagedContract>>
    where
        F: FnMut(&ContractHandle, &ContractRecord) -> Result<bool>,
    {
        let contracts = self.concord.find_contracts(self.profile.as_deref()).await?;
        let mut next_managed = BTreeMap::<String, ConcordManagedContract>::new();
        let mut next_leases = BTreeMap::<String, ConcordParticipantLease>::new();
        let mut current_leases = self.leases.clone();

        for contract in contracts {
            let key = contract.key.clone();
            let lease = current_leases.remove(&key);
            let Some((managed, lease)) = self
                .reconcile_contract(contract, lease, &mut accept_contract, current_sessions)
                .await?
            else {
                continue;
            };
            next_leases.insert(key.clone(), lease);
            next_managed.insert(key, managed);
        }

        for mut lease in current_leases.into_values() {
            lease.close();
        }
        self.managed = next_managed;
        self.leases = next_leases;
        Ok(self.managed_contracts())
    }

    async fn reconcile_contract<F>(
        &self,
        contract: ContractHandle,
        lease: Option<ConcordParticipantLease>,
        accept_contract: &mut F,
        current_sessions: Option<&BTreeMap<String, String>>,
    ) -> Result<Option<(ConcordManagedContract, ConcordParticipantLease)>>
    where
        F: FnMut(&ContractHandle, &ContractRecord) -> Result<bool>,
    {
        if !contract.participants.contains(&self.participant) {
            if let Some(mut lease) = lease {
                lease.close();
            }
            return Ok(None);
        }

        let Some(record) = self.concord.contract_record(&contract).await? else {
            if let Some(mut lease) = lease {
                lease.close();
            }
            return Ok(None);
        };
        if record.state == ContractState::Cancelled {
            if let Some(mut lease) = lease {
                lease.close();
            }
            return Ok(None);
        }
        if !accept_contract(&contract, &record)? {
            if let Some(mut lease) = lease {
                lease.close();
            }
            return Ok(None);
        }

        let sessions = self.current_sessions(current_sessions);
        let validity = self.concord.validate(&contract, Some(&sessions)).await;
        let record = validity.contract.clone().unwrap_or(record);
        if terminal_managed_status(validity.status) {
            if let Some(mut lease) = lease {
                lease.close();
            }
            return Ok(None);
        }

        let mut lease = match lease {
            Some(lease) => lease,
            None => ConcordParticipantLease::new(
                contract.clone(),
                self.participant.clone(),
                self.session_id.clone(),
            )?,
        };

        if lease.token().is_none() {
            if let Some(existing) = self
                .concord
                .participant_token(&contract, &self.participant)
                .await?
            {
                lease.adopt(existing)?;
            }
        }

        let token = match lease.attach_or_refresh(&self.concord).await {
            Ok(token) => token,
            Err(Error::StateConflict(_)) => {
                let validity = self.concord.validate(&contract, Some(&sessions)).await;
                let record = validity.contract.clone().unwrap_or(record);
                if terminal_managed_status(validity.status) {
                    lease.close();
                    return Ok(None);
                }
                return Ok(Some((
                    ConcordManagedContract {
                        contract,
                        record,
                        validity,
                        token: None,
                    },
                    lease,
                )));
            }
            Err(error) => return Err(error),
        };

        let validity = self.concord.validate(&contract, Some(&sessions)).await;
        let record = validity.contract.clone().unwrap_or(record);
        if terminal_managed_status(validity.status) {
            lease.close();
            return Ok(None);
        }
        Ok(Some((
            ConcordManagedContract {
                contract,
                record,
                validity,
                token: Some(token),
            },
            lease,
        )))
    }

    fn current_sessions(
        &self,
        current_sessions: Option<&BTreeMap<String, String>>,
    ) -> BTreeMap<String, String> {
        let mut sessions = current_sessions.cloned().unwrap_or_default();
        sessions.insert(self.participant.to_string(), self.session_id.clone());
        sessions
    }
}

pub fn concord_contract_key(contract_id: &str, generation: u64) -> String {
    make_concord_contract_key(contract_id, generation)
}

pub fn concord_participant_token_key(
    contract_id: &str,
    generation: u64,
    participant: &EndpointAddress,
) -> String {
    make_concord_participant_token_key(contract_id, generation, participant)
}

fn validity<T: Into<Option<String>>>(
    status: ContractValidityStatus,
    contract: Option<ContractRecord>,
    reason: T,
) -> ContractValidity {
    ContractValidity {
        status,
        contract,
        tokens: BTreeMap::new(),
        reason: reason.into(),
    }
}

fn token_validity_status(
    token: &ParticipantTokenRecord,
    contract: &ContractRecord,
    participant: &EndpointAddress,
    current_sessions: Option<&BTreeMap<String, String>>,
) -> Option<ContractValidityStatus> {
    if token.contract_id != contract.contract_id {
        return Some(ContractValidityStatus::InvalidToken);
    }
    if token.generation != contract.generation {
        return Some(ContractValidityStatus::GenerationMismatch);
    }
    if &token.participant != participant {
        return Some(ContractValidityStatus::InvalidToken);
    }
    if contract.terms_hash.is_some() && token.terms_hash != contract.terms_hash {
        return Some(ContractValidityStatus::TermsHashMismatch);
    }
    if let Some(current_sessions) = current_sessions {
        if let Some(current_session) = current_sessions.get(participant.as_str()) {
            if &token.session_id != current_session {
                return Some(ContractValidityStatus::SessionMismatch);
            }
        }
    }
    None
}

fn token_matches_handle(token: &ParticipantTokenRecord, handle: &ParticipantHandle) -> bool {
    token.contract_id == handle.contract_id
        && token.generation == handle.generation
        && token.participant == handle.participant
        && token.session_id == handle.session_id
        && token.token_id == handle.token_id
        && token.terms_hash == handle.terms_hash
}

fn is_state_revision_conflict(error: &Error) -> bool {
    matches!(error, Error::StateConflict(message) if message.contains("revision changed"))
}

fn is_terminal_participant_conflict(error: &Error) -> bool {
    let Error::StateConflict(message) = error else {
        return false;
    };
    (message.starts_with("Concord contract ")
        && (message.contains(" is missing") || message.contains(" is cancelled")))
        || [
            "Concord contract is missing",
            "Concord contract is cancelled",
            "Concord participant token is missing",
            "Concord participant token changed owner",
            "Concord participant is already attached",
        ]
        .iter()
        .any(|part| message.contains(part))
}

fn terminal_managed_status(status: ContractValidityStatus) -> bool {
    matches!(
        status,
        ContractValidityStatus::Cancelled
            | ContractValidityStatus::MissingContract
            | ContractValidityStatus::InvalidContract
            | ContractValidityStatus::InvalidToken
            | ContractValidityStatus::MissingToken
            | ContractValidityStatus::GenerationMismatch
            | ContractValidityStatus::SessionMismatch
            | ContractValidityStatus::TermsHashMismatch
    )
}

fn token_matches_attach_request(
    token: &ParticipantTokenRecord,
    contract: &ContractRecord,
    participant: &EndpointAddress,
    session_id: &str,
    token_id: Option<&str>,
) -> bool {
    token.contract_id == contract.contract_id
        && token.generation == contract.generation
        && &token.participant == participant
        && token.session_id == session_id
        && token_id.is_none_or(|token_id| token.token_id == token_id)
        && token.terms_hash == contract.terms_hash
}

fn contract_handle(key: String, record: &ContractRecord, revision: u64) -> ContractHandle {
    ContractHandle {
        key,
        contract_id: record.contract_id.clone(),
        generation: record.generation,
        participants: record.participants.clone(),
        attached_participants: record.attached_participants.clone(),
        revision,
        state: record.state,
        profile: record.profile.clone(),
        terms_hash: record.terms_hash.clone(),
    }
}

fn participant_handle(
    key: String,
    record: &ParticipantTokenRecord,
    revision: u64,
) -> ParticipantHandle {
    ParticipantHandle {
        key,
        contract_id: record.contract_id.clone(),
        generation: record.generation,
        participant: record.participant.clone(),
        session_id: record.session_id.clone(),
        token_id: record.token_id.clone(),
        revision,
        refresh_seq: record.refresh_seq,
        ttl_seconds: record.ttl_seconds,
        terms_hash: record.terms_hash.clone(),
    }
}

fn validate_endpoint_list(
    endpoints: &[EndpointAddress],
    require_non_empty: bool,
    field_name: &str,
) -> Result<()> {
    if require_non_empty && endpoints.is_empty() {
        return Err(Error::Invalid(format!(
            "{field_name} require at least one entry"
        )));
    }
    let strings = endpoints
        .iter()
        .map(ToString::to_string)
        .collect::<Vec<_>>();
    let unique = strings.iter().collect::<BTreeSet<_>>();
    if unique.len() != strings.len() {
        return Err(Error::Invalid(format!("{field_name} must be unique")));
    }
    let mut sorted = strings.clone();
    sorted.sort();
    if sorted != strings {
        return Err(Error::Invalid(format!(
            "{field_name} must be canonicalized"
        )));
    }
    Ok(())
}

fn require_text<'a>(value: &'a str, field_name: &str) -> Result<&'a str> {
    if value.trim() != value || value.is_empty() {
        return Err(Error::Invalid(format!(
            "{field_name} must be non-empty with no leading or trailing whitespace"
        )));
    }
    Ok(value)
}

fn contract_schema_id() -> String {
    CONCORD_CONTRACT_SCHEMA_ID.to_string()
}

fn participant_token_schema_id() -> String {
    CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID.to_string()
}

fn default_contract_state() -> ContractState {
    ContractState::Open
}

fn now() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}
