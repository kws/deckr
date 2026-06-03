use std::collections::{BTreeMap, BTreeSet};

use crate::concord::{ConcordManagedContract, ContractValidityStatus};
use crate::endpoint::EndpointAddress;
use crate::lanes::{DeviceDescriptor, DeviceRef};
use crate::profiles::hardware::{
    HardwareAdvertisementDevice, HardwareBeaconPayload, HardwareClaimTerms, ProfileCapacity,
};
use crate::Result;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HardwareClaimRoute {
    pub controller_endpoint: EndpointAddress,
    pub controller_session_id: String,
    pub contract_key: String,
    pub claim_id: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HardwareClaimRecipient<'a> {
    pub endpoint: &'a EndpointAddress,
    pub session_id: &'a str,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IgnoredHardwareClaim {
    pub contract_key: String,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HardwareClaimReconcile {
    pub reset_devices: BTreeSet<String>,
    pub ignored_claims: Vec<IgnoredHardwareClaim>,
}

#[derive(Debug, Clone, Default)]
pub struct HardwareClaimRouting {
    claims: BTreeMap<String, HardwareClaimRoute>,
}

impl HardwareClaimRouting {
    pub fn claim_recipient(&self, device_id: &str) -> Option<HardwareClaimRecipient<'_>> {
        let claim = self.claims.get(device_id)?;
        Some(HardwareClaimRecipient {
            endpoint: &claim.controller_endpoint,
            session_id: &claim.controller_session_id,
        })
    }

    pub fn claimed_device_ids(&self) -> BTreeSet<String> {
        self.claims.keys().cloned().collect()
    }

    pub fn remove_device(&mut self, device_id: &str) {
        self.claims.remove(device_id);
    }

    pub fn reconcile_snapshot<I, J>(
        &mut self,
        next_claims: I,
        invalid_claim_devices: J,
    ) -> BTreeSet<String>
    where
        I: IntoIterator<Item = (String, HardwareClaimRoute)>,
        J: IntoIterator<Item = String>,
    {
        let next_claims = next_claims.into_iter().collect::<BTreeMap<_, _>>();
        let invalid_claim_devices = invalid_claim_devices.into_iter().collect::<BTreeSet<_>>();
        let reset_devices =
            self.devices_to_reset_for_snapshot(&next_claims, &invalid_claim_devices);
        self.claims = next_claims;
        reset_devices
    }

    pub fn reconcile_claims(
        &mut self,
        managed_contracts: &[ConcordManagedContract],
        manager_endpoint: &EndpointAddress,
        known_devices: &BTreeSet<String>,
    ) -> HardwareClaimReconcile {
        let mut next_claims = BTreeMap::<String, HardwareClaimRoute>::new();
        let mut invalid_claim_devices = BTreeSet::<String>::new();
        let mut ignored_claims = Vec::<IgnoredHardwareClaim>::new();
        let mut ordered = managed_contracts.iter().collect::<Vec<_>>();
        ordered.sort_by(|left, right| {
            let left_existing = self.route_for_contract(&left.contract.key).is_none();
            let right_existing = self.route_for_contract(&right.contract.key).is_none();
            left_existing
                .cmp(&right_existing)
                .then_with(|| left.contract.key.cmp(&right.contract.key))
        });

        for managed in ordered {
            if !managed.contract.participants.contains(manager_endpoint) {
                continue;
            }
            let Some(terms_value) = managed.record.terms.clone() else {
                ignored_claims.push(IgnoredHardwareClaim {
                    contract_key: managed.contract.key.clone(),
                    reason: "missing_terms".to_string(),
                });
                continue;
            };
            let terms = match HardwareClaimTerms::from_value(terms_value) {
                Ok(terms) => terms,
                Err(error) => {
                    ignored_claims.push(IgnoredHardwareClaim {
                        contract_key: managed.contract.key.clone(),
                        reason: error.to_string(),
                    });
                    continue;
                }
            };
            if &terms.manager_endpoint != manager_endpoint {
                continue;
            }
            if managed.validity.status != ContractValidityStatus::Valid {
                for device in &terms.devices {
                    let device_id = &device.device_ref.device_id;
                    if known_devices.contains(device_id) {
                        invalid_claim_devices.insert(device_id.clone());
                    }
                }
                continue;
            }
            if !managed
                .contract
                .participants
                .contains(&terms.controller_endpoint)
            {
                ignored_claims.push(IgnoredHardwareClaim {
                    contract_key: managed.contract.key.clone(),
                    reason: "controller_not_participant".to_string(),
                });
                continue;
            }
            let controller_key = terms.controller_endpoint.to_string();
            let Some(controller_token) = managed.validity.tokens.get(&controller_key) else {
                ignored_claims.push(IgnoredHardwareClaim {
                    contract_key: managed.contract.key.clone(),
                    reason: "missing_controller_token".to_string(),
                });
                continue;
            };
            for device in &terms.devices {
                let device_id = &device.device_ref.device_id;
                if !known_devices.contains(device_id) || next_claims.contains_key(device_id) {
                    continue;
                }
                next_claims.insert(
                    device_id.clone(),
                    HardwareClaimRoute {
                        controller_endpoint: terms.controller_endpoint.clone(),
                        controller_session_id: controller_token.session_id.clone(),
                        contract_key: managed.contract.key.clone(),
                        claim_id: terms.claim_id.clone(),
                    },
                );
            }
        }

        invalid_claim_devices.retain(|device_id| !next_claims.contains_key(device_id));
        let reset_devices =
            self.devices_to_reset_for_snapshot(&next_claims, &invalid_claim_devices);
        self.claims = next_claims;
        HardwareClaimReconcile {
            reset_devices,
            ignored_claims,
        }
    }

    fn route_for_contract(&self, contract_key: &str) -> Option<&HardwareClaimRoute> {
        self.claims
            .values()
            .find(|claim| claim.contract_key == contract_key)
    }

    fn devices_to_reset_for_snapshot(
        &self,
        next_claims: &BTreeMap<String, HardwareClaimRoute>,
        invalid_claim_devices: &BTreeSet<String>,
    ) -> BTreeSet<String> {
        let mut devices_to_reset = invalid_claim_devices.clone();
        for (device_id, old_claim) in &self.claims {
            let Some(next_claim) = next_claims.get(device_id) else {
                devices_to_reset.insert(device_id.clone());
                continue;
            };
            if claim_route_identity(old_claim) != claim_route_identity(next_claim) {
                devices_to_reset.insert(device_id.clone());
            }
        }
        devices_to_reset
    }
}

