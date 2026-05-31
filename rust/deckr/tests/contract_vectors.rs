use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use deckr::beacon::{beacon_advertisement_key, AdvertisementRecord};
use deckr::canonical_json::{canonical_json_bytes_value, canonical_json_hash_value};
use deckr::concord::{
    concord_contract_key, concord_participant_token_key, ConcordCoordinator,
    ConcordParticipantManager, ContractRecord, ContractValidityStatus, ParticipantTokenRecord,
};
use deckr::endpoint::EndpointAddress;
use deckr::keys::{decode_key_token, encode_key_token};
use deckr::lanes::{
    headers_for, message_is_deliverable_to, subject_for, DeckrMessage, HardwareMessageBody,
};
use deckr::profiles::hardware::{
    hardware_payload_from_advertisement, HardwareBeaconPayload, HardwareClaimTerms,
    HARDWARE_CLAIM_PROFILE_ID,
};
use deckr::state::{MemoryStateStore, StateEntry, StateStore};
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

impl StateStore for RacingUpdateStore {
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
    let tokens = MemoryStateStore::new();
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
        .create_contract(
            vec![manager.clone(), controller.clone()],
            Some("contract-1".to_string()),
            1,
            Some(HARDWARE_CLAIM_PROFILE_ID.to_string()),
            Some(terms),
            Some(controller.clone()),
        )
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
    let tokens = MemoryStateStore::new();
    let concord = ConcordCoordinator::new(contracts, tokens.clone());
    let controller = EndpointAddress::parse("controller:main").unwrap();
    let manager = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let contract = concord
        .create_contract(
            vec![manager.clone(), controller.clone()],
            Some("contract-1".to_string()),
            1,
            None,
            None,
            Some(controller.clone()),
        )
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
        ConcordParticipantManager::new(concord.clone(), manager, "manager-session".into()).unwrap();
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
        ContractValidityStatus::MissingToken
    );

    let managed = manager_lifecycle
        .reconcile(|_, _| Ok(true), None)
        .await
        .unwrap();
    assert!(managed.is_empty());
}

#[tokio::test]
async fn concord_refresh_returns_latest_token_after_revision_race() {
    let contracts = MemoryStateStore::new();
    let tokens = RacingUpdateStore::new(MemoryStateStore::new());
    let concord = ConcordCoordinator::new(contracts.clone(), tokens.clone());
    let controller = EndpointAddress::parse("controller:main").unwrap();
    let manager = EndpointAddress::parse("hardware_manager:mirabox-main").unwrap();
    let contract = concord
        .create_contract(
            vec![manager, controller.clone()],
            Some("contract-1".to_string()),
            1,
            Some(HARDWARE_CLAIM_PROFILE_ID.to_string()),
            Some(json!({
                "profile": HARDWARE_CLAIM_PROFILE_ID,
                "claimId": "claim-1",
                "controllerEndpoint": "controller:main",
                "managerEndpoint": "hardware_manager:mirabox-main",
                "devices": []
            })),
            Some(controller.clone()),
        )
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

fn fixture(path: &str) -> Value {
    serde_json::from_str(&std::fs::read_to_string(format!("{CONTRACT_ROOT}/{path}")).unwrap())
        .unwrap()
}
