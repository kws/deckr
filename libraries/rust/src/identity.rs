use std::{fmt, str::FromStr, sync::LazyLock};

use chrono::{DateTime, Duration, Utc};
use regex::Regex;
use serde::{de, Deserialize, Deserializer, Serialize, Serializer};
use serde_json::Value;
use thiserror::Error;

pub const ACTIONS_LANE: &str = "actions";
pub const HARDWARE_MESSAGES_LANE: &str = "hardware_messages";
pub const SERVICES_LANE: &str = "services";

static PROVIDER_INSTANCE_ID_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9._-]*$").expect("valid regex"));
const RESERVED_ACTION_PROVIDER_INSTANCE_IDS: &[&str] = &["dev.deckr.controller.builtin"];

#[derive(Debug, Error)]
pub enum IdentityError {
    #[error("endpoint address must have shape '<endpoint_family>:<endpoint_id>'")]
    InvalidEndpointShape,
    #[error("unknown endpoint family {0}")]
    UnknownEndpointFamily(String),
    #[error("endpoint id must not be empty")]
    EmptyEndpointId,
    #[error("endpoint id must not contain ':'")]
    InvalidEndpointId,
    #[error("action provider endpoint id is not a valid provider instance id")]
    InvalidActionProviderId,
    #[error("action provider endpoint id {0} is reserved")]
    ReservedActionProviderId(String),
    #[error("message lane {0} is not a core Deckr lane")]
    UnknownLane(String),
    #[error("message type {message_type} is not supported on lane {lane}")]
    UnsupportedMessageType { lane: String, message_type: String },
    #[error("sender family {family} is not allowed on lane {lane}")]
    UnsupportedSender { lane: String, family: String },
    #[error("recipient family {family} is not allowed on lane {lane}")]
    UnsupportedRecipient { lane: String, family: String },
    #[error("broadcast target {scope}:{family} is not allowed on lane {lane}")]
    UnsupportedBroadcast {
        lane: String,
        scope: String,
        family: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct EndpointAddress {
    pub family: String,
    pub endpoint_id: String,
}

impl EndpointAddress {
    pub fn new(
        family: impl Into<String>,
        endpoint_id: impl Into<String>,
    ) -> Result<Self, IdentityError> {
        let family = family.into();
        let endpoint_id = endpoint_id.into();
        if family.trim() != family || endpoint_id.trim() != endpoint_id {
            return Err(IdentityError::InvalidEndpointId);
        }
        if !matches!(
            family.as_str(),
            "action_provider" | "controller" | "hardware_manager" | "service"
        ) {
            return Err(IdentityError::UnknownEndpointFamily(family));
        }
        if endpoint_id.is_empty() {
            return Err(IdentityError::EmptyEndpointId);
        }
        if endpoint_id.contains(':') {
            return Err(IdentityError::InvalidEndpointId);
        }
        if family == "action_provider" {
            if RESERVED_ACTION_PROVIDER_INSTANCE_IDS.contains(&endpoint_id.as_str()) {
                return Err(IdentityError::ReservedActionProviderId(endpoint_id));
            }
            if !PROVIDER_INSTANCE_ID_RE.is_match(&endpoint_id) {
                return Err(IdentityError::InvalidActionProviderId);
            }
        }
        Ok(Self {
            family,
            endpoint_id,
        })
    }
}

pub fn endpoint_address(
    family: impl Into<String>,
    endpoint_id: impl Into<String>,
) -> Result<EndpointAddress, IdentityError> {
    EndpointAddress::new(family, endpoint_id)
}

pub fn action_provider_address(
    provider_instance_id: &str,
) -> Result<EndpointAddress, IdentityError> {
    EndpointAddress::new("action_provider", provider_instance_id)
}

pub fn controller_address(controller_id: &str) -> Result<EndpointAddress, IdentityError> {
    EndpointAddress::new("controller", controller_id)
}

pub fn hardware_manager_address(manager_id: &str) -> Result<EndpointAddress, IdentityError> {
    EndpointAddress::new("hardware_manager", manager_id)
}

pub fn service_address(service_id: &str) -> Result<EndpointAddress, IdentityError> {
    EndpointAddress::new("service", service_id)
}

impl fmt::Display for EndpointAddress {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}", self.family, self.endpoint_id)
    }
}

impl FromStr for EndpointAddress {
    type Err = IdentityError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let (family, endpoint_id) = value
            .split_once(':')
            .ok_or(IdentityError::InvalidEndpointShape)?;
        if endpoint_id.is_empty() {
            return Err(IdentityError::EmptyEndpointId);
        }
        if endpoint_id.contains(':') {
            return Err(IdentityError::InvalidEndpointId);
        }
        EndpointAddress::new(family, endpoint_id)
    }
}