pub fn hardware_beacon_payload(
    manager_id: &str,
    manager_endpoint: EndpointAddress,
    session_id: &str,
    labels: BTreeMap<String, String>,
    devices: &BTreeMap<String, DeviceDescriptor>,
    claimed_devices: &BTreeSet<String>,
) -> Result<HardwareBeaconPayload> {
    let payload = HardwareBeaconPayload {
        profile: crate::profiles::hardware::HARDWARE_PROFILE_ID.to_string(),
        manager_id: manager_id.to_string(),
        manager_endpoint,
        session_id: session_id.to_string(),
        labels,
        devices: devices
            .iter()
            .map(|(device_id, descriptor)| {
                let claimed = claimed_devices.contains(device_id);
                (
                    device_id.clone(),
                    HardwareAdvertisementDevice {
                        capacity: ProfileCapacity {
                            total_instances: Some(1),
                            claimed_instances: if claimed { 1 } else { 0 },
                            available_instances: Some(if claimed { 0 } else { 1 }),
                        },
                        device_ref: DeviceRef {
                            manager_id: manager_id.to_string(),
                            device_id: device_id.clone(),
                            fingerprint: Some(descriptor.fingerprint.clone()),
                        },
                        descriptor: descriptor.clone(),
                    },
                )
            })
            .collect(),
    };
    payload.validate()?;
    Ok(payload)
}

