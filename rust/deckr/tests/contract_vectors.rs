use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use deckr::beacon::{
    beacon_advertisement_key, beacon_advertisement_store_policy, AdvertisementRecord,
    BeaconAdvertiser, DEFAULT_BEACON_TTL_SECONDS,
};
use deckr::canonical_json::{canonical_json_bytes_value, canonical_json_hash_value};
use deckr::concord::{
    concord_contract_key, concord_participant_token_key, concord_token_store_policy,
    ConcordCoordinator, ConcordNotificationSource, ConcordParticipantLease,
    ConcordParticipantManager, ContractHandle, ContractRecord, ContractState,
    ContractValidityStatus, CreateContractSpec, ParticipantTokenRecord,
    DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS, DEFAULT_CONCORD_TOKEN_TTL_SECONDS,
};
use deckr::endpoint::EndpointAddress;
use deckr::keys::{decode_key_token, encode_key_token};
use deckr::lanes::{
    broadcast_subject, direct_subject, endpoint_subscription_subjects, headers_for,
    message_is_deliverable_to, subject_for, DeckrMessage, HardwareMessageBody,
};
use deckr::profiles::hardware::{
    hardware_payload_from_advertisement, HardwareBeaconPayload, HardwareClaimTerms,
    HARDWARE_CLAIM_PROFILE_ID,
};
use deckr::state::{
    MaterializedStateStore, MemoryStateStore, StateEntry, StateMaintenancePolicy, StateStore,
    StateWatchStream,
};
use deckr::Result;
use serde::Deserialize;
use serde_json::{json, Value};

const CONTRACT_ROOT: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../contract/v1");

#[derive(Debug, Deserialize)]
struct KeyTokenVectors {
    cases: Vec<KeyTokenCase>,
}

#[derive(Debug, Deserialize)]
struct KeyTokenCase {
    raw: String,
    token: String,
}

#[derive(Clone)]
struct RacingUpdateStore {
    inner: MemoryStateStore,
    raced: Arc<Mutex<bool>>,
}

impl RacingUpdateStore {
    fn new(inner: MemoryStateStore) -> Self {
        Self {
            inner,
            raced: Arc::new(Mutex::new(false)),
        }
    }

    fn raced(&self) -> bool {
        *self.raced.lock().expect("racing update mutex poisoned")
    }
}

#[derive(Clone)]
struct RecordingStateStore {
    inner: MemoryStateStore,
    items_prefixes: Arc<Mutex<Vec<String>>>,
    get_keys: Arc<Mutex<Vec<String>>>,
}

impl RecordingStateStore {
    fn new(inner: MemoryStateStore) -> Self {
        Self {
            inner,
            items_prefixes: Arc::new(Mutex::new(Vec::new())),
            get_keys: Arc::new(Mutex::new(Vec::new())),
        }
    }

    fn clear_observations(&self) {
        self.items_prefixes
            .lock()
            .expect("items_prefixes mutex poisoned")
            .clear();
        self.get_keys
            .lock()
            .expect("get_keys mutex poisoned")
            .clear();
    }

    fn items_prefixes(&self) -> Vec<String> {
        self.items_prefixes
            .lock()
            .expect("items_prefixes mutex poisoned")
            .clone()
    }

    fn get_keys(&self) -> Vec<String> {
        self.get_keys
            .lock()
            .expect("get_keys mutex poisoned")
            .clone()
    }
}

impl StateStore for RecordingStateStore {
    async fn ttl_seconds(&self) -> Result<Option<u64>> {
        self.inner.ttl_seconds().await
    }

    async fn get(&self, key: &str) -> Result<Option<StateEntry>> {
        self.get_keys
            .lock()
            .expect("get_keys mutex poisoned")
            .push(key.to_string());
        self.inner.get(key).await
    }

    async fn items(&self, prefix: &str) -> Result<Vec<StateEntry>> {
        self.items_prefixes
            .lock()
            .expect("items_prefixes mutex poisoned")
            .push(prefix.to_string());
        self.inner.items(prefix).await
    }

    async fn put(&self, key: &str, value: Value, ttl: Option<u64>) -> Result<StateEntry> {
        self.inner.put(key, value, ttl).await
    }

    async fn create(&self, key: &str, value: Value, ttl: Option<u64>) -> Result<StateEntry> {
        self.inner.create(key, value, ttl).await
    }

    async fn update(
        &self,
        key: &str,
        value: Value,
        revision: u64,
        ttl: Option<u64>,
    ) -> Result<StateEntry> {
        self.inner.update(key, value, revision, ttl).await
    }

    async fn delete(&self, key: &str, revision: Option<u64>) -> Result<()> {
        self.inner.delete(key, revision).await
    }

    async fn watch(&self, prefix: &str) -> Result<StateWatchStream> {
        self.inner.watch(prefix).await
    }
}

impl StateStore for RacingUpdateStore {
    async fn ttl_seconds(&self) -> Result<Option<u64>> {
        self.inner.ttl_seconds().await
    }

    async fn get(&self, key: &str) -> Result<Option<StateEntry>> {
        self.inner.get(key).await
    }

    async fn items(&self, prefix: &str) -> Result<Vec<StateEntry>> {
        self.inner.items(prefix).await
    }

    async fn put(&self, key: &str, value: Value, ttl: Option<u64>) -> Result<StateEntry> {
        self.inner.put(key, value, ttl).await
    }

    async fn create(&self, key: &str, value: Value, ttl: Option<u64>) -> Result<StateEntry> {
        self.inner.create(key, value, ttl).await
    }

    async fn update(
        &self,
        key: &str,
        value: Value,
        revision: u64,
        ttl: Option<u64>,
    ) -> Result<StateEntry> {
        let should_race = {
            let mut raced = self.raced.lock().expect("racing update mutex poisoned");
            let should_race = !*raced && key.contains(".participants.");
            if should_race {
                *raced = true;
            }
            should_race
        };
        if should_race {
            let current = self
                .inner
                .get(key)
                .await?
                .expect("token should exist before racing update");
            let mut token = ParticipantTokenRecord::from_value(current.value)?;
            token.refresh_seq += 1;
            self.inner
                .update(key, token.to_value()?, current.revision, ttl)
                .await?;
        }
        self.inner.update(key, value, revision, ttl).await
    }

