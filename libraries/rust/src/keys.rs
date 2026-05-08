use std::str::FromStr;

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use regex::Regex;
use serde_json::Value;
use thiserror::Error;

use crate::identity::{EndpointAddress, HARDWARE_MESSAGES_LANE};

#[derive(Debug, Error)]
pub enum KeyError {
    #[error("invalid base64 key token: {0}")]
    Base64(#[from] base64::DecodeError),
    #[error("invalid UTF-8 key token: {0}")]
    Utf8(#[from] std::string::FromUtf8Error),
    #[error("invalid endpoint key")]
    InvalidEndpoint,
}

pub fn encode_key_token(raw: &str) -> String {
    let safe = Regex::new(r"^[A-Za-z0-9][A-Za-z0-9_-]*$").expect("valid regex");
    if safe.is_match(raw) && !raw.starts_with("b64_") {
        raw.to_string()
    } else {
        format!("b64_{}", URL_SAFE_NO_PAD.encode(raw.as_bytes()))
    }
}

pub fn decode_key_token(token: &str) -> Result<String, KeyError> {
    if let Some(encoded) = token.strip_prefix("b64_") {
        Ok(String::from_utf8(URL_SAFE_NO_PAD.decode(encoded)?)?)
    } else {
        Ok(token.to_string())
    }
}

pub fn presence_endpoint_key(lane: &str, endpoint: &EndpointAddress) -> String {
    format!(
        "presence.endpoint.{}.{}.{}",
        encode_key_token(lane),
        encode_key_token(&endpoint.family),
        encode_key_token(&endpoint.endpoint_id)
    )
}

pub fn presence_endpoint_prefix(lane: &str, endpoint_family: &str) -> String {
    format!(
        "presence.endpoint.{}.{}.",
        encode_key_token(lane),
        encode_key_token(endpoint_family)
    )
}

pub fn controller_presence_prefix() -> String {
    presence_endpoint_prefix(HARDWARE_MESSAGES_LANE, "controller")
}

pub fn parse_presence_endpoint_key(
    key: &str,
) -> Result<Option<(String, EndpointAddress)>, KeyError> {
    let parts: Vec<_> = key.split('.').collect();
    if parts.len() != 5 || parts[0] != "presence" || parts[1] != "endpoint" {
        return Ok(None);
    }
    let lane = decode_key_token(parts[2])?;
    let family = decode_key_token(parts[3])?;
    let endpoint_id = decode_key_token(parts[4])?;
    let endpoint = EndpointAddress::from_str(&format!("{family}:{endpoint_id}"))
        .map_err(|_| KeyError::InvalidEndpoint)?;
    Ok(Some((lane, endpoint)))
}

pub fn hardware_inventory_key(manager_id: &str) -> String {
    format!("inventory.hardware.{}", encode_key_token(manager_id))
}

pub fn parse_hardware_inventory_key(key: &str) -> Result<Option<String>, KeyError> {
    let parts: Vec<_> = key.split('.').collect();
    if parts.len() != 3 || parts[0] != "inventory" || parts[1] != "hardware" {
        return Ok(None);
    }
    Ok(Some(decode_key_token(parts[2])?))
}

pub fn device_claim_key(manager_id: &str, device_id: &str) -> String {
    format!(
        "claim.device.{}.{}",
        encode_key_token(manager_id),
        encode_key_token(device_id)
    )
}

pub fn device_claim_prefix(manager_id: &str) -> String {
    format!("claim.device.{}.", encode_key_token(manager_id))
}

pub fn parse_device_claim_key(key: &str) -> Result<Option<(String, String)>, KeyError> {
    let parts: Vec<_> = key.split('.').collect();
    if parts.len() != 4 || parts[0] != "claim" || parts[1] != "device" {
        return Ok(None);
    }
    Ok(Some((
        decode_key_token(parts[2])?,
        decode_key_token(parts[3])?,
    )))
}

pub fn action_provider_catalog_key(provider_instance_id: &str) -> String {
    format!(
        "catalog.actions.providers.{}",
        encode_key_token(provider_instance_id)
    )
}

pub fn parse_action_provider_catalog_key(key: &str) -> Result<Option<String>, KeyError> {
    let parts: Vec<_> = key.split('.').collect();
    if parts.len() != 4 || parts[0] != "catalog" || parts[1] != "actions" || parts[2] != "providers"
    {
        return Ok(None);
    }
    Ok(Some(decode_key_token(parts[3])?))
}

pub fn service_catalog_key(service_id: &str) -> String {
    format!("catalog.services.{}", encode_key_token(service_id))
}

pub fn parse_service_catalog_key(key: &str) -> Result<Option<String>, KeyError> {
    let parts: Vec<_> = key.split('.').collect();
    if parts.len() != 3 || parts[0] != "catalog" || parts[1] != "services" {
        return Ok(None);
    }
    Ok(Some(decode_key_token(parts[2])?))
}

pub fn service_status_key(service_id: &str) -> String {
    format!("status.services.{}", encode_key_token(service_id))
}

pub fn parse_service_status_key(key: &str) -> Result<Option<String>, KeyError> {
    let parts: Vec<_> = key.split('.').collect();
    if parts.len() != 3 || parts[0] != "status" || parts[1] != "services" {
        return Ok(None);
    }
    Ok(Some(decode_key_token(parts[2])?))
}

pub fn service_view_key(service_id: &str, service_namespace: &str, tokens: &[String]) -> String {
    let mut parts = vec![
        "view".to_string(),
        "services".to_string(),
        encode_key_token(service_id),
        encode_key_token(service_namespace),
    ];
    parts.extend(tokens.iter().map(|token| encode_key_token(token)));
    parts.join(".")
}

pub fn parse_service_view_key(
    key: &str,
) -> Result<Option<(String, String, Vec<String>)>, KeyError> {
    let parts: Vec<_> = key.split('.').collect();
    if parts.len() < 4 || parts[0] != "view" || parts[1] != "services" {
        return Ok(None);
    }
    Ok(Some((
        decode_key_token(parts[2])?,
        decode_key_token(parts[3])?,
        parts[4..]
            .iter()
            .map(|part| decode_key_token(part))
            .collect::<Result<Vec<_>, _>>()?,
    )))
}

pub fn settings_target_key(target: &Value) -> String {
    let scope = target["scope"].as_str().unwrap_or_default();
    let controller_id = target["controllerId"].as_str().unwrap_or_default();
    let config_id = target["configId"].as_str().unwrap_or_default();
    let provider_instance_id = target["providerInstanceId"].as_str().unwrap_or_default();
    let provider_id = target["providerId"].as_str().unwrap_or_default();
    let mut parts = vec![
        "settings".to_string(),
        "target".to_string(),
        encode_key_token(scope),
        encode_key_token(controller_id),
        encode_key_token(config_id),
        encode_key_token(provider_instance_id),
        encode_key_token(provider_id),
    ];
    if scope == "action_instance" {
        parts.push(encode_key_token(
            target["actionId"].as_str().unwrap_or_default(),
        ));
        parts.push(encode_key_token(
            target["actionInstanceId"].as_str().unwrap_or_default(),
        ));
        if let Some(stable_id) = target.get("stableId").and_then(Value::as_str) {
            parts.push("1".to_string());
            parts.push(encode_key_token(stable_id));
        } else {
            parts.push("0".to_string());
        }
    }
    parts.join(".")
}

pub fn parse_settings_target_key(key: &str) -> Result<Option<Value>, KeyError> {
    let parts: Vec<_> = key.split('.').collect();
    if parts.len() < 7 || parts[0] != "settings" || parts[1] != "target" {
        return Ok(None);
    }
    let scope = decode_key_token(parts[2])?;
    let controller_id = decode_key_token(parts[3])?;
    let config_id = decode_key_token(parts[4])?;
    let provider_instance_id = decode_key_token(parts[5])?;
    let provider_id = decode_key_token(parts[6])?;
    let mut target = serde_json::Map::new();
    target.insert("scope".to_string(), Value::String(scope.clone()));
    target.insert("controllerId".to_string(), Value::String(controller_id));
    target.insert("configId".to_string(), Value::String(config_id));
    target.insert(
        "providerInstanceId".to_string(),
        Value::String(provider_instance_id),
    );
    target.insert("providerId".to_string(), Value::String(provider_id));
    if scope == "action_provider_instance" && parts.len() == 7 {
        return Ok(Some(Value::Object(target)));
    }
    if scope != "action_instance" || !matches!(parts.len(), 10 | 11) {
        return Ok(None);
    }
    target.insert(
        "actionId".to_string(),
        Value::String(decode_key_token(parts[7])?),
    );
    target.insert(
        "actionInstanceId".to_string(),
        Value::String(decode_key_token(parts[8])?),
    );
    match parts[9] {
        "0" if parts.len() == 10 => {}
        "1" if parts.len() == 11 => {
            target.insert(
                "stableId".to_string(),
                Value::String(decode_key_token(parts[10])?),
            );
        }
        _ => return Ok(None),
    }
    Ok(Some(Value::Object(target)))
}