fn claim_route_identity(claim: &HardwareClaimRoute) -> (&EndpointAddress, &str, &str, &str) {
    (
        &claim.controller_endpoint,
        &claim.controller_session_id,
        &claim.contract_key,
        &claim.claim_id,
    )
}

pub mod runtime {
    use std::collections::{BTreeMap, BTreeSet};

    use crate::concord::{ConcordManagedContract, ContractHandle, ContractRecord};
    use crate::endpoint::{hardware_manager_address, EndpointAddress};
    use crate::lanes::{DeckrMessage, DeviceDescriptor, HardwareMessageBody};
    use crate::profiles::hardware::{HardwareClaimTerms, HARDWARE_CLAIM_PROFILE_ID};
    use crate::{Error, Result};

    use super::{
        hardware_beacon_payload, HardwareClaimReconcile, HardwareClaimRoute, HardwareClaimRouting,
    };

    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct LiveHardwareClaim {
        pub device_id: String,
        pub route: HardwareClaimRoute,
    }

    #[derive(Debug, Clone, PartialEq)]
    pub enum HardwareCommandDecision {
        Authorized {
            device_id: String,
            body: HardwareMessageBody,
            sender: EndpointAddress,
            sender_session_id: String,
        },
        Rejected {
            reason: String,
            reply: Option<DeckrMessage>,
        },
        Ignored,
    }

    #[allow(async_fn_in_trait)]
    pub trait HardwareCommandHandler {
        async fn handle_hardware_command(&self, message: DeckrMessage) -> Result<bool>;
    }

    #[allow(async_fn_in_trait)]
    pub trait HardwareResetHandler {
        async fn reset_hardware_device(&self, device_id: &str) -> Result<()>;
    }

    #[derive(Debug, Clone)]
    pub struct HardwareManagerRuntime {
        manager_id: String,
        endpoint: EndpointAddress,
        session_id: String,
        labels: BTreeMap<String, String>,
        devices: BTreeMap<String, DeviceDescriptor>,
        routing: HardwareClaimRouting,
    }

    impl HardwareManagerRuntime {
        pub fn new(manager_id: impl Into<String>, session_id: impl Into<String>) -> Result<Self> {
            let manager_id = manager_id.into();
            Self::with_endpoint(
                manager_id.clone(),
                EndpointAddress::parse(hardware_manager_address(&manager_id))?,
                session_id,
            )
        }

        pub fn with_endpoint(
            manager_id: impl Into<String>,
            endpoint: EndpointAddress,
            session_id: impl Into<String>,
        ) -> Result<Self> {
            let manager_id = manager_id.into();
            let session_id = session_id.into();
            if endpoint.family() != crate::endpoint::HARDWARE_MANAGER_FAMILY {
                return Err(Error::Invalid(
                    "hardware manager runtime endpoint must be hardware_manager".to_string(),
                ));
            }
            if endpoint.endpoint_id() != manager_id {
                return Err(Error::Invalid(
                    "hardware manager endpoint id must match manager id".to_string(),
                ));
            }
            if session_id.trim() != session_id || session_id.is_empty() {
                return Err(Error::Invalid(
                    "hardware manager session id must be non-empty with no leading or trailing whitespace"
                        .to_string(),
                ));
            }
            Ok(Self {
                manager_id,
                endpoint,
                session_id,
                labels: BTreeMap::new(),
                devices: BTreeMap::new(),
                routing: HardwareClaimRouting::default(),
            })
        }

        pub fn manager_id(&self) -> &str {
            &self.manager_id
        }

        pub fn endpoint(&self) -> &EndpointAddress {
            &self.endpoint
        }

        pub fn session_id(&self) -> &str {
            &self.session_id
        }