    async fn delete(&self, key: &str, revision: Option<u64>) -> Result<()> {
        self.inner.delete(key, revision).await
    }

    async fn watch(&self, prefix: &str) -> Result<StateWatchStream> {
        self.inner.watch(prefix).await
    }
}

#[test]
fn key_token_vectors_match_contract_artifacts() {
    let vectors: KeyTokenVectors = serde_json::from_str(include_str!(
        "../../../contract/v1/vectors/key-tokens.v1.json"
    ))
    .unwrap();
    for case in vectors.cases {
        assert_eq!(encode_key_token(&case.raw), case.token);
        assert_eq!(decode_key_token(&case.token).unwrap(), case.raw);
    }
}

#[derive(Debug, Deserialize)]
struct BeaconConcordKeyVectors {
    cases: Vec<BeaconConcordKeyCase>,
}

#[derive(Debug, Deserialize)]
struct BeaconConcordKeyCase {
    helper: String,
    input: Value,
    key: String,
}

#[test]
fn beacon_concord_key_vectors_match_contract_artifacts() {
    let vectors: BeaconConcordKeyVectors = serde_json::from_str(include_str!(
        "../../../contract/v1/vectors/beacon-concord-keys.v1.json"
    ))
    .unwrap();
    for case in vectors.cases {
        let actual = match case.helper.as_str() {
            "beacon_advertisement_key" => beacon_advertisement_key(
                case.input["featureId"].as_str().unwrap(),
                case.input["advertisementId"].as_str().unwrap(),
            ),
            "concord_contract_key" => concord_contract_key(
                case.input["contractId"].as_str().unwrap(),
                case.input["generation"].as_u64().unwrap(),
            ),
            "concord_participant_token_key" => concord_participant_token_key(
                case.input["contractId"].as_str().unwrap(),
                case.input["generation"].as_u64().unwrap(),
                &EndpointAddress::parse(case.input["participant"].as_str().unwrap()).unwrap(),
            ),
            other => panic!("unknown key helper {other}"),
        };
        assert_eq!(actual, case.key);
    }
}

#[derive(Debug, Deserialize)]
struct TermsHashVectors {
    cases: Vec<TermsHashCase>,
}

#[derive(Debug, Deserialize)]
struct TermsHashCase {
    #[serde(rename = "canonicalJson")]
    canonical_json: String,
    hash: String,
}

#[test]
fn concord_terms_hash_vectors_match_contract_artifacts() {
    let vectors: TermsHashVectors = serde_json::from_str(include_str!(
        "../../../contract/v1/vectors/concord-terms-hash.v1.json"
    ))
    .unwrap();
    for case in vectors.cases {
        let value: Value = serde_json::from_str(&case.canonical_json).unwrap();
        assert_eq!(
            String::from_utf8(canonical_json_bytes_value(&value).unwrap()).unwrap(),
            case.canonical_json
        );
        assert_eq!(canonical_json_hash_value(&value).unwrap(), case.hash);
    }
}

#[derive(Debug, Deserialize)]
struct NatsLaneVectors {
    cases: Vec<NatsLaneCase>,
}

#[derive(Debug, Deserialize)]
struct NatsLaneCase {
    fixture: String,
    subject: String,
    headers: BTreeMap<String, String>,
}

#[test]
fn nats_lane_vectors_match_contract_artifacts() {
    let vectors: NatsLaneVectors = serde_json::from_str(include_str!(
        "../../../contract/v1/vectors/nats-lane.v1.json"
    ))
    .unwrap();
    for case in vectors.cases {
        let fixture = std::fs::read_to_string(format!("{CONTRACT_ROOT}/{}", case.fixture)).unwrap();
        let message = DeckrMessage::from_text(&fixture).unwrap();
        assert_eq!(subject_for(&message).unwrap(), case.subject);
        let actual_headers = headers_for(&message);
        for (key, value) in case.headers {
            assert_eq!(actual_headers.get(&key), Some(&value), "{key}");
        }
    }
}

#[test]
fn nats_subject_helpers_are_recipient_scoped() {
    let controller = EndpointAddress::parse("controller:controller-main").unwrap();

    assert_eq!(
        direct_subject("actions", &controller).unwrap(),
        "deckr.msg.actions.to.controller.controller-main"
    );
    assert_eq!(
        broadcast_subject("hardware_messages", "controllers", "controller").unwrap(),
        "deckr.msg.hardware_messages.broadcast.controllers.controller"
    );
    assert_eq!(
        endpoint_subscription_subjects("hardware_messages", &controller).unwrap(),
        [
            "deckr.msg.hardware_messages.to.controller.controller-main".to_string(),
            "deckr.msg.hardware_messages.broadcast.*.controller".to_string()
        ]
    );
}

#[test]
fn contract_fixtures_parse_and_enforce_profile_semantics() {
    let hardware_ad: Value = fixture("fixtures/valid/beacon/hardware-advertisement.v1.json");
    let hardware_ad = AdvertisementRecord::from_value(hardware_ad).unwrap();
    hardware_payload_from_advertisement(&hardware_ad).unwrap();

    let hardware_payload: Value = fixture("fixtures/valid/profiles/hardware.v1.json");
    HardwareBeaconPayload::from_value(hardware_payload).unwrap();

    let hardware_claim: Value = fixture("fixtures/valid/profiles/hardware-claim.v1.json");
    HardwareClaimTerms::from_value(hardware_claim).unwrap();

    let contract: Value = fixture("fixtures/valid/concord/hardware-claim-contract.v1.json");
    ContractRecord::from_value(contract).unwrap();

    let token: Value = fixture("fixtures/valid/concord/hardware-claim-token.v1.json");
    ParticipantTokenRecord::from_value(token).unwrap();

    let invalid_hardware_payload: Value =
        fixture("fixtures/invalid/profiles/hardware-missing-manager.v1.json");
    assert!(HardwareBeaconPayload::from_value(invalid_hardware_payload).is_err());

    let invalid_token: Value =
        fixture("fixtures/invalid/concord/token-missing-participant.v1.json");
    assert!(ParticipantTokenRecord::from_value(invalid_token).is_err());

    let invalid_message = std::fs::read_to_string(format!(
        "{CONTRACT_ROOT}/fixtures/invalid/hardware/control-input-missing-device-ref.v1.json"
    ))
    .unwrap();
    assert!(DeckrMessage::from_text(&invalid_message).is_err());
}

