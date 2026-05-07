use std::collections::BTreeMap;

use serde_json::Value;

use crate::{
    identity::{DeckrMessage, MessageTarget},
    keys::encode_key_token,
};

pub fn subject_for(message: &DeckrMessage) -> String {
    format!(
        "deckr.lane.{}.{}.{}",
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

pub fn state_payload_json_bytes(value: &Value) -> Result<Vec<u8>, serde_json::Error> {
    serde_json::to_vec(value)
}
