use crate::identity::{context_subject, EndpointAddress, EntitySubject};

pub const ACTION_INSTANCE_CREATED: &str = "actionInstanceCreated";
pub const ACTION_INSTANCE_DESTROYED: &str = "actionInstanceDestroyed";
pub const BINDING_ATTACHED: &str = "bindingAttached";
pub const BINDING_DETACHED: &str = "bindingDetached";
pub const PAGE_SESSION_OPENED: &str = "pageSessionOpened";
pub const PAGE_SESSION_CLOSED: &str = "pageSessionClosed";
pub const CAPABILITY_INPUT: &str = "capabilityInput";
pub const BINDING_OUTPUT: &str = "bindingOutput";
pub const BINDING_OVERLAY: &str = "bindingOverlay";
pub const BINDING_OVERLAY_CLEAR: &str = "bindingOverlayClear";
pub const SETTINGS_REQUEST: &str = "settingsRequest";
pub const SETTINGS_PATCH: &str = "settingsPatch";
pub const SETTINGS_REPLACE: &str = "settingsReplace";
pub const SETTINGS_SNAPSHOT: &str = "settingsSnapshot";
pub const OPEN_PAGE: &str = "openPage";
pub const REPLACE_PAGE: &str = "replacePage";
pub const CLOSE_PAGE: &str = "closePage";
pub const ACTION_EXTENSION: &str = "actionExtension";

pub fn action_provider_address(
    provider_instance_id: &str,
) -> Result<EndpointAddress, crate::identity::IdentityError> {
    EndpointAddress::new("action_provider", provider_instance_id)
}

pub fn action_context_subject(
    context_id: &str,
    provider_instance_id: Option<&str>,
    provider_id: Option<&str>,
    config_id: Option<&str>,
    action_instance_id: Option<&str>,
    binding_id: Option<&str>,
) -> EntitySubject {
    context_subject(
        context_id,
        provider_instance_id,
        provider_id,
        config_id,
        action_instance_id,
        binding_id,
    )
}

pub use crate::keys::parse_settings_target_key as parse_settings_target;
pub use crate::keys::settings_target_key as settings_target_key_for;