#[test]
fn strict_records_reject_non_object_extension_payloads() {
    let mut advertisement: Value = fixture("fixtures/valid/beacon/hardware-advertisement.v1.json");
    advertisement["payload"] = json!("not-an-object");
    assert!(AdvertisementRecord::from_value(advertisement).is_err());

    let mut contract: Value = fixture("fixtures/valid/concord/hardware-claim-contract.v1.json");
    contract["terms"] = json!("not-an-object");
    assert!(ContractRecord::from_value(contract).is_err());
}

#[test]
fn hardware_body_rejects_inventory_message_types() {
    for suffix in ["Available", "DescriptorChanged", "Unavailable"] {
        let message_type = format!("device{suffix}");
        assert!(HardwareMessageBody::from_message(&message_type, &json!({})).is_err());
    }
}

#[test]
fn hardware_body_accepts_all_v1_runtime_message_types() {
    let device_ref = json!({
        "managerId": "mirabox-main",
        "deviceId": "deck",
        "fingerprint": "fingerprint:deck"
    });
    let cases = [
        (
            "capabilityStateChanged",
            json!({
                "deviceRef": device_ref.clone(),
                "controlId": "screen",
                "capabilityId": "raster.bitmap",
                "stateType": "frame",
                "value": null,
                "occurredAt": "2026-04-29T10:00:00Z",
                "sequence": 1
            }),
        ),
        (
            "capabilityStateRequest",
            json!({
                "deviceRef": device_ref.clone(),
                "controlId": "screen",
                "capabilityId": "raster.bitmap",
                "stateType": "frame",
                "params": {}
            }),
        ),
        (
            "capabilityStateReply",
            json!({
                "deviceRef": device_ref.clone(),
                "controlId": "screen",
                "capabilityId": "raster.bitmap",
                "stateType": "frame",
                "status": "ok",
                "value": null
            }),
        ),
        (
            "commandAccepted",
            json!({
                "deviceRef": device_ref.clone(),
                "controlId": "screen",
                "capabilityId": "raster.bitmap",
                "commandType": "clear",
                "acceptedAt": "2026-04-29T10:00:00Z"
            }),
        ),
        (
            "commandRejected",
            json!({
                "deviceRef": device_ref.clone(),
                "controlId": "screen",
                "capabilityId": "raster.bitmap",
                "commandType": "clear",
                "reason": "unauthorized",
                "message": "not claimed"
            }),
        ),
        (
            "commandReply",
            json!({
                "deviceRef": device_ref.clone(),
                "controlId": "screen",
                "capabilityId": "raster.bitmap",
                "commandType": "clear",
                "result": null
            }),
        ),
    ];

    for (message_type, body) in cases {
        let parsed = HardwareMessageBody::from_message(message_type, &body).unwrap();
        assert_eq!(parsed.message_type(), message_type);
    }
}

#[test]
fn rust_beacon_concord_ttl_defaults_match_bucket_policies() {
    assert_eq!(DEFAULT_BEACON_TTL_SECONDS, 300);
    assert_eq!(DEFAULT_CONCORD_TOKEN_TTL_SECONDS, 120);
    assert_eq!(DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS, 60);
    assert_eq!(
        StateMaintenancePolicy::default()
            .concord_token_refresh_interval
            .as_secs(),
        DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS
    );

    assert_eq!(
        beacon_advertisement_store_policy().broker_ttl_seconds,
        Some(DEFAULT_BEACON_TTL_SECONDS)
    );
    assert_eq!(
        concord_token_store_policy().broker_ttl_seconds,
        Some(DEFAULT_CONCORD_TOKEN_TTL_SECONDS)
    );
}

#[tokio::test]
async fn beacon_advertisement_bucket_must_be_ttl_bound() {
    let state = MemoryStateStore::new();
    let endpoint = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let advertiser = BeaconAdvertiser::new(state, "feature", endpoint, "session")
        .advertisement_id("ad-1")
        .payload(json!({}));

    assert!(advertiser.publish().await.is_err());
}

#[tokio::test]
async fn beacon_advertisement_uses_bucket_ttl_and_coalesces_unchanged_refreshes() {
    let state = MemoryStateStore::ttl_bound(1).unwrap();
    let endpoint = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let advertiser = BeaconAdvertiser::new(state.clone(), "feature", endpoint.clone(), "session")
        .advertisement_id("ad-1")
        .payload(json!({"version": 1}))
        .refresh_interval(Duration::from_millis(10));

    let first = advertiser.publish().await.unwrap();
    let first_entry = state.get(&first.key).await.unwrap().unwrap();
    let first_record = AdvertisementRecord::from_value(first_entry.value).unwrap();
    assert_eq!(first_record.ttl_seconds, 1);

    let repeated = advertiser.refresh(&first).await.unwrap();
    assert_eq!(repeated.refresh_seq, first.refresh_seq);
    assert_eq!(repeated.revision, first.revision);

    state.set_ttl_seconds(Some(2)).unwrap();
    let ttl_changed = advertiser.refresh(&repeated).await.unwrap();
    assert_eq!(ttl_changed.refresh_seq, first.refresh_seq + 1);
    assert_ne!(ttl_changed.revision, first.revision);
    let ttl_changed_entry = state.get(&ttl_changed.key).await.unwrap().unwrap();
    assert_eq!(
        AdvertisementRecord::from_value(ttl_changed_entry.value)
            .unwrap()
            .ttl_seconds,
        2
    );

    let changed_advertiser = BeaconAdvertiser::new(state.clone(), "feature", endpoint, "session")
        .advertisement_id("ad-1")
        .payload(json!({"version": 2}))
        .refresh_interval(Duration::from_millis(10));
    let changed = changed_advertiser.refresh(&ttl_changed).await.unwrap();
    assert_eq!(changed.refresh_seq, ttl_changed.refresh_seq + 1);
    assert_ne!(changed.revision, ttl_changed.revision);

    let early = changed_advertiser.refresh(&changed).await.unwrap();
    assert_eq!(early.refresh_seq, changed.refresh_seq);
    assert_eq!(early.revision, changed.revision);

    tokio::time::sleep(Duration::from_millis(1600)).await;
    let due = changed_advertiser.refresh(&early).await.unwrap();
    assert_eq!(due.refresh_seq, early.refresh_seq + 1);
    assert_ne!(due.revision, early.revision);
}

