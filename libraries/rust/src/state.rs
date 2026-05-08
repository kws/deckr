use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{
    hardware::{DeviceDescriptor, DeviceRef},
    identity::{hardware_manager_address, EndpointAddress},
};

pub use crate::keys::{
    action_provider_catalog_key, controller_presence_prefix, device_claim_key, device_claim_prefix,
    hardware_inventory_key, parse_action_provider_catalog_key, parse_device_claim_key,
    parse_hardware_inventory_key, parse_presence_endpoint_key, parse_service_catalog_key,
    parse_service_status_key, parse_service_view_key, presence_endpoint_key,
    presence_endpoint_prefix, service_catalog_key, service_status_key, service_view_key,
};

pub const DEFAULT_LEASE_STATE_BUCKET: &str = "deckr_lease_v1";
pub const DEFAULT_DISCOVERY_STATE_BUCKET: &str = "deckr_discovery_v1";
pub const STATE_TTL_SECONDS: u64 = 30;
pub const STATE_RENEWAL_INTERVAL_SECONDS: u64 = 5;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EndpointPresence {
    pub endpoint: EndpointAddress,
    pub lane: String,
    pub session_id: String,
    pub timestamp: DateTime<Utc>,
    pub ttl_seconds: u64,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub metadata: BTreeMap<String, String>,
}

impl EndpointPresence {
    pub fn new(
        endpoint: EndpointAddress,
        lane: impl Into<String>,
        session_id: impl Into<String>,
        ttl_seconds: u64,
        metadata: BTreeMap<String, String>,
    ) -> Self {
        Self {
            endpoint,
            lane: lane.into(),
            session_id: session_id.into(),
            timestamp: Utc::now(),
            ttl_seconds,
            metadata,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct HardwareInventoryDevice {
    pub device_ref: DeviceRef,
    pub descriptor: DeviceDescriptor,
}

impl HardwareInventoryDevice {
    pub fn from_device(manager_id: &str, device: &DeviceDescriptor) -> Self {
        Self {
            device_ref: DeviceRef {
                manager_id: manager_id.to_string(),
                device_id: device.device_id.clone(),
                fingerprint: Some(device.fingerprint.clone()),
            },
            descriptor: device.clone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct HardwareInventory {
    pub manager_id: String,
    pub manager_endpoint: EndpointAddress,
    pub session_id: String,
    pub timestamp: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub labels: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub devices: BTreeMap<String, HardwareInventoryDevice>,
}

impl HardwareInventory {
    pub fn new(
        manager_id: &str,
        session_id: &str,
        devices: BTreeMap<String, HardwareInventoryDevice>,
    ) -> Result<Self, crate::identity::IdentityError> {
        Ok(Self {
            manager_id: manager_id.to_string(),
            manager_endpoint: hardware_manager_address(manager_id)?,
            session_id: session_id.to_string(),
            timestamp: Utc::now(),
            labels: BTreeMap::new(),
            devices,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DeviceClaim {
    pub claimed_by_endpoint: EndpointAddress,
    pub claimed_by_session_id: String,
    pub timestamp: DateTime<Utc>,
    pub ttl_seconds: u64,
}

pub type ActionProviderCatalog = Value;
pub type ServiceCatalog = Value;
pub type ServiceStatus = Value;
