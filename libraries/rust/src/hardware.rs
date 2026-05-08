use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;
use uuid::Uuid;

use crate::identity::{
    hardware_manager_address, hardware_subject_for_capability as subject_for_capability_value,
    BroadcastTarget, DeckrMessage, EndpointAddress, EndpointTarget, EntitySubject, IdentityError,
    MessageTarget, HARDWARE_MESSAGES_LANE,
};

pub const HARDWARE_MESSAGES_SCHEMA_ID: &str = "dev.deckr.message.hardware_messages.v1";
pub const DECKR_PROTOCOL_VERSION: &str = "1";

pub const DEVICE_AVAILABLE: &str = "deviceAvailable";
pub const DEVICE_DESCRIPTOR_CHANGED: &str = "deviceDescriptorChanged";
pub const DEVICE_UNAVAILABLE: &str = "deviceUnavailable";
pub const CONTROL_INPUT: &str = "controlInput";
pub const CONTROL_COMMAND: &str = "controlCommand";
pub const CAPABILITY_STATE_CHANGED: &str = "capabilityStateChanged";
pub const CAPABILITY_STATE_REQUEST: &str = "capabilityStateRequest";
pub const CAPABILITY_STATE_REPLY: &str = "capabilityStateReply";
pub const COMMAND_ACCEPTED: &str = "commandAccepted";
pub const COMMAND_REJECTED: &str = "commandRejected";
pub const COMMAND_REPLY: &str = "commandReply";