#[tokio::test]
async fn concord_token_bucket_must_be_ttl_bound() {
    let contracts = MemoryStateStore::new();
    let tokens = MemoryStateStore::new();
    let concord = ConcordCoordinator::new(contracts, tokens);
    let controller = EndpointAddress::parse("controller:main").unwrap();
    let manager = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let contract = concord
        .create_contract(CreateContractSpec {
            participants: vec![controller.clone(), manager.clone()],
            contract_id: Some("contract-1".to_string()),
            generation: 1,
            profile: None,
            terms: None,
            created_by: Some(controller),
            supersedes: None,
        })
        .await
        .unwrap();

    assert!(concord
        .attach(&contract, &manager, "manager-session", None)
        .await
        .is_err());
}

#[tokio::test]
async fn concord_token_attach_and_refresh_use_bucket_ttl() {
    let contracts = MemoryStateStore::new();
    let tokens = MemoryStateStore::ttl_bound(1).unwrap();
    let concord = ConcordCoordinator::new(contracts, tokens.clone());
    let controller = EndpointAddress::parse("controller:main").unwrap();
    let manager = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let contract = concord
        .create_contract(CreateContractSpec {
            participants: vec![controller.clone(), manager.clone()],
            contract_id: Some("contract-1".to_string()),
            generation: 1,
            profile: None,
            terms: None,
            created_by: Some(controller),
            supersedes: None,
        })
        .await
        .unwrap();

    let token = concord
        .attach(&contract, &manager, "manager-session", None)
        .await
        .unwrap();
    assert_eq!(token.ttl_seconds, 1);

    tokens.set_ttl_seconds(Some(2)).unwrap();
    let refreshed = concord.refresh(&token).await.unwrap();
    assert_eq!(refreshed.ttl_seconds, 2);
    assert_eq!(refreshed.refresh_seq, token.refresh_seq + 1);
    let stored = concord
        .participant_token(&contract, &manager)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(stored.ttl_seconds, 2);
}

#[tokio::test]
async fn materialized_concord_participant_profile_discovery_uses_cache() {
    let contracts = RecordingStateStore::new(MemoryStateStore::new());
    let tokens = RecordingStateStore::new(token_store());
    let exact = ConcordCoordinator::new(contracts.clone(), tokens.clone());
    let controller = EndpointAddress::parse("controller:main").unwrap();
    let manager = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let contract = exact
        .create_contract(CreateContractSpec {
            participants: vec![controller.clone(), manager.clone()],
            contract_id: Some("materialized-claim-1".to_string()),
            generation: 1,
            profile: Some(HARDWARE_CLAIM_PROFILE_ID.to_string()),
            terms: Some(json!({
                "profile": HARDWARE_CLAIM_PROFILE_ID,
                "claimId": "materialized-claim-1",
                "controllerEndpoint": "controller:main",
                "managerEndpoint": "hardware_manager:mirabox-main",
                "devices": []
            })),
            created_by: Some(controller),
            supersedes: None,
        })
        .await
        .unwrap();
    let materialized = ConcordCoordinator::new(
        MaterializedStateStore::start(contracts.clone(), "contracts.")
            .await
            .unwrap(),
        MaterializedStateStore::start(tokens.clone(), "contracts.")
            .await
            .unwrap(),
    );
    materialized.wait_current().await.unwrap();
    contracts.clear_observations();
    tokens.clear_observations();

    let discovered = materialized
        .contracts_cached(deckr::concord::ContractFilters {
            profile: Some(HARDWARE_CLAIM_PROFILE_ID),
            participant: Some(&manager),
            state: Some(deckr::concord::ContractState::Open),
            ..deckr::concord::ContractFilters::default()
        })
        .unwrap();

    assert_eq!(discovered.len(), 1);
    assert_eq!(discovered[0].key, contract.key);
    assert_eq!(
        materialized.validate_cached(&contract, None).status,
        ContractValidityStatus::NotYetFulfilled
    );
    assert!(
        contracts.items_prefixes().is_empty(),
        "cached discovery must not perform broker prefix scans"
    );
    assert!(
        contracts.get_keys().is_empty(),
        "cached discovery must not perform broker exact gets"
    );
    assert!(
        tokens.get_keys().is_empty(),
        "cached validation must not perform broker token gets"
    );
}

#[test]
fn endpoint_delivery_honors_recipient_session() {
    let command = DeckrMessage::hardware_command(
        "main",
        "controller-session",
        "mirabox-main",
        "manager-session",
        "deck",
        HardwareMessageBody::ControlCommand {
            device_ref: deckr::lanes::DeviceRef {
                manager_id: "mirabox-main".to_string(),
                device_id: "deck".to_string(),
                fingerprint: None,
            },
            control_id: Some("key-1".to_string()),
            capability_id: "dev.deckr.controls.raster.v1".to_string(),
            command_type: "setFrame".to_string(),
            params: Default::default(),
        },
    )
    .unwrap();
    let manager = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();

    assert!(
        message_is_deliverable_to(&command, &manager, "manager-session").unwrap(),
        "matching endpoint session should receive direct command"
    );
    assert!(
        !message_is_deliverable_to(&command, &manager, "stale-session").unwrap(),
        "stale endpoint session must not receive direct command"
    );
}

