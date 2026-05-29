use ring::digest::{digest, SHA256};
use serde::Serialize;
use serde_json::{Map, Value};

use crate::Result;

pub fn canonical_json_bytes<T: Serialize>(value: &T) -> Result<Vec<u8>> {
    let value = serde_json::to_value(value)?;
    canonical_json_bytes_value(&value)
}

pub fn canonical_json_bytes_value(value: &Value) -> Result<Vec<u8>> {
    Ok(serde_json::to_vec(&sort_json_value(value.clone()))?)
}

pub fn canonical_json_hash<T: Serialize>(value: &T) -> Result<String> {
    Ok(terms_hash_bytes(&canonical_json_bytes(value)?))
}

pub fn canonical_json_hash_value(value: &Value) -> Result<String> {
    Ok(terms_hash_bytes(&canonical_json_bytes_value(value)?))
}

fn terms_hash_bytes(bytes: &[u8]) -> String {
    let hash = digest(&SHA256, bytes);
    let mut output = String::from("sha256:");
    for byte in hash.as_ref() {
        output.push_str(&format!("{byte:02x}"));
    }
    output
}

fn sort_json_value(value: Value) -> Value {
    match value {
        Value::Array(items) => Value::Array(items.into_iter().map(sort_json_value).collect()),
        Value::Object(map) => {
            let mut items = map.into_iter().collect::<Vec<_>>();
            items.sort_by(|left, right| left.0.cmp(&right.0));
            let mut sorted = Map::new();
            for (key, value) in items {
                sorted.insert(key, sort_json_value(value));
            }
            Value::Object(sorted)
        }
        other => other,
    }
}