        pub fn devices(&self) -> &BTreeMap<String, DeviceDescriptor> {
            &self.devices
        }

        pub fn routing(&self) -> &HardwareClaimRouting {
            &self.routing
        }

        pub fn labels_mut(&mut self) -> &mut BTreeMap<String, String> {
            &mut self.labels
        }

        pub fn advertisement_payload(
            &self,
        ) -> Result<crate::profiles::hardware::HardwareBeaconPayload> {
            hardware_beacon_payload(
                &self.manager_id,
                self.endpoint.clone(),
                &self.session_id,
                self.labels.clone(),
                &self.devices,
                &self.routing.claimed_device_ids(),
            )
        }

        pub fn set_device(&mut self, descriptor: DeviceDescriptor) -> Result<()> {
            descriptor.validate()?;
            self.devices
                .insert(descriptor.device_id.clone(), descriptor);
            Ok(())
        }

        pub fn remove_device(&mut self, device_id: &str) -> Option<LiveHardwareClaim> {
            self.devices.remove(device_id);
            let route =
                self.routing
                    .claim_recipient(device_id)
                    .map(|recipient| HardwareClaimRoute {
                        controller_endpoint: recipient.endpoint.clone(),
                        controller_session_id: recipient.session_id.to_string(),
                        contract_key: self
                            .routing
                            .claims
                            .get(device_id)
                            .map(|claim| claim.contract_key.clone())
                            .unwrap_or_default(),
                        claim_id: self
                            .routing
                            .claims
                            .get(device_id)
                            .map(|claim| claim.claim_id.clone())
                            .unwrap_or_default(),
                    });
            self.routing.remove_device(device_id);
            route.map(|route| LiveHardwareClaim {
                device_id: device_id.to_string(),
                route,
            })
        }

        pub fn accept_current_hardware_claim(
            &self,
            contract: &ContractHandle,
            record: &ContractRecord,
        ) -> Result<bool> {
            accept_current_hardware_claim(
                contract,
                record,
                &self.endpoint,
                &self.manager_id,
                &self.devices,
            )
        }

        pub fn reconcile_claims(
            &mut self,
            managed_contracts: &[ConcordManagedContract],
        ) -> HardwareClaimReconcile {
            let known_devices = self.devices.keys().cloned().collect::<BTreeSet<_>>();
            self.routing
                .reconcile_claims(managed_contracts, &self.endpoint, &known_devices)
        }

        pub fn reconcile_routing_snapshot<I, J>(
            &mut self,
            next_claims: I,
            invalid_claim_devices: J,
        ) -> BTreeSet<String>
        where
            I: IntoIterator<Item = (String, HardwareClaimRoute)>,
            J: IntoIterator<Item = String>,
        {
            self.routing
                .reconcile_snapshot(next_claims, invalid_claim_devices)
        }

        pub fn route_hardware_message(
            &self,
            body: HardwareMessageBody,
        ) -> Result<Option<DeckrMessage>> {
            if body.is_command() {
                return Err(Error::Invalid(
                    "hardware manager runtime does not route outbound command bodies".to_string(),
                ));
            }
            let device_ref = body.device_ref().clone();
            if device_ref.manager_id != self.manager_id {
                return Ok(None);
            }
            let Some(recipient) = self.routing.claim_recipient(&device_ref.device_id) else {
                return Ok(None);
            };
            DeckrMessage::hardware_input_to(
                &self.manager_id,
                &self.session_id,
                &device_ref.device_id,
                recipient.endpoint.as_str(),
                recipient.session_id,
                body,
            )
            .map(Some)
        }