#[tokio::test]
async fn concord_validation_distinguishes_pending_missing_and_lost_tokens() {
    let contracts = MemoryStateStore::new();
    let tokens = token_store();
    let concord = ConcordCoordinator::new(contracts.clone(), tokens.clone());
    let controller = EndpointAddress::parse("controller:main").unwrap();
    let manager = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let terms = json!({
        "profile": HARDWARE_CLAIM_PROFILE_ID,
        "claimId": "claim-1",
        "controllerEndpoint": "controller:main",
        "managerEndpoint": "hardware_manager:mirabox-main",
        "devices": [{
            "deviceRef": {"managerId": "mirabox-main", "deviceId": "deck"},
            "instanceCount": 1
        }]
    });
    let contract = concord
        .create_contract(CreateContractSpec {
            participants: vec![manager.clone(), controller.clone()],
            contract_id: Some("contract-1".to_string()),
            generation: 1,
            profile: Some(HARDWARE_CLAIM_PROFILE_ID.to_string()),
            terms: Some(terms),
            created_by: Some(controller.clone()),
            supersedes: None,
        })
        .await
        .unwrap();

    assert_eq!(
        concord.validate(&contract, None).await.status,
        ContractValidityStatus::NotYetFulfilled
    );

    let controller_token = concord
        .attach(
            &contract,
            &controller,
            "controller-session",
            Some("controller-token".into()),
        )
        .await
        .unwrap();
    assert_eq!(
        concord.validate(&contract, None).await.status,
        ContractValidityStatus::NotYetFulfilled
    );

    let manager_token = concord
        .attach(
            &contract,
            &manager,
            "manager-session",
            Some("manager-token".into()),
        )
        .await
        .unwrap();
    assert_eq!(
        concord.validate(&contract, None).await.status,
        ContractValidityStatus::Valid
    );

    tokens
        .delete(&manager_token.key, Some(manager_token.revision))
        .await
        .unwrap();
    assert_eq!(
        concord.validate(&contract, None).await.status,
        ContractValidityStatus::MissingToken
    );
    assert!(concord
        .attach(
            &contract,
            &manager,
            "manager-session",
            Some("replacement".into())
        )
        .await
        .is_err());

    tokens
        .delete(&controller_token.key, Some(controller_token.revision))
        .await
        .unwrap();
    assert_eq!(
        concord.validate(&contract, None).await.status,
        ContractValidityStatus::MissingToken
    );
}

#[tokio::test]
async fn concord_participant_manager_does_not_resurrect_lost_authority() {
    let contracts = MemoryStateStore::new();
    let tokens = token_store();
    let concord = ConcordCoordinator::new(contracts, tokens.clone());
    let controller = EndpointAddress::parse("controller:main").unwrap();
    let manager = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let contract = concord
        .create_contract(CreateContractSpec {
            participants: vec![manager.clone(), controller.clone()],
            contract_id: Some("contract-1".to_string()),
            generation: 1,
            profile: None,
            terms: None,
            created_by: Some(controller.clone()),
            supersedes: None,
        })
        .await
        .unwrap();
    concord
        .attach(
            &contract,
            &controller,
            "controller-session",
            Some("controller-token".into()),
        )
        .await
        .unwrap();

    let mut manager_lifecycle =
        ConcordParticipantManager::new(concord.clone(), manager.clone(), "manager-session".into())
            .unwrap();
    let managed = manager_lifecycle
        .reconcile(|_, _| Ok(true), None)
        .await
        .unwrap();
    assert_eq!(managed.len(), 1);
    assert_eq!(managed[0].validity.status, ContractValidityStatus::Valid);

    let manager_token = managed[0].token.clone().unwrap();
    tokens
        .delete(&manager_token.key, Some(manager_token.revision))
        .await
        .unwrap();

    let managed = manager_lifecycle
        .reconcile(|_, _| Ok(true), None)
        .await
        .unwrap();
    assert!(managed.is_empty());
    assert_eq!(
        concord.validate(&contract, None).await.status,
        ContractValidityStatus::Cancelled
    );
    let record = concord.contract_record(&contract).await.unwrap().unwrap();
    assert_eq!(record.state, ContractState::Cancelled);
    assert_eq!(
        record.cancel_reason.as_deref(),
        Some("concord_managed_missing_token")
    );
    assert!(concord
        .participant_token(&contract, &manager)
        .await
        .unwrap()
        .is_none());

    let managed = manager_lifecycle
        .reconcile(|_, _| Ok(true), None)
        .await
        .unwrap();
    assert!(managed.is_empty());
    assert!(concord
        .participant_token(&contract, &manager)
        .await
        .unwrap()
        .is_none());
}

#[tokio::test]
async fn concord_participant_manager_session_mismatch_cancels_before_accept() {
    let (concord, _tokens, contract, manager, mut lifecycle) =
        managed_claim_context("contract-1").await;

    let managed = lifecycle.reconcile(|_, _| Ok(true), None).await.unwrap();
    assert_eq!(managed.len(), 1);
    let manager_token = managed[0].token.clone().unwrap();

    lifecycle.session_id = "manager-session-new".to_string();
    let managed = lifecycle
        .reconcile(
            |_, _| panic!("stale terminal validation should happen before accept"),
            None,
        )
        .await
        .unwrap();

    assert!(managed.is_empty());
    assert_eq!(
        concord.validate(&contract, None).await.status,
        ContractValidityStatus::Cancelled
    );
    let record = concord.contract_record(&contract).await.unwrap().unwrap();
    assert_eq!(record.state, ContractState::Cancelled);
    assert_eq!(
        record.cancel_reason.as_deref(),
        Some("concord_managed_session_mismatch")
    );
    let current_token = concord
        .participant_token(&contract, &manager)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(current_token.token_id, manager_token.token_id);
    assert_eq!(current_token.session_id, "manager-session");
}

