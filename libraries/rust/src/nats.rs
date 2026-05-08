use std::collections::BTreeMap;

use serde::Serialize;
use thiserror::Error;

use crate::{
    identity::{DeckrMessage, MessageTarget},
    keys::encode_key_token,
};

const LANE_PREFIX: &str = "deckr.lane";

#[derive(Debug, Error)]
pub enum NatsBindingError {
    #[error("NATS subject disagrees with Deckr envelope sender")]
    SubjectMismatch,
    #[error("NATS header {0} disagrees with Deckr envelope")]
    HeaderMismatch(String),
}

pub fn subject_for(message: &DeckrMessage) -> String {
    format!(
        "{LANE_PREFIX}.{}.{}.{}",
        encode_key_token(&message.lane),
        encode_key_token(&message.sender.family),
        encode_key_token(&message.sender.endpoint_id)
    )
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
    headers.insert("Deckr-Message-Id".to_string(), message.message_id.clone());
    headers.insert(
        "Deckr-Message-Type".to_string(),
        message.message_type.clone(),
    );
    headers.insert("Deckr-Sender".to_string(), message.sender.to_string());
    headers.insert(
        "Deckr-Sender-Session".to_string(),
        message.sender_session_id.clone(),
    );
    headers.insert("Deckr-Recipient".to_string(), recipient_header(message));
    if let Some(value) = &message.recipient_session_id {
        headers.insert("Deckr-Recipient-Session".to_string(), value.clone());
    }
    if let Some(value) = &message.in_reply_to {
        headers.insert("Deckr-In-Reply-To".to_string(), value.clone());
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
    let prefix = format!("{LANE_PREFIX}.");
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