impl Serialize for EndpointAddress {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&self.to_string())
    }
}

impl<'de> Deserialize<'de> for EndpointAddress {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        EndpointAddress::from_str(&value).map_err(de::Error::custom)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EndpointTarget {
    pub endpoint: EndpointAddress,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct BroadcastTarget {
    pub scope: String,
    pub endpoint_family: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub domain: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hop_limit: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "targetType", rename_all = "camelCase")]
pub enum MessageTarget {
    #[serde(rename = "endpoint")]
    Endpoint(EndpointTarget),
    #[serde(rename = "broadcast")]
    Broadcast(BroadcastTarget),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EntitySubject {
    pub kind: String,
    #[serde(default)]
    pub identifiers: serde_json::Map<String, Value>,
}

impl EntitySubject {
    pub fn device_id(&self) -> Option<&str> {
        self.identifiers.get("deviceId").and_then(Value::as_str)
    }

    pub fn manager_id(&self) -> Option<&str> {
        self.identifiers.get("managerId").and_then(Value::as_str)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DeckrMessage {
    pub message_id: String,
    pub protocol_version: String,
    pub schema_version: String,
    pub lane: String,
    pub message_type: String,
    pub sender: EndpointAddress,
    pub sender_session_id: String,
    pub recipient: MessageTarget,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recipient_session_id: Option<String>,
    pub subject: EntitySubject,
    pub created_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ttl_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub in_reply_to: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub causation_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace: Option<Value>,
    pub body: Value,
}

impl DeckrMessage {
    pub fn to_text(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(self)
    }

    pub fn from_text(text: &str) -> Result<Self, serde_json::Error> {
        serde_json::from_str(text)
    }

    pub fn from_bytes(bytes: &[u8]) -> Result<Self, serde_json::Error> {
        serde_json::from_slice(bytes)
    }

    pub fn recipient_endpoint(&self) -> Option<&EndpointAddress> {
        match &self.recipient {
            MessageTarget::Endpoint(target) => Some(&target.endpoint),
            MessageTarget::Broadcast(_) => None,
        }
    }

    pub fn is_expired(&self) -> bool {
        message_is_expired_at(self, Utc::now())
    }
}

pub fn message_targets_endpoint(message: &DeckrMessage, endpoint: &EndpointAddress) -> bool {
    match &message.recipient {
        MessageTarget::Endpoint(target) => &target.endpoint == endpoint,
        MessageTarget::Broadcast(target) => target.endpoint_family == endpoint.family,
    }
}

pub fn message_expires_at(message: &DeckrMessage) -> Option<DateTime<Utc>> {
    let ttl_expiry = message
        .ttl_ms
        .map(|ttl_ms| message.created_at + Duration::milliseconds(ttl_ms));
    match (message.expires_at, ttl_expiry) {
        (Some(explicit), Some(ttl)) => Some(std::cmp::min(explicit, ttl)),
        (Some(explicit), None) => Some(explicit),
        (None, Some(ttl)) => Some(ttl),
        (None, None) => None,
    }
}

pub fn message_is_expired_at(message: &DeckrMessage, now: DateTime<Utc>) -> bool {
    message_expires_at(message).is_some_and(|expires_at| expires_at <= now)
}

pub fn validate_lane_message(message: &DeckrMessage) -> Result<(), IdentityError> {
    let contract = lane_contract(message.lane.as_str())
        .ok_or_else(|| IdentityError::UnknownLane(message.lane.clone()))?;
    if !contract
        .message_types
        .contains(&message.message_type.as_str())
    {
        return Err(IdentityError::UnsupportedMessageType {
            lane: message.lane.clone(),
            message_type: message.message_type.clone(),
        });
    }
    if !contract
        .sender_families
        .contains(&message.sender.family.as_str())
    {
        return Err(IdentityError::UnsupportedSender {
            lane: message.lane.clone(),
            family: message.sender.family.clone(),
        });
    }
    match &message.recipient {
        MessageTarget::Endpoint(target) => {
            if !contract
                .recipient_families
                .contains(&target.endpoint.family.as_str())
            {
                return Err(IdentityError::UnsupportedRecipient {
                    lane: message.lane.clone(),
                    family: target.endpoint.family.clone(),
                });
            }
        }
        MessageTarget::Broadcast(target) => {
            if !contract
                .recipient_families
                .contains(&target.endpoint_family.as_str())
            {
                return Err(IdentityError::UnsupportedRecipient {
                    lane: message.lane.clone(),
                    family: target.endpoint_family.clone(),
                });
            }
            if !contract
                .broadcast_targets
                .iter()
                .any(|(scope, family)| *scope == target.scope && *family == target.endpoint_family)
            {
                return Err(IdentityError::UnsupportedBroadcast {
                    lane: message.lane.clone(),
                    scope: target.scope.clone(),
                    family: target.endpoint_family.clone(),
                });
            }
        }
    }
    Ok(())
}

struct LaneContract {
    sender_families: &'static [&'static str],
    recipient_families: &'static [&'static str],
    message_types: &'static [&'static str],
    broadcast_targets: &'static [(&'static str, &'static str)],
}

fn lane_contract(lane: &str) -> Option<LaneContract> {
    match lane {
        ACTIONS_LANE => Some(LaneContract {
            sender_families: &["action_provider", "controller"],
            recipient_families: &["action_provider", "controller"],
            message_types: &[
                "actionExtension",
                "actionInstanceCreated",
                "actionInstanceDestroyed",
                "bindingAttached",
                "bindingDetached",
                "bindingOverlay",
                "bindingOverlayClear",
                "bindingOutput",
                "capabilityInput",
                "closePage",
                "openPage",
                "pageSessionClosed",
                "pageSessionOpened",
                "replacePage",
                "settingsRequest",
                "settingsPatch",
                "settingsReplace",
                "settingsSnapshot",
            ],
            broadcast_targets: &[("controllers", "controller")],
        }),
        HARDWARE_MESSAGES_LANE => Some(LaneContract {
            sender_families: &["controller", "hardware_manager"],
            recipient_families: &["controller", "hardware_manager"],
            message_types: &[
                "capabilityStateChanged",
                "capabilityStateReply",
                "capabilityStateRequest",
                "commandAccepted",
                "commandRejected",
                "commandReply",
                "controlCommand",
                "controlInput",
                "deviceAvailable",
                "deviceDescriptorChanged",
                "deviceUnavailable",
            ],
            broadcast_targets: &[
                ("controllers", "controller"),
                ("hardware_managers", "hardware_manager"),
            ],
        }),
        SERVICES_LANE => Some(LaneContract {
            sender_families: &["controller", "service"],
            recipient_families: &["controller", "service"],
            message_types: &["serviceCommand", "serviceCommandReply"],
            broadcast_targets: &[],
        }),
        _ => None,
    }
}

pub fn context_subject(
    context_id: &str,
    provider_instance_id: Option<&str>,
    provider_id: Option<&str>,
    config_id: Option<&str>,
    action_instance_id: Option<&str>,
    binding_id: Option<&str>,
) -> EntitySubject {
    let mut identifiers = serde_json::Map::new();
    identifiers.insert(
        "contextId".to_string(),
        Value::String(context_id.to_string()),
    );
    if let Some(value) = provider_instance_id {
        identifiers.insert(
            "providerInstanceId".to_string(),
            Value::String(value.to_string()),
        );
    }
    if let Some(value) = provider_id {
        identifiers.insert("providerId".to_string(), Value::String(value.to_string()));
    }
    if let Some(value) = config_id {
        identifiers.insert("configId".to_string(), Value::String(value.to_string()));
    }
    if let Some(value) = action_instance_id {
        identifiers.insert(
            "actionInstanceId".to_string(),
            Value::String(value.to_string()),
        );
    }
    if let Some(value) = binding_id {
        identifiers.insert("bindingId".to_string(), Value::String(value.to_string()));
    }
    EntitySubject {
        kind: "context".to_string(),
        identifiers,
    }
}

pub fn hardware_subject_for_capability(
    device_ref: &Value,
    control_id: Option<&str>,
    capability_id: &str,
) -> EntitySubject {
    let mut identifiers = serde_json::Map::new();
    if let Some(manager_id) = device_ref.get("managerId").and_then(Value::as_str) {
        identifiers.insert(
            "managerId".to_string(),
            Value::String(manager_id.to_string()),
        );
    }
    if let Some(device_id) = device_ref.get("deviceId").and_then(Value::as_str) {
        identifiers.insert("deviceId".to_string(), Value::String(device_id.to_string()));
    }
    if let Some(value) = control_id {
        identifiers.insert("controlId".to_string(), Value::String(value.to_string()));
    }
    identifiers.insert(
        "capabilityId".to_string(),
        Value::String(capability_id.to_string()),
    );
    EntitySubject {
        kind: "hardware_capability".to_string(),
        identifiers,
    }
}
