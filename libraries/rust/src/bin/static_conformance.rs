use std::{
    collections::BTreeSet,
    path::{Path, PathBuf},
};

use chrono::{DateTime, Utc};
use clap::Parser;
use deckr_core::{
    action_provider_catalog_key, context_subject, decode_key_token, default_contract_root,
    device_claim_key, encode_key_token, hardware_body_from_message, hardware_inventory_key,
    hardware_subject_for_capability, headers_for, load_manifest, message_is_expired_at,
    message_targets_endpoint, parse_action_provider_catalog_key, parse_device_claim_key,
    parse_hardware_inventory_key, parse_presence_endpoint_key, parse_service_catalog_key,
    parse_service_status_key, parse_service_view_key, parse_settings_target_key,
    payload_json_bytes, presence_endpoint_key, read_json, service_catalog_key, service_status_key,
    service_view_key, settings_target_key, subject_for, subscribe_subject_for_lane,
    validate_lane_message, DeckrMessage, EndpointAddress, DECKR_NATS_HEADERS,
    DEFAULT_DISCOVERY_STATE_BUCKET, DEFAULT_LEASE_STATE_BUCKET, HARDWARE_MESSAGES_LANE,
    LANE_SUBJECT_PREFIX, LANE_SUBJECT_TEMPLATE, LANE_SUBSCRIBE_TEMPLATE, NATS_BINDING_PATH,
    NATS_BINDING_SCHEMA_ID, REQUIRED_DECKR_NATS_HEADERS, STATE_RENEWAL_INTERVAL_SECONDS,
    STATE_TTL_SECONDS,
};
use jsonschema::{Draft, JSONSchema};
use serde::Serialize;
use serde_json::{json, Value};

const REQUIRED_GROUP_IDS: &[&str] = &[
    "artifacts.manifest",
    "schemas.fixtures",
    "vectors.keys",
    "vectors.identity",
    "messages.actions",
    "messages.hardware",
    "messages.services",
    "runtime.lane",
    "substrate.nats",
];

#[derive(Debug, Parser)]
#[command(about = "Run static Deckr contract conformance for the Rust core library.")]
struct Args {
    #[arg(long)]
    contract_root: Option<PathBuf>,
    #[arg(long)]
    output: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
struct Report {
    schema: &'static str,
    #[serde(rename = "contractVersion")]
    contract_version: String,
    #[serde(rename = "specVersion")]
    spec_version: String,
    implementation: Implementation,
    roles: Vec<&'static str>,
    groups: Vec<GroupResult>,
    summary: Summary,
}

#[derive(Debug, Serialize)]
struct Implementation {
    language: &'static str,
    package: &'static str,
    version: &'static str,
}

#[derive(Debug, Serialize)]
struct Summary {
    status: &'static str,
    passed: usize,
    failed: usize,
    skipped: usize,
}

#[derive(Debug, Serialize)]
struct GroupResult {
    id: &'static str,
    status: &'static str,
    passed: usize,
    failed: usize,
    skipped: usize,
    diagnostics: Vec<String>,
}

impl GroupResult {
    fn new(id: &'static str) -> Self {
        Self {
            id,
            status: "passed",
            passed: 0,
            failed: 0,
            skipped: 0,
            diagnostics: Vec::new(),
        }
    }

    fn pass(&mut self) {
        self.passed += 1;
        self.update_status();
    }

    fn fail(&mut self, diagnostic: impl Into<String>) {
        self.failed += 1;
        self.diagnostics.push(diagnostic.into());
        self.update_status();
    }

