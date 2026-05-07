use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::identity::{
    hardware_subject_for_capability as subject_for_capability_value, EntitySubject,
};

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

pub type DeviceDescriptor = Value;
pub type ControlDescriptor = Value;
pub type CapabilityDescriptor = Value;
pub type HardwareMessageBody = Value;

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