        pub fn authorize_command(&self, envelope: DeckrMessage) -> Result<HardwareCommandDecision> {
            if envelope.is_expired() {
                return Ok(HardwareCommandDecision::Rejected {
                    reason: "expired".to_string(),
                    reply: self.command_rejection_reply(&envelope, "expired", None)?,
                });
            }
            if !envelope.is_directly_deliverable_to(&self.endpoint, &self.session_id)? {
                return Ok(HardwareCommandDecision::Ignored);
            }
            let body = match envelope.hardware_body() {
                Ok(body) => body,
                Err(error) => {
                    return Ok(HardwareCommandDecision::Rejected {
                        reason: "malformed".to_string(),
                        reply: None.or_else(|| {
                            let _ = error;
                            None
                        }),
                    })
                }
            };
            if !body.is_command() {
                return Ok(HardwareCommandDecision::Ignored);
            }
            let device_id = body.device_ref().device_id.clone();
            if body.device_ref().manager_id != self.manager_id
                || envelope.subject.manager_id() != Some(self.manager_id.as_str())
            {
                return Ok(HardwareCommandDecision::Rejected {
                    reason: "stale".to_string(),
                    reply: self.command_rejection_reply(
                        &envelope,
                        "stale",
                        Some("command targets a different hardware manager"),
                    )?,
                });
            }
            if !self.devices.contains_key(&device_id) {
                return Ok(HardwareCommandDecision::Rejected {
                    reason: "stale".to_string(),
                    reply: self.command_rejection_reply(
                        &envelope,
                        "stale",
                        Some("command targets an unknown hardware device"),
                    )?,
                });
            }
            if self
                .routing
                .claim_recipient(&device_id)
                .is_none_or(|recipient| {
                    recipient.endpoint.as_str() != envelope.sender
                        || recipient.session_id != envelope.sender_session_id
                })
            {
                return Ok(HardwareCommandDecision::Rejected {
                    reason: "unauthorized".to_string(),
                    reply: self.command_rejection_reply(
                        &envelope,
                        "unauthorized",
                        Some("sender does not hold the live hardware claim"),
                    )?,
                });
            }
            Ok(HardwareCommandDecision::Authorized {
                device_id,
                body,
                sender: EndpointAddress::parse(&envelope.sender)?,
                sender_session_id: envelope.sender_session_id,
            })
        }

        pub fn rejection_reply_to(
            &self,
            recipient_endpoint: &EndpointAddress,
            recipient_session_id: &str,
            body: HardwareMessageBody,
            reason: &str,
            message: Option<&str>,
        ) -> Result<Option<DeckrMessage>> {
            let reply_body = match body {
                HardwareMessageBody::ControlCommand {
                    device_ref,
                    control_id,
                    capability_id,
                    command_type,
                    ..
                } => HardwareMessageBody::CommandRejected {
                    device_ref,
                    control_id,
                    capability_id,
                    command_type,
                    reason: reason.to_string(),
                    message: message.map(ToString::to_string),
                },
                HardwareMessageBody::CapabilityStateRequest {
                    device_ref,
                    control_id,
                    capability_id,
                    state_type,
                    ..
                } => HardwareMessageBody::CapabilityStateReply {
                    device_ref,
                    control_id,
                    capability_id,
                    state_type,
                    status: match reason {
                        "unsupported" => "unsupported",
                        "stale" => "unavailable",
                        _ => "rejected",
                    }
                    .to_string(),
                    value: None,
                    error: message.or(Some(reason)).map(ToString::to_string),
                },
                _ => return Ok(None),
            };
            let device_id = reply_body.device_ref().device_id.clone();
            DeckrMessage::hardware_input_to(
                &self.manager_id,
                &self.session_id,
                &device_id,
                recipient_endpoint.as_str(),
                recipient_session_id,
                reply_body,
            )
            .map(Some)
        }

        fn command_rejection_reply(
            &self,
            envelope: &DeckrMessage,
            reason: &str,
            message: Option<&str>,
        ) -> Result<Option<DeckrMessage>> {
            self.rejection_reply_to(
                &EndpointAddress::parse(&envelope.sender)?,
                &envelope.sender_session_id,
                envelope.hardware_body()?,
                reason,
                message,
            )
        }
    }

