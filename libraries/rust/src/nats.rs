use std::collections::BTreeMap;

use serde::Serialize;
use thiserror::Error;

use crate::{
    identity::{DeckrMessage, MessageTarget},
    keys::encode_key_token,
};

pub const LANE_SUBJECT_PREFIX: &str = "deckr.lane";
pub const LANE_SUBJECT_TEMPLATE: &str = "deckr.lane.{lane}.{senderFamily}.{senderEndpointToken}";
pub const LANE_SUBSCRIBE_TEMPLATE: &str = "deckr.lane.{lane}.>";
pub const NATS_BINDING_SCHEMA_ID: &str = "dev.deckr.binding.nats.v1";
pub const NATS_BINDING_PATH: &str = "bindings/nats.v1.json";
pub const DECKR_NATS_HEADERS: &[&str] = &[
    "Deckr-Message-Id",
    "Deckr-Message-Type",
    "Deckr-Sender",
    "Deckr-Sender-Session",
    "Deckr-Recipient",
    "Deckr-Recipient-Session",
    "Deckr-In-Reply-To",
];
pub const REQUIRED_DECKR_NATS_HEADERS: &[&str] = &[
    "Deckr-Message-Id",
    "Deckr-Message-Type",
    "Deckr-Sender",
    "Deckr-Sender-Session",
    "Deckr-Recipient",
];

#[derive(Debug, Error)]
pub enum NatsBindingError {
    #[error("NATS subject disagrees with Deckr envelope sender")]
    SubjectMismatch,
    #[error("NATS header {0} disagrees with Deckr envelope")]
    HeaderMismatch(String),
}

pub fn subject_for(message: &DeckrMessage) -> String {
    format!(
        "{LANE_SUBJECT_PREFIX}.{}.{}.{}",
        encode_key_token(&message.lane),
        encode_key_token(&message.sender.family),
        encode_key_token(&message.sender.endpoint_id)
    )
}

pub fn subscribe_subject_for_lane(lane: &str) -> String {
    format!("{LANE_SUBJECT_PREFIX}.{}.>", encode_key_token(lane))
}

pub fn recipient_header(message: &DeckrMessage) -> String {
    match &message.recipient {
        MessageTarget::Endpoint(target) => target.endpoint.to_string(),
        MessageTarget::Broadcast(target) => {
            format!("broadcast:{}:{}", target.scope, target.endpoint_family)
        }
    }
}

pub fn headers_for(message: &DeckrMessage) -> BTreeMap<String, String> {
    let mut headers = BTreeMap::new();
    headers.insert(
        DECKR_NATS_HEADERS[0].to_string(),
        message.message_id.clone(),
    );
    headers.insert(
        DECKR_NATS_HEADERS[1].to_string(),
        message.message_type.clone(),
    );
    headers.insert(
        DECKR_NATS_HEADERS[2].to_string(),
        message.sender.to_string(),
    );
    headers.insert(
        DECKR_NATS_HEADERS[3].to_string(),
        message.sender_session_id.clone(),
    );
    headers.insert(DECKR_NATS_HEADERS[4].to_string(), recipient_header(message));
    if let Some(value) = &message.recipient_session_id {
        headers.insert(DECKR_NATS_HEADERS[5].to_string(), value.clone());
    }
    if let Some(value) = &message.in_reply_to {
        headers.insert(DECKR_NATS_HEADERS[6].to_string(), value.clone());
    }
    headers
}

pub fn payload_json_bytes(message: &DeckrMessage) -> Result<Vec<u8>, serde_json::Error> {
    serde_json::to_vec(message)
}

pub fn state_payload_json_bytes<T: Serialize + ?Sized>(
    value: &T,
) -> Result<Vec<u8>, serde_json::Error> {
    serde_json::to_vec(value)
}

pub fn validate_subject_hint(
    subject: &str,
    message: &DeckrMessage,
) -> Result<(), NatsBindingError> {
    let prefix = format!("{LANE_SUBJECT_PREFIX}.");
    if !subject.starts_with(&prefix) {
        return Ok(());
    }
    if subject_for(message) == subject {
        Ok(())
    } else {
        Err(NatsBindingError::SubjectMismatch)
    }
}

pub fn validate_headers(
    headers: Option<&BTreeMap<String, String>>,
    message: &DeckrMessage,
) -> Result<(), NatsBindingError> {
    let Some(headers) = headers else {
        return Ok(());
    };
    let expected = headers_for(message);
    for (key, expected_value) in expected {
        if headers
            .get(&key)
            .is_some_and(|actual| actual != &expected_value)
        {
            return Err(NatsBindingError::HeaderMismatch(key));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subscribe_subject_encodes_lane_token() {
        assert_eq!(
            subscribe_subject_for_lane("hardware_messages"),
            "deckr.lane.hardware_messages.>"
        );
        assert_eq!(
            subscribe_subject_for_lane("owner/custom lane"),
            "deckr.lane.b64_b3duZXIvY3VzdG9tIGxhbmU.>"
        );
    }
}