#[tokio::test]
async fn concord_participant_manager_discovers_from_filtered_contracts() {
    let contracts = RecordingStateStore::new(MemoryStateStore::new());
    let tokens = token_store();
    let concord = ConcordCoordinator::new(contracts.clone(), tokens.clone());
    let controller = EndpointAddress::parse("controller:main").unwrap();
    let manager = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let other_manager = EndpointAddress::parse("hardware_manager:other").unwrap();
    let contract = concord
        .create_contract(CreateContractSpec {
            participants: vec![controller.clone(), manager.clone()],
            contract_id: Some("hardware-contract-1".to_string()),
            generation: 1,
            profile: Some(HARDWARE_CLAIM_PROFILE_ID.to_string()),
            terms: Some(json!({
                "profile": HARDWARE_CLAIM_PROFILE_ID,
                "claimId": "claim-1",
                "controllerEndpoint": "controller:main",
                "managerEndpoint": "hardware_manager:mirabox-main",
                "devices": []
            })),
            created_by: Some(controller.clone()),
            supersedes: None,
        })
        .await
        .unwrap();
    concord
        .create_contract(CreateContractSpec {
            participants: vec![controller.clone(), manager.clone()],
            contract_id: Some("other-profile-contract".to_string()),
            generation: 1,
            profile: Some("dev.deckr.profile.other.v1".to_string()),
            terms: None,
            created_by: Some(controller.clone()),
            supersedes: None,
        })
        .await
        .unwrap();
    concord
        .create_contract(CreateContractSpec {
            participants: vec![controller.clone(), other_manager],
            contract_id: Some("other-participant-contract".to_string()),
            generation: 1,
            profile: Some(HARDWARE_CLAIM_PROFILE_ID.to_string()),
            terms: Some(json!({
                "profile": HARDWARE_CLAIM_PROFILE_ID,
                "claimId": "claim-2",
                "controllerEndpoint": "controller:main",
                "managerEndpoint": "hardware_manager:other",
                "devices": []
            })),
            created_by: Some(controller.clone()),
            supersedes: None,
        })
        .await
        .unwrap();
    concord
        .attach(
            &contract,
            &controller,
            "controller-session",
            Some("controller-token".into()),
        )
        .await
        .unwrap();
    let mut lifecycle =
        ConcordParticipantManager::new(concord.clone(), manager.clone(), "manager-session".into())
            .unwrap()
            .profile(HARDWARE_CLAIM_PROFILE_ID.to_string());
    contracts.clear_observations();

    let managed = lifecycle.reconcile(|_, _| Ok(true), None).await.unwrap();

    assert_eq!(
        managed
            .iter()
            .map(|managed| managed.contract.contract_id.as_str())
            .collect::<Vec<_>>(),
        vec!["hardware-contract-1"]
    );
    assert_eq!(contracts.items_prefixes(), vec!["contracts.".to_string()]);
    assert!(
        contracts.get_keys().iter().all(|key| key == &contract.key),
        "reconcile should exact-read only accepted filtered contracts"
    );
}

#[tokio::test]
async fn concord_participant_manager_reuses_fresh_token_without_refresh() {
    let (concord, _tokens, contract, manager, mut lifecycle) =
        managed_claim_context("contract-1").await;

    let managed = lifecycle.reconcile(|_, _| Ok(true), None).await.unwrap();
    let first_token = managed[0].token.clone().unwrap();

    let managed = lifecycle.reconcile(|_, _| Ok(true), None).await.unwrap();
    let second_token = managed[0].token.clone().unwrap();
    let stored_token = concord
        .participant_token(&contract, &manager)
        .await
        .unwrap()
        .unwrap();

    assert_eq!(second_token.refresh_seq, first_token.refresh_seq);
    assert_eq!(second_token.revision, first_token.revision);
    assert_eq!(stored_token.refresh_seq, first_token.refresh_seq);
    assert_eq!(stored_token.revision, first_token.revision);
}

#[tokio::test]
async fn concord_participant_lease_public_refresh_path_is_rate_limited() {
    let (concord, _tokens, contract, manager, _lifecycle) =
        managed_claim_context("contract-1").await;
    let mut lease =
        ConcordParticipantLease::new(contract.clone(), manager.clone(), "manager-session".into())
            .unwrap()
            .with_token_refresh_interval(Duration::from_millis(10));

    let first_token = lease.attach_or_refresh(&concord).await.unwrap();
    let repeated_token = lease.attach_or_refresh(&concord).await.unwrap();
    let stored_token = concord
        .participant_token(&contract, &manager)
        .await
        .unwrap()
        .unwrap();

    assert_eq!(repeated_token.refresh_seq, first_token.refresh_seq);
    assert_eq!(repeated_token.revision, first_token.revision);
    assert_eq!(stored_token.refresh_seq, first_token.refresh_seq);
    assert_eq!(stored_token.revision, first_token.revision);

    tokio::time::sleep(Duration::from_millis(850)).await;
    let refreshed_token = lease.attach_or_refresh(&concord).await.unwrap();

    assert_eq!(refreshed_token.refresh_seq, first_token.refresh_seq + 1);
    assert_ne!(refreshed_token.revision, first_token.revision);
}

#[tokio::test]
async fn concord_participant_lease_adopted_token_is_not_immediately_refreshed() {
    let (concord, _tokens, contract, manager, _lifecycle) =
        managed_claim_context("contract-1").await;
    let manager_token = concord
        .attach(
            &contract,
            &manager,
            "manager-session",
            Some("manager-token".into()),
        )
        .await
        .unwrap();
    let mut lease =
        ConcordParticipantLease::new(contract.clone(), manager.clone(), "manager-session".into())
            .unwrap()
            .with_token_refresh_interval(Duration::from_millis(10));

    lease.adopt(manager_token.clone()).unwrap();
    let adopted_token = lease.attach_or_refresh(&concord).await.unwrap();

    assert_eq!(adopted_token.refresh_seq, manager_token.refresh_seq);
    assert_eq!(adopted_token.revision, manager_token.revision);

    tokio::time::sleep(Duration::from_millis(850)).await;
    let refreshed_token = lease.attach_or_refresh(&concord).await.unwrap();

    assert_eq!(refreshed_token.refresh_seq, manager_token.refresh_seq + 1);
    assert_ne!(refreshed_token.revision, manager_token.revision);
}

