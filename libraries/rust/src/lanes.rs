use chrono::{DateTime, Utc};

use crate::identity::{
    message_is_expired_at, message_targets_endpoint, validate_lane_message, DeckrMessage,
    EndpointAddress, ACTIONS_LANE, HARDWARE_MESSAGES_LANE, SERVICES_LANE,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LaneName {
    Actions,
    HardwareMessages,
    Services,
}

impl LaneName {
    pub fn as_str(self) -> &'static str {
        match self {
            LaneName::Actions => ACTIONS_LANE,
            LaneName::HardwareMessages => HARDWARE_MESSAGES_LANE,
            LaneName::Services => SERVICES_LANE,
        }
    }
}

pub const CORE_LANES: &[LaneName] = &[
    LaneName::Actions,
    LaneName::HardwareMessages,
    LaneName::Services,
];

pub fn message_is_deliverable_at(
    message: &DeckrMessage,
    endpoint: &EndpointAddress,
    endpoint_session_id: &str,
    now: DateTime<Utc>,
) -> bool {
    if message_is_expired_at(message, now) || validate_lane_message(message).is_err() {
        return false;
    }
    if message
        .recipient_session_id
        .as_ref()
        .is_some_and(|session| session != endpoint_session_id)
    {
        return false;
    }
    message_targets_endpoint(message, endpoint)
}
