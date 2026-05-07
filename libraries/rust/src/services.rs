use crate::identity::{EndpointAddress, IdentityError};

pub const SERVICE_COMMAND: &str = "serviceCommand";
pub const SERVICE_COMMAND_REPLY: &str = "serviceCommandReply";

pub fn service_address(service_id: &str) -> Result<EndpointAddress, IdentityError> {
    EndpointAddress::new("service", service_id)
}

pub use crate::keys::parse_service_catalog_key as parse_catalog_key;
pub use crate::keys::parse_service_status_key as parse_status_key;
pub use crate::keys::parse_service_view_key as parse_view_key;
pub use crate::keys::service_catalog_key as catalog_key;
pub use crate::keys::service_status_key as status_key;
pub use crate::keys::service_view_key as view_key;
