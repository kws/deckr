from __future__ import annotations

from types import MappingProxyType

from deckr.substrates.nats_kv import KvBucketPolicy

DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME = "deckr_beacon_advertisement_v1"
DEFAULT_BEACON_TTL_SECONDS = 300
DEFAULT_CONCORD_CONTRACT_BUCKET_NAME = "deckr_concord_contract_v1"
DEFAULT_CONCORD_TOKEN_BUCKET_NAME = "deckr_concord_token_v1"
DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME = "deckr_concord_maintenance_v1"

BEACON_ADVERTISEMENT_STORE_POLICY = KvBucketPolicy(
    bucket=DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME,
    ttl_seconds=float(DEFAULT_BEACON_TTL_SECONDS),
    allow_write_ttl=True,
    description="Beacon advertisement KV",
)
CONCORD_CONTRACT_BUCKET_POLICY = KvBucketPolicy(
    bucket=DEFAULT_CONCORD_CONTRACT_BUCKET_NAME,
    ttl_seconds=None,
    description="Concord contract KV",
)
CONCORD_MAINTENANCE_BUCKET_POLICY = KvBucketPolicy(
    bucket=DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME,
    ttl_seconds=None,
    description="Concord maintenance KV",
)
CONCORD_TOKEN_BUCKET_POLICY = KvBucketPolicy(
    bucket=DEFAULT_CONCORD_TOKEN_BUCKET_NAME,
    ttl_seconds=120.0,
    allow_write_ttl=True,
    description="Concord participant token KV",
)

# Core authority bucket names are reserved across all component roles. This is
# the single internal registry used by source guards and runtime owners; the
# existing public constants remain re-exported by deckr.beacon and deckr.concord.
RESERVED_AUTHORITY_BUCKET_POLICIES = MappingProxyType(
    {
        policy.bucket: policy
        for policy in (
            BEACON_ADVERTISEMENT_STORE_POLICY,
            CONCORD_CONTRACT_BUCKET_POLICY,
            CONCORD_TOKEN_BUCKET_POLICY,
            CONCORD_MAINTENANCE_BUCKET_POLICY,
        )
    }
)