    fn update_status(&mut self) {
        self.status = if self.failed > 0 {
            "failed"
        } else if self.passed == 0 && self.skipped > 0 {
            "skipped"
        } else {
            "passed"
        };
    }
}

fn main() -> std::process::ExitCode {
    let args = Args::parse();
    let root = match args.contract_root {
        Some(path) => path,
        None => match default_contract_root() {
            Ok(path) => path,
            Err(error) => {
                eprintln!("{error}");
                return std::process::ExitCode::FAILURE;
            }
        },
    };
    let report = match run_conformance(&root) {
        Ok(report) => report,
        Err(error) => {
            eprintln!("{error}");
            return std::process::ExitCode::FAILURE;
        }
    };
    let output = serde_json::to_string_pretty(&report).expect("report serializes");
    if let Some(path) = args.output {
        if let Some(parent) = path.parent() {
            if let Err(error) = std::fs::create_dir_all(parent) {
                eprintln!("failed to create {}: {error}", parent.display());
                return std::process::ExitCode::FAILURE;
            }
        }
        if let Err(error) = std::fs::write(&path, format!("{output}\n")) {
            eprintln!("failed to write {}: {error}", path.display());
            return std::process::ExitCode::FAILURE;
        }
    } else {
        println!("{output}");
    }
    if report.summary.failed == 0 {
        std::process::ExitCode::SUCCESS
    } else {
        std::process::ExitCode::FAILURE
    }
}

fn run_conformance(root: &Path) -> Result<Report, Box<dyn std::error::Error>> {
    let manifest = load_manifest(root)?;
    let groups = vec![
        check_manifest(root, &manifest),
        check_fixtures(root, &manifest),
        check_key_vectors(root),
        check_identity_vectors(root),
        check_lane_messages(
            root,
            &manifest,
            "messages.actions",
            "schemas/actions/actions.v1.schema.json",
            "actions",
        ),
        check_lane_messages(
            root,
            &manifest,
            "messages.hardware",
            "schemas/hardware/hardware-messages.v1.schema.json",
            "hardware_messages",
        ),
        check_lane_messages(
            root,
            &manifest,
            "messages.services",
            "schemas/services/services.v1.schema.json",
            "services",
        ),
        check_lane_runtime_vectors(root),
        check_nats_lane_vectors(root),
    ];
    let passed = groups.iter().map(|group| group.passed).sum();
    let failed = groups.iter().map(|group| group.failed).sum();
    let skipped = groups.iter().map(|group| group.skipped).sum();
    Ok(Report {
        schema: "dev.deckr.interop.report.v1",
        contract_version: string_at(&manifest, "contractVersion").unwrap_or_default(),
        spec_version: string_at(&manifest, "specVersion").unwrap_or_default(),
        implementation: Implementation {
            language: "rust",
            package: "deckr-core",
            version: env!("CARGO_PKG_VERSION"),
        },
        roles: vec!["validator"],
        groups,
        summary: Summary {
            status: if failed > 0 { "failed" } else { "passed" },
            passed,
            failed,
            skipped,
        },
    })
}

fn check_manifest(root: &Path, manifest: &Value) -> GroupResult {
    let mut group = GroupResult::new("artifacts.manifest");
    if root.join("manifest.json").exists() {
        group.pass();
    } else {
        group.fail("Missing manifest.json");
    }
    if root.join("asyncapi.json").exists() {
        group.pass();
    } else {
        group.fail("Missing asyncapi.json");
    }
    if string_at(manifest, "bundle").as_deref() == Some("deckr-contract-v1") {
        group.pass();
    } else {
        group.fail("Unexpected bundle id");
    }
    if REQUIRED_GROUP_IDS == planned_groups().as_slice() {
        group.pass();
    } else {
        group.fail("Static runner group ids drifted from required ids");
    }
    group
}

fn check_fixtures(root: &Path, manifest: &Value) -> GroupResult {
    let mut group = GroupResult::new("schemas.fixtures");
    for artifact in artifacts(manifest) {
        if artifact.get("kind").and_then(Value::as_str) != Some("fixture") {
            continue;
        }
        let schema_path = match artifact.get("schemaPath").and_then(Value::as_str) {
            Some(path) => path,
            None => {
                group.fail("fixture artifact lacks schemaPath");
                continue;
            }
        };
        let fixture_path = match artifact.get("path").and_then(Value::as_str) {
            Some(path) => path,
            None => {
                group.fail("fixture artifact lacks path");
                continue;
            }
        };
        let valid = artifact
            .get("valid")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let mut schema = match read_json(root.join(schema_path)) {
            Ok(schema) => schema,
            Err(error) => {
                group.fail(format!("{fixture_path}: schema read failed: {error}"));
                continue;
            }
        };
        normalize_json_schema_ids(&mut schema);
        let fixture = match read_json(root.join(fixture_path)) {
            Ok(fixture) => fixture,
            Err(error) => {
                group.fail(format!("{fixture_path}: fixture read failed: {error}"));
                continue;
            }
        };
        let compiled = match JSONSchema::options()
            .with_draft(Draft::Draft202012)
            .should_validate_formats(true)
            .compile(&schema)
        {
            Ok(compiled) => compiled,
            Err(error) => {
                group.fail(format!("{fixture_path}: schema compile failed: {error}"));
                continue;
            }
        };
        let errors = compiled
            .validate(&fixture)
            .err()
            .map(|errors| errors.count())
            .unwrap_or(0);
        if valid && errors > 0 {
            group.fail(format!("{fixture_path}: expected valid, got errors"));
        } else if !valid && errors == 0 {
            group.fail(format!("{fixture_path}: expected invalid, got no errors"));
        } else {
            group.pass();
        }
    }
    group
}

fn check_key_vectors(root: &Path) -> GroupResult {
    let mut group = GroupResult::new("vectors.keys");
    let key_vectors = match read_json(root.join("vectors/key-tokens.v1.json")) {
        Ok(value) => value,
        Err(error) => {
            group.fail(error.to_string());
            return group;
        }
    };
    for case in cases(&key_vectors) {
        if !case_is_valid(case) {
            check_invalid_key_token_case(&mut group, case);
            continue;
        }
        let raw = case["raw"].as_str().unwrap_or_default();
        let encoded = encode_key_token(raw);
        let decoded = decode_key_token(case["encoded"].as_str().unwrap_or_default());
        if encoded != case["encoded"] {
            group.fail(format!("{}: encoded as {encoded}", raw));
        } else if decoded.ok().as_deref() != case["decoded"].as_str() {
            group.fail(format!("{}: decoded mismatch", case["encoded"]));
        } else {
            group.pass();
        }
    }

    let state_vectors = match read_json(root.join("vectors/state-keys.v1.json")) {
        Ok(value) => value,
        Err(error) => {
            group.fail(error.to_string());
            return group;
        }
    };
    for case in cases(&state_vectors) {
        if !case_is_valid(case) {
            check_invalid_state_key_case(&mut group, case);
            continue;
        }
        match state_key_case(case) {
            Ok((key, parsed)) if key == case["key"] && parsed == case["parsed"] => group.pass(),
            Ok((key, parsed)) => group.fail(format!(
                "{}: key/parsed mismatch: {key} {parsed}",
                case["id"]
            )),
            Err(error) => group.fail(format!("{}: {error}", case["id"])),
        }
    }
    group
}

fn case_is_valid(case: &Value) -> bool {
    case.get("valid").and_then(Value::as_bool).unwrap_or(true)
}

fn check_invalid_key_token_case(group: &mut GroupResult, case: &Value) {
    match case["operation"].as_str().unwrap_or_default() {
        "decode_key_token" => {
            match decode_key_token(case["encoded"].as_str().unwrap_or_default()) {
                Ok(_) => group.fail(format!("{}: invalid key-token accepted", case["id"])),
                Err(_) => group.pass(),
            }
        }
        operation => group.fail(format!(
            "{}: unknown invalid key-token operation {operation}",
            case["id"]
        )),
    }
}

fn check_invalid_state_key_case(group: &mut GroupResult, case: &Value) {
    match invalid_state_key_case(case) {
        Ok(None) | Err(_) => group.pass(),
        Ok(Some(parsed)) => group.fail(format!(
            "{}: invalid state-key parsed as {parsed}",
            case["id"]
        )),
    }
}

fn invalid_state_key_case(case: &Value) -> Result<Option<Value>, Box<dyn std::error::Error>> {
    let key = case["key"].as_str().unwrap_or_default();
    match case["helper"].as_str().unwrap_or_default() {
        "parse_presence_endpoint_key" => {
            Ok(parse_presence_endpoint_key(key)?.map(|parsed| {
                json!({"lane": parsed.0, "endpoint": parsed.1.to_string()})
            }))
        }
        "parse_hardware_inventory_key" => {
            Ok(parse_hardware_inventory_key(key)?.map(|manager_id| {
                json!({"managerId": manager_id})
            }))
        }
        "parse_device_claim_key" => Ok(parse_device_claim_key(key)?
            .map(|parsed| json!({"managerId": parsed.0, "deviceId": parsed.1}))),
        "parse_action_provider_catalog_key" => Ok(parse_action_provider_catalog_key(key)?
            .map(|provider_instance_id| json!({"providerInstanceId": provider_instance_id}))),
        "parse_service_catalog_key" => {
            Ok(parse_service_catalog_key(key)?.map(|service_id| json!({"serviceId": service_id})))
        }
        "parse_service_status_key" => {
            Ok(parse_service_status_key(key)?.map(|service_id| json!({"serviceId": service_id})))
        }
        "parse_service_view_key" => Ok(parse_service_view_key(key)?.map(|parsed| {
            json!({"serviceId": parsed.0, "serviceNamespace": parsed.1, "tokens": parsed.2})
        })),
        "parse_settings_target_key" => Ok(parse_settings_target_key(key)?),
        helper => Err(format!("unknown invalid state-key helper {helper}").into()),
    }
}

fn state_key_case(case: &Value) -> Result<(String, Value), Box<dyn std::error::Error>> {
    let helper = case["helper"].as_str().unwrap_or_default();
    let input = &case["input"];
    match helper {
        "presence_endpoint_key" => {
            let endpoint: EndpointAddress =
                input["endpoint"].as_str().unwrap_or_default().parse()?;
            let key = presence_endpoint_key(input["lane"].as_str().unwrap_or_default(), &endpoint);
            let parsed = parse_presence_endpoint_key(&key)?.expect("presence key parses");
            Ok((
                key,
                json!({"lane": parsed.0, "endpoint": parsed.1.to_string()}),
            ))
        }
        "hardware_inventory_key" => {
            let key = hardware_inventory_key(input["managerId"].as_str().unwrap_or_default());
            Ok((
                key.clone(),
                json!({"managerId": parse_hardware_inventory_key(&key)?}),
            ))
        }
        "device_claim_key" => {
            let key = device_claim_key(
                input["managerId"].as_str().unwrap_or_default(),
                input["deviceId"].as_str().unwrap_or_default(),
            );
            let parsed = parse_device_claim_key(&key)?.expect("device claim key parses");
            Ok((key, json!({"managerId": parsed.0, "deviceId": parsed.1})))
        }
        "action_provider_catalog_key" => {
            let key = action_provider_catalog_key(
                input["providerInstanceId"].as_str().unwrap_or_default(),
            );
            Ok((
                key.clone(),
                json!({"providerInstanceId": parse_action_provider_catalog_key(&key)?}),
            ))
        }
        "service_catalog_key" => {
            let key = service_catalog_key(input["serviceId"].as_str().unwrap_or_default());
            Ok((
                key.clone(),
                json!({"serviceId": parse_service_catalog_key(&key)?}),
            ))
        }
        "service_status_key" => {
            let key = service_status_key(input["serviceId"].as_str().unwrap_or_default());
            Ok((
                key.clone(),
                json!({"serviceId": parse_service_status_key(&key)?}),
            ))
        }
        "service_view_key" => {
            let tokens: Vec<String> = input["tokens"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect();
            let key = service_view_key(
                input["serviceId"].as_str().unwrap_or_default(),
                input["serviceNamespace"].as_str().unwrap_or_default(),
                &tokens,
            );
            let parsed = parse_service_view_key(&key)?.expect("service view key parses");
            Ok((
                key,
                json!({"serviceId": parsed.0, "serviceNamespace": parsed.1, "tokens": parsed.2}),
            ))
        }
        "settings_target_key" => {
            let target = &input["target"];
            let key = settings_target_key(target);
            let parsed = parse_settings_target_key(&key)?.expect("settings target key parses");
            Ok((key, json!({"target": parsed})))
        }
        _ => Err(format!("unknown helper {helper}").into()),
    }
}

fn check_identity_vectors(root: &Path) -> GroupResult {
    let mut group = GroupResult::new("vectors.identity");
    let vector = match read_json(root.join("vectors/identity.v1.json")) {
        Ok(value) => value,
        Err(error) => {
            group.fail(error.to_string());
            return group;
        }
    };
    for case in vector["endpointCases"].as_array().into_iter().flatten() {
        let parsed = case["input"]
            .as_str()
            .unwrap_or_default()
            .parse::<EndpointAddress>();
        if case["valid"].as_bool() == Some(false) {
            if parsed.is_err() {
                group.pass();
            } else {
                group.fail(format!("{}: invalid endpoint accepted", case["id"]));
            }
            continue;
        }
        match parsed {
            Ok(endpoint)
                if endpoint.family == case["family"].as_str().unwrap_or_default()
                    && endpoint.endpoint_id == case["endpointId"].as_str().unwrap_or_default() =>
            {
                group.pass();
            }
            Ok(endpoint) => group.fail(format!("{}: parsed as {endpoint}", case["id"])),
            Err(error) => group.fail(format!("{}: {error}", case["id"])),
        }
    }
    for case in vector["subjectCases"].as_array().into_iter().flatten() {
        match subject_case(case) {
            Ok(subject) if subject == case["subject"] => group.pass(),
            Ok(subject) => group.fail(format!("{}: subject {subject}", case["id"])),
            Err(error) => group.fail(format!("{}: {error}", case["id"])),
        }
    }
    group
}

fn subject_case(case: &Value) -> Result<Value, Box<dyn std::error::Error>> {
    let input = &case["input"];
    match case["helper"].as_str().unwrap_or_default() {
        "context_subject" => Ok(serde_json::to_value(context_subject(
            input["contextId"].as_str().unwrap_or_default(),
            input["providerInstanceId"].as_str(),
            input["providerId"].as_str(),
            input["configId"].as_str(),
            input["actionInstanceId"].as_str(),
            input["bindingId"].as_str(),
        ))?),
        "hardware_subject_for_capability" => {
            Ok(serde_json::to_value(hardware_subject_for_capability(
                &input["deviceRef"],
                input["controlId"].as_str(),
                input["capabilityId"].as_str().unwrap_or_default(),
            ))?)
        }
        helper => Err(format!("unknown subject helper {helper}").into()),
    }
}

fn check_lane_messages(
    root: &Path,
    manifest: &Value,
    group_id: &'static str,
    schema_path: &str,
    lane: &str,
) -> GroupResult {
    let mut group = GroupResult::new(group_id);
    let mut seen_types = BTreeSet::new();
    for artifact in artifacts(manifest) {
        if artifact.get("kind").and_then(Value::as_str) != Some("fixture")
            || artifact.get("schemaPath").and_then(Value::as_str) != Some(schema_path)
            || artifact.get("valid").and_then(Value::as_bool) != Some(true)
        {
            continue;
        }
        let path = artifact["path"].as_str().unwrap_or_default();
        match read_json(root.join(path)).and_then(|value| {
            serde_json::from_value::<DeckrMessage>(value).map_err(|source| {
                deckr_core::artifacts::ArtifactError::Json {
                    path: root.join(path),
                    source,
                }
            })
        }) {
            Ok(message) if message.lane == lane => {
                seen_types.insert(message.message_type.clone());
                if let Err(error) = validate_lane_message(&message) {
                    group.fail(format!("{path}: {error}"));
                } else if lane == deckr_core::HARDWARE_MESSAGES_LANE {
                    if let Err(error) = hardware_body_from_message(&message) {
                        group.fail(format!("{path}: {error}"));
                    } else {
                        group.pass();
                    }
                } else if !message.body.is_object() {
                    group.fail(format!("{path}: message body is not an object"));
                } else {
                    group.pass();
                }
            }
            Ok(message) => group.fail(format!(
                "{path}: expected lane {lane}, got {}",
                message.lane
            )),
            Err(error) => group.fail(format!("{path}: {error}")),
        }
    }
    if seen_types.is_empty() {
        group.fail(format!("{group_id}: no valid message fixtures"));
    }
    group
}

fn check_lane_runtime_vectors(root: &Path) -> GroupResult {
    let mut group = GroupResult::new("runtime.lane");
    let vector = match read_json(root.join("vectors/lane-runtime.v1.json")) {
        Ok(value) => value,
        Err(error) => {
            group.fail(error.to_string());
            return group;
        }
    };
    for case in cases(&vector) {
        let payload = match case.get("message") {
            Some(value) => Ok(value.clone()),
            None => read_json(root.join(case["fixture"].as_str().unwrap_or_default())),
        };
        let message: DeckrMessage = match payload.and_then(|value| {
            serde_json::from_value(value).map_err(|source| {
                deckr_core::artifacts::ArtifactError::Json {
                    path: root.join("vectors/lane-runtime.v1.json"),
                    source,
                }
            })
        }) {
            Ok(message) => message,
            Err(error) => {
                group.fail(format!("{}: {error}", case["id"]));
                continue;
            }
        };
        let endpoint = match case["endpoint"]
            .as_str()
            .unwrap_or_default()
            .parse::<EndpointAddress>()
        {
            Ok(endpoint) => endpoint,
            Err(error) => {
                group.fail(format!("{}: {error}", case["id"]));
                continue;
            }
        };
        let now = match DateTime::parse_from_rfc3339(case["now"].as_str().unwrap_or_default()) {
            Ok(now) => now.with_timezone(&Utc),
            Err(error) => {
                group.fail(format!("{}: {error}", case["id"]));
                continue;
            }
        };
        let expired = message_is_expired_at(&message, now);
        let targets = message_targets_endpoint(&message, &endpoint);
        let session_matches = message
            .recipient_session_id
            .as_ref()
            .is_none_or(|session| Some(session.as_str()) == case["endpointSessionId"].as_str());
        let deliverable =
            !expired && targets && session_matches && validate_lane_message(&message).is_ok();
        if expired != case["expired"].as_bool().unwrap_or(false) {
            group.fail(format!("{}: expired {expired}", case["id"]));
        } else if targets != case["targetsEndpoint"].as_bool().unwrap_or(false) {
            group.fail(format!("{}: targetsEndpoint {targets}", case["id"]));
        } else if deliverable != case["deliverable"].as_bool().unwrap_or(false) {
            group.fail(format!("{}: deliverable {deliverable}", case["id"]));
        } else {
            group.pass();
        }
    }
    group
}

fn check_nats_lane_vectors(root: &Path) -> GroupResult {
    let mut group = GroupResult::new("substrate.nats");
    match read_json(root.join(NATS_BINDING_PATH)) {
        Ok(binding) => {
            let lane_messages = &binding["laneMessages"];
            let buckets = &binding["currentState"]["buckets"];
            let headers = lane_messages["headers"]
                .as_array()
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|item| item["name"].as_str())
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            let required_headers = lane_messages["headers"]
                .as_array()
                .map(|items| {
                    items
                        .iter()
                        .filter(|item| item["required"].as_bool().unwrap_or(false))
                        .filter_map(|item| item["name"].as_str())
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            let binding_ok = binding["schema"] == json!(NATS_BINDING_SCHEMA_ID)
                && lane_messages["subjectRoot"] == json!(LANE_SUBJECT_PREFIX)
                && lane_messages["publishSubjectTemplate"] == json!(LANE_SUBJECT_TEMPLATE)
                && lane_messages["subscribeSubjectTemplate"] == json!(LANE_SUBSCRIBE_TEMPLATE)
                && headers == DECKR_NATS_HEADERS
                && required_headers == REQUIRED_DECKR_NATS_HEADERS
                && lane_messages["lanes"][HARDWARE_MESSAGES_LANE]["subscribeSubject"]
                    == json!(subscribe_subject_for_lane(HARDWARE_MESSAGES_LANE))
                && buckets["lease"]["name"] == json!(DEFAULT_LEASE_STATE_BUCKET)
                && buckets["lease"]["brokerTtlSeconds"] == json!(STATE_TTL_SECONDS)
                && buckets["lease"]["renewalIntervalSeconds"].as_f64()
                    == Some(STATE_RENEWAL_INTERVAL_SECONDS as f64)
                && buckets["discovery"]["name"] == json!(DEFAULT_DISCOVERY_STATE_BUCKET)
                && buckets["discovery"]["brokerTtlSeconds"].is_null();
            if binding_ok {
                group.pass();
            } else {
                group.fail("NATS binding artifact disagrees with Rust helpers");
            }
        }
        Err(error) => group.fail(format!("NATS binding check failed: {error}")),
    }

    let vector = match read_json(root.join("vectors/nats-lane.v1.json")) {
        Ok(value) => value,
        Err(error) => {
            group.fail(error.to_string());
            return group;
        }
    };
    for case in cases(&vector) {
        let path = case["fixture"].as_str().unwrap_or_default();
        let message: DeckrMessage = match read_json(root.join(path)).and_then(|value| {
            serde_json::from_value(value).map_err(|source| {
                deckr_core::artifacts::ArtifactError::Json {
                    path: root.join(path),
                    source,
                }
            })
        }) {
            Ok(message) => message,
            Err(error) => {
                group.fail(format!("{}: {error}", case["id"]));
                continue;
            }
        };
        let payload_value = payload_json_bytes(&message)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
        if subject_for(&message) != case["subject"].as_str().unwrap_or_default() {
            group.fail(format!("{}: subject mismatch", case["id"]));
        } else if serde_json::to_value(headers_for(&message)).ok().as_ref() != case.get("headers") {
            group.fail(format!("{}: headers mismatch", case["id"]));
        } else if payload_value.as_ref()
            != case["payloadUtf8"]
                .as_str()
                .and_then(|raw| serde_json::from_str::<Value>(raw).ok())
                .as_ref()
        {
            group.fail(format!("{}: payload JSON mismatch", case["id"]));
        } else {
            group.pass();
        }
    }
    group
}

fn artifacts(manifest: &Value) -> impl Iterator<Item = &Value> {
    manifest["artifacts"].as_array().into_iter().flatten()
}

fn cases(vector: &Value) -> impl Iterator<Item = &Value> {
    vector["cases"].as_array().into_iter().flatten()
}

fn string_at(value: &Value, field: &str) -> Option<String> {
    value.get(field).and_then(Value::as_str).map(str::to_string)
}

fn planned_groups() -> Vec<&'static str> {
    REQUIRED_GROUP_IDS.to_vec()
}

fn normalize_json_schema_ids(value: &mut Value) {
    match value {
        Value::Object(map) => {
            if let Some(Value::String(schema_id)) = map.get_mut("$id") {
                if !schema_id.contains(':') {
                    *schema_id = format!("urn:{schema_id}");
                }
            }
            for child in map.values_mut() {
                normalize_json_schema_ids(child);
            }
        }
        Value::Array(items) => {
            for child in items {
                normalize_json_schema_ids(child);
            }
        }
        _ => {}
    }
}