    pub fn accept_current_hardware_claim(
        contract: &ContractHandle,
        record: &ContractRecord,
        manager_endpoint: &EndpointAddress,
        manager_id: &str,
        current_devices: &BTreeMap<String, DeviceDescriptor>,
    ) -> Result<bool> {
        if record.profile.as_deref() != Some(HARDWARE_CLAIM_PROFILE_ID) {
            return Ok(false);
        }
        if !contract.participants.contains(manager_endpoint) {
            return Ok(false);
        }
        let Some(terms_value) = record.terms.clone() else {
            return Ok(false);
        };
        let Ok(terms) = HardwareClaimTerms::from_value(terms_value) else {
            return Ok(false);
        };
        if &terms.manager_endpoint != manager_endpoint {
            return Ok(false);
        }
        if terms.manager_endpoint.endpoint_id() != manager_id {
            return Ok(false);
        }
        if !contract.participants.contains(&terms.controller_endpoint) {
            return Ok(false);
        }
        for device in &terms.devices {
            let device_ref = &device.device_ref;
            if device_ref.manager_id != manager_id {
                return Ok(false);
            }
            let Some(current) = current_devices.get(&device_ref.device_id) else {
                return Ok(false);
            };
            if device_ref
                .fingerprint
                .as_ref()
                .is_some_and(|fingerprint| fingerprint != &current.fingerprint)
            {
                return Ok(false);
            }
        }
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lanes::{DeckrMessage, HardwareMessageBody};

    fn claim(endpoint: &str, session: &str) -> HardwareClaimRoute {
        HardwareClaimRoute {
            controller_endpoint: EndpointAddress::parse(endpoint).unwrap(),
            controller_session_id: session.to_string(),
            contract_key: "contracts.claim.1.meta".to_string(),
            claim_id: "claim-1".to_string(),
        }
    }

    #[test]
    fn claim_recipient_uses_valid_concord_route() {
        let mut routing = HardwareClaimRouting::default();
        routing.reconcile_snapshot(
            BTreeMap::from([("deck".to_string(), claim("controller:main", "s1"))]),
            BTreeSet::new(),
        );

        assert_eq!(
            routing.claim_recipient("deck"),
            Some(HardwareClaimRecipient {
                endpoint: &EndpointAddress::parse("controller:main").unwrap(),
                session_id: "s1",
            })
        );
    }

    #[test]
    fn missing_or_transferred_claim_resets_device() {
        let mut routing = HardwareClaimRouting::default();
        routing.reconcile_snapshot(
            BTreeMap::from([("deck".to_string(), claim("controller:main", "s1"))]),
            BTreeSet::new(),
        );

        let reset = routing.reconcile_snapshot(BTreeMap::new(), BTreeSet::new());
        assert!(reset.contains("deck"));

        routing.reconcile_snapshot(
            BTreeMap::from([("deck".to_string(), claim("controller:main", "s1"))]),
            BTreeSet::new(),
        );
        let reset = routing.reconcile_snapshot(
            BTreeMap::from([("deck".to_string(), claim("controller:other", "s2"))]),
            BTreeSet::new(),
        );
        assert!(reset.contains("deck"));
    }

    #[test]
    fn unchanged_or_invalid_claim_snapshot_keeps_policy_stable() {
        let mut routing = HardwareClaimRouting::default();
        routing.reconcile_snapshot(
            BTreeMap::from([("deck".to_string(), claim("controller:main", "s1"))]),
            BTreeSet::new(),
        );

        let reset = routing.reconcile_snapshot(
            BTreeMap::from([("deck".to_string(), claim("controller:main", "s1"))]),
            BTreeSet::new(),
        );
        assert!(!reset.contains("deck"));

        let reset = routing.reconcile_snapshot(BTreeMap::new(), BTreeSet::from(["deck".into()]));
        assert!(reset.contains("deck"));
        assert_eq!(routing.claim_recipient("deck"), None);
    }

    fn descriptor(device_id: &str) -> DeviceDescriptor {
        DeviceDescriptor {
            device_id: device_id.to_string(),
            fingerprint: format!("fingerprint:{device_id}"),
            display_name: "Test Device".to_string(),
            manufacturer: None,
            model: None,
            serial_number: None,
            controls: Vec::new(),
            capabilities: Vec::new(),
        }
    }

    fn input_body(device_id: &str) -> HardwareMessageBody {
        HardwareMessageBody::ControlInput {
            device_ref: DeviceRef {
                manager_id: "manager-main".to_string(),
                device_id: device_id.to_string(),
                fingerprint: None,
            },
            control_id: "button.1".to_string(),
            capability_id: "button.press".to_string(),
            event_type: "press".to_string(),
            value: None,
            occurred_at: None,
            sequence: None,
            sources: Vec::new(),
        }
    }

    #[test]
    fn hardware_manager_runtime_routes_only_live_claimed_input() {
        let mut runtime =
            runtime::HardwareManagerRuntime::new("manager-main", "manager-session").unwrap();
        runtime.set_device(descriptor("deck")).unwrap();

        assert!(runtime
            .route_hardware_message(input_body("deck"))
            .unwrap()
            .is_none());

        runtime.reconcile_routing_snapshot(
            [(
                "deck".to_string(),
                HardwareClaimRoute {
                    controller_endpoint: EndpointAddress::parse("controller:main").unwrap(),
                    controller_session_id: "controller-session".to_string(),
                    contract_key: "contracts.claim.1.meta".to_string(),
                    claim_id: "claim-1".to_string(),
                },
            )],
            BTreeSet::new(),
        );
        let routed = runtime
            .route_hardware_message(input_body("deck"))
            .unwrap()
            .unwrap();

        assert_eq!(routed.sender, "hardware_manager:manager-main");
        assert_eq!(routed.sender_session_id, "manager-session");
        assert_eq!(routed.recipient_endpoint(), Some("controller:main"));
        assert_eq!(
            routed.recipient_session_id.as_deref(),
            Some("controller-session")
        );
    }

    #[test]
    fn hardware_manager_runtime_rejects_unclaimed_controller_command() {
        let mut runtime =
            runtime::HardwareManagerRuntime::new("manager-main", "manager-session").unwrap();
        runtime.set_device(descriptor("deck")).unwrap();
        runtime.reconcile_routing_snapshot(
            [(
                "deck".to_string(),
                HardwareClaimRoute {
                    controller_endpoint: EndpointAddress::parse("controller:main").unwrap(),
                    controller_session_id: "controller-session".to_string(),
                    contract_key: "contracts.claim.1.meta".to_string(),
                    claim_id: "claim-1".to_string(),
                },
            )],
            BTreeSet::new(),
        );
        let command = DeckrMessage::hardware_command(
            "other",
            "other-session",
            "manager-main",
            "manager-session",
            "deck",
            HardwareMessageBody::ControlCommand {
                device_ref: DeviceRef {
                    manager_id: "manager-main".to_string(),
                    device_id: "deck".to_string(),
                    fingerprint: None,
                },
                control_id: Some("screen".to_string()),
                capability_id: "raster.bitmap".to_string(),
                command_type: "clear".to_string(),
                params: Default::default(),
            },
        )
        .unwrap();

        let decision = runtime.authorize_command(command).unwrap();

        match decision {
            runtime::HardwareCommandDecision::Rejected { reason, reply } => {
                assert_eq!(reason, "unauthorized");
                let reply = reply.expect("rejection should produce commandRejected");
                assert_eq!(reply.message_type, "commandRejected");
                assert_eq!(reply.recipient_endpoint(), Some("controller:other"));
            }
            other => panic!("expected rejection, got {other:?}"),
        }
    }
}