#[derive(Debug, Error)]
pub enum HardwareError {
    #[error(transparent)]
    Identity(#[from] IdentityError),
    #[error(transparent)]
    Serde(#[from] serde_json::Error),
    #[error("unsupported hardware message type {0}")]
    UnsupportedMessageType(String),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DeviceRef {
    pub manager_id: String,
    pub device_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fingerprint: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ControlRef {
    pub device_ref: DeviceRef,
    pub control_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CapabilityRef {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub device_ref: Option<DeviceRef>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub control_id: Option<String>,
    pub capability_id: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DescriptorCapabilityRef {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub control_id: Option<String>,
    pub capability_id: String,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ControlGeometry {
    #[serde(default)]
    pub x: f64,
    #[serde(default)]
    pub y: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub width: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub height: Option<f64>,
    #[serde(default = "default_geometry_unit")]
    pub unit: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rotation: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub layer: Option<i64>,
}

fn default_geometry_unit() -> String {
    "grid".to_string()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CapabilitySchema {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub schema_id: Option<String>,
    pub schema: Value,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CapabilityConstraint {
    #[serde(rename = "type")]
    pub constraint_type: String,
    pub subject: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub value: Option<Value>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub values: Vec<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub minimum: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub maximum: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub step: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CapabilityUnit {
    pub subject: String,
    pub unit: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub symbol: Option<String>,
    #[serde(default = "default_unit_scale")]
    pub scale: f64,
}

fn default_unit_scale() -> f64 {
    1.0
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CapabilityProjection {
    #[serde(rename = "type", default = "default_projection_type")]
    pub projection_type: String,
    pub owner: String,
    pub source: DescriptorCapabilityRef,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
}

fn default_projection_type() -> String {
    "projection".to_string()
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CapabilityDescriptor {
    pub capability_id: String,
    pub family: String,
    #[serde(rename = "type")]
    pub capability_type: String,
    pub direction: String,
    pub access: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub value_schema: Option<CapabilitySchema>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub command_schema: Option<CapabilitySchema>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub constraints: Vec<CapabilityConstraint>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub units: Vec<CapabilityUnit>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub event_types: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub command_types: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub projection: Option<CapabilityProjection>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub sources: Vec<DeviceSourceReference>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ControlDescriptor {
    pub control_id: String,
    pub kind: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub group_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_control_id: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub related_control_ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub surface_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub geometry: Option<ControlGeometry>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub input_capabilities: Vec<CapabilityDescriptor>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub output_capabilities: Vec<CapabilityDescriptor>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub state_capabilities: Vec<CapabilityDescriptor>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub config_capabilities: Vec<CapabilityDescriptor>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub diagnostic_capabilities: Vec<CapabilityDescriptor>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub sources: Vec<DeviceSourceReference>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DeviceIdentifier {
    #[serde(rename = "type")]
    pub identifier_type: String,
    pub value: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub namespace: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub issuer: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DeviceConnection {
    pub connection_id: String,
    #[serde(rename = "type")]
    pub connection_type: String,
    #[serde(default = "default_connection_status")]
    pub status: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub transport: Option<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub facts: BTreeMap<String, Value>,
}

fn default_connection_status() -> String {
    "available".to_string()
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DeviceSourceReference {
    pub source_id: String,
    #[serde(rename = "type")]
    pub source_type: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub connection_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub facts: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DeviceDescriptor {
    pub device_id: String,
    pub fingerprint: String,
    pub display_name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub manufacturer: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub serial_number: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hardware_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub firmware_version: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub identifiers: Vec<DeviceIdentifier>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub connections: Vec<DeviceConnection>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent: Option<DeviceRef>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub default_status_indicator: Option<DescriptorCapabilityRef>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub controls: Vec<ControlDescriptor>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub capabilities: Vec<CapabilityDescriptor>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub sources: Vec<DeviceSourceReference>,
}

pub fn hardware_subject_for_device(ref_: &DeviceRef) -> EntitySubject {
    let mut identifiers = serde_json::Map::new();
    identifiers.insert(
        "managerId".to_string(),
        Value::String(ref_.manager_id.clone()),
    );
    identifiers.insert(
        "deviceId".to_string(),
        Value::String(ref_.device_id.clone()),
    );
    EntitySubject {
        kind: "hardware_device".to_string(),
        identifiers,
    }
}

pub fn hardware_subject_for_capability(ref_: &CapabilityRef) -> EntitySubject {
    let device = ref_
        .device_ref
        .as_ref()
        .map(|device| serde_json::to_value(device).expect("DeviceRef serializes"))
        .unwrap_or(Value::Null);
    subject_for_capability_value(&device, ref_.control_id.as_deref(), &ref_.capability_id)
}

#[derive(Debug, Clone, PartialEq)]
pub enum HardwareMessageBody {
    DeviceAvailable {
        descriptor: DeviceDescriptor,
    },
    DeviceDescriptorChanged {
        descriptor: DeviceDescriptor,
    },
    DeviceUnavailable {
        device_ref: DeviceRef,
        reason: Option<String>,
    },
    ControlInput {
        device_ref: DeviceRef,
        control_id: String,
        capability_id: String,
        event_type: String,
        value: Option<Value>,
        sequence: Option<u64>,
        occurred_at: Option<DateTime<Utc>>,
        sources: Vec<DeviceSourceReference>,
    },
    ControlCommand {
        device_ref: DeviceRef,
        control_id: Option<String>,
        capability_id: String,
        command_type: String,
        params: serde_json::Map<String, Value>,
    },
    CapabilityStateChanged {
        device_ref: DeviceRef,
        capability_id: String,
        value: Option<Value>,
        control_id: Option<String>,
        state_type: Option<String>,
        sequence: Option<u64>,
        occurred_at: Option<DateTime<Utc>>,
    },
    CapabilityStateRequest {
        device_ref: DeviceRef,
        capability_id: String,
        control_id: Option<String>,
        state_type: Option<String>,
        params: serde_json::Map<String, Value>,
    },
    CapabilityStateReply {
        device_ref: DeviceRef,
        capability_id: String,
        status: String,
        value: Option<Value>,
        control_id: Option<String>,
        state_type: Option<String>,
        error: Option<String>,
    },
    CommandAccepted {
        device_ref: DeviceRef,
        control_id: Option<String>,
        capability_id: String,
        command_type: String,
        accepted_at: Option<DateTime<Utc>>,
    },
    CommandRejected {
        device_ref: DeviceRef,
        control_id: Option<String>,
        capability_id: String,
        command_type: String,
        reason: String,
        message: Option<String>,
    },
    CommandReply {
        device_ref: DeviceRef,
        control_id: Option<String>,
        capability_id: String,
        command_type: String,
        result: Option<Value>,
    },
}

impl HardwareMessageBody {
    pub fn message_type(&self) -> &'static str {
        match self {
            Self::DeviceAvailable { .. } => DEVICE_AVAILABLE,
            Self::DeviceDescriptorChanged { .. } => DEVICE_DESCRIPTOR_CHANGED,
            Self::DeviceUnavailable { .. } => DEVICE_UNAVAILABLE,
            Self::ControlInput { .. } => CONTROL_INPUT,
            Self::ControlCommand { .. } => CONTROL_COMMAND,
            Self::CapabilityStateChanged { .. } => CAPABILITY_STATE_CHANGED,
            Self::CapabilityStateRequest { .. } => CAPABILITY_STATE_REQUEST,
            Self::CapabilityStateReply { .. } => CAPABILITY_STATE_REPLY,
            Self::CommandAccepted { .. } => COMMAND_ACCEPTED,
            Self::CommandRejected { .. } => COMMAND_REJECTED,
            Self::CommandReply { .. } => COMMAND_REPLY,
        }
    }

    pub fn control_id(&self) -> Option<&str> {
        match self {
            Self::ControlInput { control_id, .. } => Some(control_id),
            Self::ControlCommand { control_id, .. }
            | Self::CapabilityStateChanged { control_id, .. }
            | Self::CapabilityStateRequest { control_id, .. }
            | Self::CapabilityStateReply { control_id, .. }
            | Self::CommandAccepted { control_id, .. }
            | Self::CommandRejected { control_id, .. }
            | Self::CommandReply { control_id, .. } => control_id.as_deref(),
            Self::DeviceAvailable { .. }
            | Self::DeviceDescriptorChanged { .. }
            | Self::DeviceUnavailable { .. } => None,
        }
    }

    pub fn capability_id(&self) -> Option<&str> {
        match self {
            Self::ControlInput { capability_id, .. }
            | Self::ControlCommand { capability_id, .. }
            | Self::CapabilityStateChanged { capability_id, .. }
            | Self::CapabilityStateRequest { capability_id, .. }
            | Self::CapabilityStateReply { capability_id, .. }
            | Self::CommandAccepted { capability_id, .. }
            | Self::CommandRejected { capability_id, .. }
            | Self::CommandReply { capability_id, .. } => Some(capability_id),
            Self::DeviceAvailable { .. }
            | Self::DeviceDescriptorChanged { .. }
            | Self::DeviceUnavailable { .. } => None,
        }
    }

    pub fn device_ref(&self) -> Option<&DeviceRef> {
        match self {
            Self::DeviceUnavailable { device_ref, .. }
            | Self::ControlInput { device_ref, .. }
            | Self::ControlCommand { device_ref, .. }
            | Self::CapabilityStateChanged { device_ref, .. }
            | Self::CapabilityStateRequest { device_ref, .. }
            | Self::CapabilityStateReply { device_ref, .. }
            | Self::CommandAccepted { device_ref, .. }
            | Self::CommandRejected { device_ref, .. }
            | Self::CommandReply { device_ref, .. } => Some(device_ref),
            Self::DeviceAvailable { .. } | Self::DeviceDescriptorChanged { .. } => None,
        }
    }

    pub fn is_input(&self) -> bool {
        matches!(
            self,
            Self::DeviceAvailable { .. }
                | Self::DeviceDescriptorChanged { .. }
                | Self::DeviceUnavailable { .. }
                | Self::ControlInput { .. }
                | Self::CapabilityStateChanged { .. }
                | Self::CapabilityStateReply { .. }
                | Self::CommandAccepted { .. }
                | Self::CommandRejected { .. }
                | Self::CommandReply { .. }
        )
    }

    pub fn is_command(&self) -> bool {
        matches!(
            self,
            Self::ControlCommand { .. } | Self::CapabilityStateRequest { .. }
        )
    }

    pub fn to_value(&self) -> Result<Value, HardwareError> {
        Ok(match self {
            Self::DeviceAvailable { descriptor } => {
                serde_json::to_value(DeviceDescriptorBody { descriptor })?
            }
            Self::DeviceDescriptorChanged { descriptor } => {
                serde_json::to_value(DeviceDescriptorBody { descriptor })?
            }
            Self::DeviceUnavailable { device_ref, reason } => {
                serde_json::to_value(DeviceUnavailableBody { device_ref, reason })?
            }
            Self::ControlInput {
                device_ref,
                control_id,
                capability_id,
                event_type,
                value,
                sequence,
                occurred_at,
                sources,
            } => serde_json::to_value(ControlInputBody {
                device_ref,
                control_id,
                capability_id,
                event_type,
                value,
                sequence,
                occurred_at,
                sources,
            })?,
            Self::ControlCommand {
                device_ref,
                control_id,
                capability_id,
                command_type,
                params,
            } => serde_json::to_value(ControlCommandBody {
                device_ref,
                control_id,
                capability_id,
                command_type,
                params,
            })?,
            Self::CapabilityStateChanged {
                device_ref,
                capability_id,
                value,
                control_id,
                state_type,
                sequence,
                occurred_at,
            } => serde_json::to_value(CapabilityStateChangedBody {
                device_ref,
                capability_id,
                value,
                control_id,
                state_type,
                sequence,
                occurred_at,
            })?,
            Self::CapabilityStateRequest {
                device_ref,
                capability_id,
                control_id,
                state_type,
                params,
            } => serde_json::to_value(CapabilityStateRequestBody {
                device_ref,
                capability_id,
                control_id,
                state_type,
                params,
            })?,
            Self::CapabilityStateReply {
                device_ref,
                capability_id,
                status,
                value,
                control_id,
                state_type,
                error,
            } => serde_json::to_value(CapabilityStateReplyBody {
                device_ref,
                capability_id,
                status,
                value,
                control_id,
                state_type,
                error,
            })?,
            Self::CommandAccepted {
                device_ref,
                control_id,
                capability_id,
                command_type,
                accepted_at,
            } => serde_json::to_value(CommandAcceptedBody {
                device_ref,
                control_id,
                capability_id,
                command_type,
                accepted_at,
            })?,
            Self::CommandRejected {
                device_ref,
                control_id,
                capability_id,
                command_type,
                reason,
                message,
            } => serde_json::to_value(CommandRejectedBody {
                device_ref,
                control_id,
                capability_id,
                command_type,
                reason,
                message,
            })?,
            Self::CommandReply {
                device_ref,
                control_id,
                capability_id,
                command_type,
                result,
            } => serde_json::to_value(CommandReplyBody {
                device_ref,
                control_id,
                capability_id,
                command_type,
                result,
            })?,
        })
    }

    pub fn from_message(message_type: &str, body: &Value) -> Result<Self, HardwareError> {
        Ok(match message_type {
            DEVICE_AVAILABLE => {
                let body: OwnedDeviceDescriptorBody = serde_json::from_value(body.clone())?;
                Self::DeviceAvailable {
                    descriptor: body.descriptor,
                }
            }
            DEVICE_DESCRIPTOR_CHANGED => {
                let body: OwnedDeviceDescriptorBody = serde_json::from_value(body.clone())?;
                Self::DeviceDescriptorChanged {
                    descriptor: body.descriptor,
                }
            }
            DEVICE_UNAVAILABLE => {
                serde_json::from_value::<OwnedDeviceUnavailableBody>(body.clone())?.into()
            }
            CONTROL_INPUT => serde_json::from_value::<OwnedControlInputBody>(body.clone())?.into(),
            CONTROL_COMMAND => {
                serde_json::from_value::<OwnedControlCommandBody>(body.clone())?.into()
            }
            CAPABILITY_STATE_CHANGED => {
                serde_json::from_value::<OwnedCapabilityStateChangedBody>(body.clone())?.into()
            }
            CAPABILITY_STATE_REQUEST => {
                serde_json::from_value::<OwnedCapabilityStateRequestBody>(body.clone())?.into()
            }
            CAPABILITY_STATE_REPLY => {
                serde_json::from_value::<OwnedCapabilityStateReplyBody>(body.clone())?.into()
            }
            COMMAND_ACCEPTED => {
                serde_json::from_value::<OwnedCommandAcceptedBody>(body.clone())?.into()
            }
            COMMAND_REJECTED => {
                serde_json::from_value::<OwnedCommandRejectedBody>(body.clone())?.into()
            }
            COMMAND_REPLY => serde_json::from_value::<OwnedCommandReplyBody>(body.clone())?.into(),
            other => return Err(HardwareError::UnsupportedMessageType(other.to_string())),
        })
    }
}

impl DeckrMessage {
    pub fn hardware_input(
        manager_id: &str,
        sender_session_id: &str,
        device_id: &str,
        body: HardwareMessageBody,
    ) -> Result<Self, HardwareError> {
        Self::hardware_lane_message(
            hardware_manager_address(manager_id)?,
            sender_session_id.to_string(),
            MessageTarget::Broadcast(BroadcastTarget {
                scope: "controllers".to_string(),
                endpoint_family: "controller".to_string(),
                domain: None,
                hop_limit: None,
            }),
            None,
            manager_id,
            device_id,
            body,
        )
    }

    pub fn hardware_input_to(
        manager_id: &str,
        sender_session_id: &str,
        device_id: &str,
        controller_endpoint: EndpointAddress,
        controller_session_id: &str,
        body: HardwareMessageBody,
    ) -> Result<Self, HardwareError> {
        Self::hardware_lane_message(
            hardware_manager_address(manager_id)?,
            sender_session_id.to_string(),
            MessageTarget::Endpoint(EndpointTarget {
                endpoint: controller_endpoint,
            }),
            Some(controller_session_id.to_string()),
            manager_id,
            device_id,
            body,
        )
    }

    pub fn hardware_command(
        controller_id: &str,
        controller_session_id: &str,
        manager_id: &str,
        manager_session_id: &str,
        device_id: &str,
        body: HardwareMessageBody,
    ) -> Result<Self, HardwareError> {
        Self::hardware_lane_message(
            EndpointAddress::new("controller", controller_id)?,
            controller_session_id.to_string(),
            MessageTarget::Endpoint(EndpointTarget {
                endpoint: hardware_manager_address(manager_id)?,
            }),
            Some(manager_session_id.to_string()),
            manager_id,
            device_id,
            body,
        )
    }

    pub fn hardware_lane_message(
        sender: EndpointAddress,
        sender_session_id: String,
        recipient: MessageTarget,
        recipient_session_id: Option<String>,
        manager_id: &str,
        device_id: &str,
        body: HardwareMessageBody,
    ) -> Result<Self, HardwareError> {
        let subject = match body.capability_id() {
            Some(capability_id) => hardware_subject_for_capability(&CapabilityRef {
                device_ref: Some(DeviceRef {
                    manager_id: manager_id.to_string(),
                    device_id: device_id.to_string(),
                    fingerprint: None,
                }),
                control_id: body.control_id().map(ToString::to_string),
                capability_id: capability_id.to_string(),
            }),
            None => hardware_subject_for_device(&DeviceRef {
                manager_id: manager_id.to_string(),
                device_id: device_id.to_string(),
                fingerprint: None,
            }),
        };
        Ok(Self {
            message_id: Uuid::new_v4().to_string(),
            protocol_version: DECKR_PROTOCOL_VERSION.to_string(),
            schema_version: "1".to_string(),
            lane: HARDWARE_MESSAGES_LANE.to_string(),
            message_type: body.message_type().to_string(),
            sender,
            sender_session_id,
            recipient,
            recipient_session_id,
            subject,
            created_at: Utc::now(),
            expires_at: None,
            ttl_ms: None,
            in_reply_to: None,
            causation_id: None,
            trace: None,
            body: body.to_value()?,
        })
    }

    pub fn hardware_body(&self) -> Result<HardwareMessageBody, HardwareError> {
        HardwareMessageBody::from_message(&self.message_type, &self.body)
    }
}

pub fn hardware_body_from_message(
    message: &DeckrMessage,
) -> Result<HardwareMessageBody, HardwareError> {
    message.hardware_body()
}

#[derive(Serialize)]
struct DeviceDescriptorBody<'a> {
    descriptor: &'a DeviceDescriptor,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct OwnedDeviceDescriptorBody {
    descriptor: DeviceDescriptor,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct DeviceUnavailableBody<'a> {
    device_ref: &'a DeviceRef,
    #[serde(skip_serializing_if = "Option::is_none")]
    reason: &'a Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct OwnedDeviceUnavailableBody {
    device_ref: DeviceRef,
    #[serde(default)]
    reason: Option<String>,
}

impl From<OwnedDeviceUnavailableBody> for HardwareMessageBody {
    fn from(body: OwnedDeviceUnavailableBody) -> Self {
        Self::DeviceUnavailable {
            device_ref: body.device_ref,
            reason: body.reason,
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ControlInputBody<'a> {
    device_ref: &'a DeviceRef,
    control_id: &'a String,
    capability_id: &'a String,
    event_type: &'a String,
    #[serde(skip_serializing_if = "Option::is_none")]
    value: &'a Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    sequence: &'a Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    occurred_at: &'a Option<DateTime<Utc>>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    sources: &'a Vec<DeviceSourceReference>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct OwnedControlInputBody {
    device_ref: DeviceRef,
    control_id: String,
    capability_id: String,
    event_type: String,
    #[serde(default)]
    value: Option<Value>,
    #[serde(default)]
    sequence: Option<u64>,
    #[serde(default)]
    occurred_at: Option<DateTime<Utc>>,
    #[serde(default)]
    sources: Vec<DeviceSourceReference>,
}

impl From<OwnedControlInputBody> for HardwareMessageBody {
    fn from(body: OwnedControlInputBody) -> Self {
        Self::ControlInput {
            device_ref: body.device_ref,
            control_id: body.control_id,
            capability_id: body.capability_id,
            event_type: body.event_type,
            value: body.value,
            sequence: body.sequence,
            occurred_at: body.occurred_at,
            sources: body.sources,
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ControlCommandBody<'a> {
    device_ref: &'a DeviceRef,
    #[serde(skip_serializing_if = "Option::is_none")]
    control_id: &'a Option<String>,
    capability_id: &'a String,
    command_type: &'a String,
    params: &'a serde_json::Map<String, Value>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct OwnedControlCommandBody {
    device_ref: DeviceRef,
    #[serde(default)]
    control_id: Option<String>,
    capability_id: String,
    command_type: String,
    #[serde(default)]
    params: serde_json::Map<String, Value>,
}

impl From<OwnedControlCommandBody> for HardwareMessageBody {
    fn from(body: OwnedControlCommandBody) -> Self {
        Self::ControlCommand {
            device_ref: body.device_ref,
            control_id: body.control_id,
            capability_id: body.capability_id,
            command_type: body.command_type,
            params: body.params,
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CapabilityStateChangedBody<'a> {
    device_ref: &'a DeviceRef,
    capability_id: &'a String,
    #[serde(skip_serializing_if = "Option::is_none")]
    value: &'a Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    control_id: &'a Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    state_type: &'a Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    sequence: &'a Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    occurred_at: &'a Option<DateTime<Utc>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct OwnedCapabilityStateChangedBody {
    device_ref: DeviceRef,
    capability_id: String,
    #[serde(default)]
    value: Option<Value>,
    #[serde(default)]
    control_id: Option<String>,
    #[serde(default)]
    state_type: Option<String>,
    #[serde(default)]
    sequence: Option<u64>,
    #[serde(default)]
    occurred_at: Option<DateTime<Utc>>,
}

impl From<OwnedCapabilityStateChangedBody> for HardwareMessageBody {
    fn from(body: OwnedCapabilityStateChangedBody) -> Self {
        Self::CapabilityStateChanged {
            device_ref: body.device_ref,
            capability_id: body.capability_id,
            value: body.value,
            control_id: body.control_id,
            state_type: body.state_type,
            sequence: body.sequence,
            occurred_at: body.occurred_at,
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CapabilityStateRequestBody<'a> {
    device_ref: &'a DeviceRef,
    capability_id: &'a String,
    #[serde(skip_serializing_if = "Option::is_none")]
    control_id: &'a Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    state_type: &'a Option<String>,
    params: &'a serde_json::Map<String, Value>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct OwnedCapabilityStateRequestBody {
    device_ref: DeviceRef,
    capability_id: String,
    #[serde(default)]
    control_id: Option<String>,
    #[serde(default)]
    state_type: Option<String>,
    #[serde(default)]
    params: serde_json::Map<String, Value>,
}

impl From<OwnedCapabilityStateRequestBody> for HardwareMessageBody {
    fn from(body: OwnedCapabilityStateRequestBody) -> Self {
        Self::CapabilityStateRequest {
            device_ref: body.device_ref,
            capability_id: body.capability_id,
            control_id: body.control_id,
            state_type: body.state_type,
            params: body.params,
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CapabilityStateReplyBody<'a> {
    device_ref: &'a DeviceRef,
    capability_id: &'a String,
    status: &'a String,
    #[serde(skip_serializing_if = "Option::is_none")]
    value: &'a Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    control_id: &'a Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    state_type: &'a Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: &'a Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct OwnedCapabilityStateReplyBody {
    device_ref: DeviceRef,
    capability_id: String,
    #[serde(default = "default_state_status")]
    status: String,
    #[serde(default)]
    value: Option<Value>,
    #[serde(default)]
    control_id: Option<String>,
    #[serde(default)]
    state_type: Option<String>,
    #[serde(default)]
    error: Option<String>,
}

fn default_state_status() -> String {
    "ok".to_string()
}

impl From<OwnedCapabilityStateReplyBody> for HardwareMessageBody {
    fn from(body: OwnedCapabilityStateReplyBody) -> Self {
        Self::CapabilityStateReply {
            device_ref: body.device_ref,
            capability_id: body.capability_id,
            status: body.status,
            value: body.value,
            control_id: body.control_id,
            state_type: body.state_type,
            error: body.error,
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CommandAcceptedBody<'a> {
    device_ref: &'a DeviceRef,
    #[serde(skip_serializing_if = "Option::is_none")]
    control_id: &'a Option<String>,
    capability_id: &'a String,
    command_type: &'a String,
    #[serde(skip_serializing_if = "Option::is_none")]
    accepted_at: &'a Option<DateTime<Utc>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct OwnedCommandAcceptedBody {
    device_ref: DeviceRef,
    #[serde(default)]
    control_id: Option<String>,
    capability_id: String,
    command_type: String,
    #[serde(default)]
    accepted_at: Option<DateTime<Utc>>,
}

impl From<OwnedCommandAcceptedBody> for HardwareMessageBody {
    fn from(body: OwnedCommandAcceptedBody) -> Self {
        Self::CommandAccepted {
            device_ref: body.device_ref,
            control_id: body.control_id,
            capability_id: body.capability_id,
            command_type: body.command_type,
            accepted_at: body.accepted_at,
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CommandRejectedBody<'a> {
    device_ref: &'a DeviceRef,
    #[serde(skip_serializing_if = "Option::is_none")]
    control_id: &'a Option<String>,
    capability_id: &'a String,
    command_type: &'a String,
    reason: &'a String,
    #[serde(skip_serializing_if = "Option::is_none")]
    message: &'a Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct OwnedCommandRejectedBody {
    device_ref: DeviceRef,
    #[serde(default)]
    control_id: Option<String>,
    capability_id: String,
    command_type: String,
    reason: String,
    #[serde(default)]
    message: Option<String>,
}

impl From<OwnedCommandRejectedBody> for HardwareMessageBody {
    fn from(body: OwnedCommandRejectedBody) -> Self {
        Self::CommandRejected {
            device_ref: body.device_ref,
            control_id: body.control_id,
            capability_id: body.capability_id,
            command_type: body.command_type,
            reason: body.reason,
            message: body.message,
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CommandReplyBody<'a> {
    device_ref: &'a DeviceRef,
    #[serde(skip_serializing_if = "Option::is_none")]
    control_id: &'a Option<String>,
    capability_id: &'a String,
    command_type: &'a String,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: &'a Option<Value>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct OwnedCommandReplyBody {
    device_ref: DeviceRef,
    #[serde(default)]
    control_id: Option<String>,
    capability_id: String,
    command_type: String,
    #[serde(default)]
    result: Option<Value>,
}

impl From<OwnedCommandReplyBody> for HardwareMessageBody {
    fn from(body: OwnedCommandReplyBody) -> Self {
        Self::CommandReply {
            device_ref: body.device_ref,
            control_id: body.control_id,
            capability_id: body.capability_id,
            command_type: body.command_type,
            result: body.result,
        }
    }
}
