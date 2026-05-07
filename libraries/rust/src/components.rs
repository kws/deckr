use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::identity::EndpointAddress;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReadinessState {
    #[serde(rename = "unknown")]
    Unknown,
    #[serde(rename = "ready")]
    Ready,
    #[serde(rename = "unready")]
    Unready,
}

impl ReadinessState {
    pub fn as_str(self) -> &'static str {
        match self {
            ReadinessState::Unknown => "unknown",
            ReadinessState::Ready => "ready",
            ReadinessState::Unready => "unready",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DependencyKind {
    #[serde(rename = "endpoint")]
    Endpoint,
    #[serde(rename = "service")]
    Service,
}

impl DependencyKind {
    pub fn as_str(self) -> &'static str {
        match self {
            DependencyKind::Endpoint => "endpoint",
            DependencyKind::Service => "service",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DependencyMode {
    #[serde(rename = "required")]
    Required,
    #[serde(rename = "optional")]
    Optional,
    #[serde(rename = "preferred")]
    Preferred,
    #[serde(rename = "observed")]
    Observed,
}

impl DependencyMode {
    pub fn as_str(self) -> &'static str {
        match self {
            DependencyMode::Required => "required",
            DependencyMode::Optional => "optional",
            DependencyMode::Preferred => "preferred",
            DependencyMode::Observed => "observed",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DependencyConditionState {
    #[serde(rename = "unknown")]
    Unknown,
    #[serde(rename = "satisfied")]
    Satisfied,
    #[serde(rename = "degraded")]
    Degraded,
    #[serde(rename = "unsatisfied")]
    Unsatisfied,
}

impl DependencyConditionState {
    pub fn as_str(self) -> &'static str {
        match self {
            DependencyConditionState::Unknown => "unknown",
            DependencyConditionState::Satisfied => "satisfied",
            DependencyConditionState::Degraded => "degraded",
            DependencyConditionState::Unsatisfied => "unsatisfied",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ComponentDependency {
    pub name: String,
    pub kind: DependencyKind,
    pub mode: DependencyMode,
    pub endpoint: EndpointAddress,
    pub lane: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub namespace: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DependencyCondition {
    pub name: String,
    pub kind: DependencyKind,
    pub mode: DependencyMode,
    pub state: DependencyConditionState,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default, skip_serializing_if = "serde_json::Map::is_empty")]
    pub diagnostics: serde_json::Map<String, Value>,
}

pub fn dependency_effective_readiness(
    conditions: &BTreeMap<String, DependencyCondition>,
) -> (ReadinessState, Vec<String>, Value) {
    let mut diagnostics = serde_json::Map::new();
    for (name, condition) in conditions {
        if condition.state != DependencyConditionState::Satisfied
            || condition.mode != DependencyMode::Required
        {
            diagnostics.insert(name.clone(), condition_diagnostic(condition));
        }
    }

    let mut blocking: Vec<_> = conditions
        .values()
        .filter(|condition| {
            condition.mode == DependencyMode::Required
                && matches!(
                    condition.state,
                    DependencyConditionState::Unknown
                        | DependencyConditionState::Degraded
                        | DependencyConditionState::Unsatisfied
                )
        })
        .collect();
    blocking.sort_by(|left, right| left.name.cmp(&right.name));
    if blocking.is_empty() {
        return (
            ReadinessState::Ready,
            Vec::new(),
            serde_json::json!({ "dependencies": diagnostics }),
        );
    }
    let reasons = blocking
        .into_iter()
        .map(|condition| format!("dependency.{}.{}", condition.name, condition.state.as_str()))
        .collect();
    (
        ReadinessState::Unready,
        reasons,
        serde_json::json!({ "dependencies": diagnostics }),
    )
}

fn condition_diagnostic(condition: &DependencyCondition) -> Value {
    let mut value = serde_json::Map::new();
    value.insert(
        "kind".to_string(),
        Value::String(condition.kind.as_str().to_string()),
    );
    value.insert(
        "mode".to_string(),
        Value::String(condition.mode.as_str().to_string()),
    );
    value.insert(
        "state".to_string(),
        Value::String(condition.state.as_str().to_string()),
    );
    if let Some(reason) = &condition.reason {
        value.insert("reason".to_string(), Value::String(reason.clone()));
    }
    if !condition.diagnostics.is_empty() {
        value.insert(
            "diagnostics".to_string(),
            Value::Object(condition.diagnostics.clone()),
        );
    }
    Value::Object(value)
}