#[tokio::test]
async fn concord_participant_manager_refreshes_due_token() {
    let (concord, _tokens, contract, manager, lifecycle) =
        managed_claim_context("contract-1").await;
    let mut lifecycle = lifecycle.token_refresh_interval(Duration::from_millis(10));

    let managed = lifecycle.reconcile(|_, _| Ok(true), None).await.unwrap();
    let first_token = managed[0].token.clone().unwrap();

    tokio::time::sleep(Duration::from_millis(850)).await;
    let managed = lifecycle.reconcile(|_, _| Ok(true), None).await.unwrap();
    let second_token = managed[0].token.clone().unwrap();
    let stored_token = concord
        .participant_token(&contract, &manager)
        .await
        .unwrap()
        .unwrap();

    assert_eq!(first_token.refresh_seq, 1);
    assert_eq!(second_token.refresh_seq, 2);
    assert_eq!(stored_token.refresh_seq, 2);
    assert_ne!(second_token.revision, first_token.revision);
}

#[tokio::test]
async fn concord_participant_manager_reconcile_managed_does_not_discover_new_contracts() {
    let (concord, _tokens, first_contract, manager, mut lifecycle) =
        managed_claim_context("contract-1").await;
    let controller = EndpointAddress::parse("controller:main").unwrap();

    let managed = lifecycle.reconcile(|_, _| Ok(true), None).await.unwrap();
    assert_eq!(managed.len(), 1);

    let second_contract = concord
        .create_contract(CreateContractSpec {
            participants: vec![controller.clone(), manager.clone()],
            contract_id: Some("contract-2".to_string()),
            generation: 1,
            profile: Some(HARDWARE_CLAIM_PROFILE_ID.to_string()),
            terms: Some(json!({
                "profile": HARDWARE_CLAIM_PROFILE_ID,
                "claimId": "claim-2",
                "controllerEndpoint": "controller:main",
                "managerEndpoint": "hardware_manager:mirabox-main",
                "devices": []
            })),
            created_by: Some(controller.clone()),
            supersedes: None,
        })
        .await
        .unwrap();
    concord
        .attach(
            &second_contract,
            &controller,
            "controller-session",
            Some("controller-token-2".into()),
        )
        .await
        .unwrap();

    let managed = lifecycle.reconcile_managed(None).await.unwrap();

    assert_eq!(managed.len(), 1);
    assert_eq!(managed[0].contract.key, first_contract.key);
    assert!(concord
        .participant_token(&second_contract, &manager)
        .await
        .unwrap()
        .is_none());
}

#[tokio::test]
async fn concord_participant_manager_reconcile_managed_does_not_resurrect_deleted_token() {
    let (concord, tokens, contract, manager, mut lifecycle) =
        managed_claim_context("contract-1").await;

    let managed = lifecycle.reconcile(|_, _| Ok(true), None).await.unwrap();
    let manager_token = managed[0].token.clone().unwrap();
    tokens
        .delete(&manager_token.key, Some(manager_token.revision))
        .await
        .unwrap();

    let managed = lifecycle.reconcile_managed(None).await.unwrap();

    assert!(managed.is_empty());
    assert_eq!(
        concord.validate(&contract, None).await.status,
        ContractValidityStatus::Cancelled
    );
    let record = concord.contract_record(&contract).await.unwrap().unwrap();
    assert_eq!(record.state, ContractState::Cancelled);
    assert_eq!(
        record.cancel_reason.as_deref(),
        Some("concord_managed_missing_token")
    );
    assert!(concord
        .participant_token(&contract, &manager)
        .await
        .unwrap()
        .is_none());
}

#[tokio::test]
async fn concord_contract_notifications_include_contract_and_token_details() {
    let contracts = MemoryStateStore::new();
    let tokens = token_store();
    let concord = ConcordCoordinator::new(contracts, tokens);
    let controller = EndpointAddress::parse("controller:main").unwrap();
    let manager = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let mut stream = concord
        .watch_contract_notifications(Some(HARDWARE_CLAIM_PROFILE_ID), None)
        .await
        .unwrap();

    let contract = concord
        .create_contract(CreateContractSpec {
            participants: vec![controller.clone(), manager.clone()],
            contract_id: Some("contract-watch-1".to_string()),
            generation: 1,
            profile: Some(HARDWARE_CLAIM_PROFILE_ID.to_string()),
            terms: Some(json!({
                "profile": HARDWARE_CLAIM_PROFILE_ID,
                "claimId": "contract-watch-1",
                "controllerEndpoint": "controller:main",
                "managerEndpoint": "hardware_manager:mirabox-main",
                "devices": []
            })),
            created_by: Some(controller.clone()),
            supersedes: None,
        })
        .await
        .unwrap();

    let notification = stream.next().await.unwrap();
    assert_eq!(notification.source, ConcordNotificationSource::Contract);
    assert_eq!(notification.contract_id, contract.contract_id);
    assert_eq!(notification.generation, contract.generation);
    assert_eq!(
        notification.profile.as_deref(),
        Some(HARDWARE_CLAIM_PROFILE_ID)
    );
    assert_eq!(notification.contract.unwrap().key, contract.key);

    concord
        .attach(
            &contract,
            &controller,
            "controller-session",
            Some("controller-token".into()),
        )
        .await
        .unwrap();

    let mut saw_controller_token = false;
    for _ in 0..4 {
        let notification = tokio::time::timeout(Duration::from_millis(100), stream.next())
            .await
            .expect("expected token notification")
            .unwrap();
        if notification.source == ConcordNotificationSource::Token
            && notification.participant.as_ref() == Some(&controller)
        {
            saw_controller_token = true;
            break;
        }
    }
    assert!(saw_controller_token);
}

