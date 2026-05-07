use serde_json::Value;

pub use crate::keys::{
    action_provider_catalog_key, device_claim_key, hardware_inventory_key,
    parse_action_provider_catalog_key, parse_device_claim_key, parse_hardware_inventory_key,
    parse_presence_endpoint_key, parse_service_catalog_key, parse_service_status_key,
    parse_service_view_key, presence_endpoint_key, service_catalog_key, service_status_key,
    service_view_key,
};

pub type EndpointPresence = Value;
pub type HardwareInventory = Value;
pub type DeviceClaim = Value;
pub type ActionProviderCatalog = Value;
pub type ServiceCatalog = Value;
pub type ServiceStatus = Value;
