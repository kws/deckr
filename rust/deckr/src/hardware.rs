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

#[cfg(test)]
mod tests {
    use super::*;

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
}