#[tokio::test]
async fn concord_participant_manager_notification_discovers_new_contract() {
    let contracts = MemoryStateStore::new();
    let tokens = token_store();
    let concord = ConcordCoordinator::new(contracts, tokens);
    let controller = EndpointAddress::parse("controller:main").unwrap();
    let manager = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let mut stream = concord
        .watch_contract_notifications(Some(HARDWARE_CLAIM_PROFILE_ID), None)
        .await
        .unwrap();

    let contract = concord
        .create_contract(CreateContractSpec {
            participants: vec![controller.clone(), manager.clone()],
            contract_id: Some("contract-watch-2".to_string()),
            generation: 1,
            profile: Some(HARDWARE_CLAIM_PROFILE_ID.to_string()),
            terms: Some(json!({
                "profile": HARDWARE_CLAIM_PROFILE_ID,
                "claimId": "contract-watch-2",
                "controllerEndpoint": "controller:main",
                "managerEndpoint": "hardware_manager:mirabox-main",
                "devices": []
            })),
            created_by: Some(controller.clone()),
            supersedes: None,
        })
        .await
        .unwrap();
    let notification = stream.next().await.unwrap();
    concord
        .attach(
            &contract,
            &controller,
            "controller-session",
            Some("controller-token".into()),
        )
        .await
        .unwrap();

    let mut lifecycle =
        ConcordParticipantManager::new(concord.clone(), manager.clone(), "manager-session".into())
            .unwrap()
            .profile(HARDWARE_CLAIM_PROFILE_ID.to_string());
    let managed = lifecycle
        .reconcile_notification(&notification, |_, _| Ok(true), None)
        .await
        .unwrap();

    assert_eq!(managed.len(), 1);
    assert_eq!(managed[0].contract.key, contract.key);
    assert_eq!(managed[0].validity.status, ContractValidityStatus::Valid);
    assert!(concord
        .participant_token(&contract, &manager)
        .await
        .unwrap()
        .is_some());
}

#[tokio::test]
async fn concord_refresh_returns_latest_token_after_revision_race() {
    let contracts = MemoryStateStore::new();
    let tokens = RacingUpdateStore::new(token_store());
    let concord = ConcordCoordinator::new(contracts.clone(), tokens.clone());
    let controller = EndpointAddress::parse("controller:main").unwrap();
    let manager = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let contract = concord
        .create_contract(CreateContractSpec {
            participants: vec![manager, controller.clone()],
            contract_id: Some("contract-1".to_string()),
            generation: 1,
            profile: Some(HARDWARE_CLAIM_PROFILE_ID.to_string()),
            terms: Some(json!({
                "profile": HARDWARE_CLAIM_PROFILE_ID,
                "claimId": "claim-1",
                "controllerEndpoint": "controller:main",
                "managerEndpoint": "hardware_manager:mirabox-main",
                "devices": []
            })),
            created_by: Some(controller.clone()),
            supersedes: None,
        })
        .await
        .unwrap();
    let controller_token = concord
        .attach(
            &contract,
            &controller,
            "controller-session",
            Some("controller-token".into()),
        )
        .await
        .unwrap();

    let refreshed = concord.refresh(&controller_token).await.unwrap();

    assert!(tokens.raced());
    assert_eq!(refreshed.refresh_seq, 2);
    assert_ne!(refreshed.revision, controller_token.revision);
}

#[test]
fn endpoint_semantics_reject_non_core_and_reserved_addresses() {
    assert!(EndpointAddress::parse("controller:main").is_ok());
    assert!(EndpointAddress::parse("hardware_manager:mirabox-main").is_ok());
    assert!(EndpointAddress::parse("action_provider:clock-main").is_ok());
    assert!(EndpointAddress::parse("driver:mirabox-main").is_err());
    assert!(EndpointAddress::parse("controller: main").is_err());
    assert!(EndpointAddress::parse("action_provider:dev.deckr.controller.builtin").is_err());
}

async fn managed_claim_context(
    contract_id: &str,
) -> (
    ConcordCoordinator<MemoryStateStore, MemoryStateStore>,
    MemoryStateStore,
    ContractHandle,
    EndpointAddress,
    ConcordParticipantManager<MemoryStateStore, MemoryStateStore>,
) {
    let contracts = MemoryStateStore::new();
    let tokens = short_token_store();
    let concord = ConcordCoordinator::new(contracts, tokens.clone());
    let controller = EndpointAddress::parse("controller:main").unwrap();
    let manager = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let lifecycle =
        ConcordParticipantManager::new(concord.clone(), manager.clone(), "manager-session".into())
            .unwrap()
            .profile(HARDWARE_CLAIM_PROFILE_ID.to_string());
    let contract = concord
        .create_contract(CreateContractSpec {
            participants: vec![controller.clone(), manager.clone()],
            contract_id: Some(contract_id.to_string()),
            generation: 1,
            profile: Some(HARDWARE_CLAIM_PROFILE_ID.to_string()),
            terms: Some(json!({
                "profile": HARDWARE_CLAIM_PROFILE_ID,
                "claimId": contract_id,
                "controllerEndpoint": "controller:main",
                "managerEndpoint": "hardware_manager:mirabox-main",
                "devices": []
            })),
            created_by: Some(controller.clone()),
            supersedes: None,
        })
        .await
        .unwrap();
    concord
        .attach(
            &contract,
            &controller,
            "controller-session",
            Some(format!("{contract_id}-controller-token")),
        )
        .await
        .unwrap();

    (concord, tokens, contract, manager, lifecycle)
}

fn token_store() -> MemoryStateStore {
    MemoryStateStore::ttl_bound(DEFAULT_CONCORD_TOKEN_TTL_SECONDS).unwrap()
}

fn short_token_store() -> MemoryStateStore {
    MemoryStateStore::ttl_bound(1).unwrap()
}

fn fixture(path: &str) -> Value {
    serde_json::from_str(&std::fs::read_to_string(format!("{CONTRACT_ROOT}/{path}")).unwrap())
        .unwrap()
}
