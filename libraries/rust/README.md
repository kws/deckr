# deckr-core

Rust core helpers for the Deckr `contract/v1` artifact set.

This crate is a peer of the Python `deckr` package. Its API is Rust-native:
Serde structs and helper functions are shaped for Rust callers, while static
conformance verifies the same observable contract behavior as Python.

Run the Rust checks from the repository root:

```bash
cargo test --manifest-path libraries/rust/Cargo.toml
cargo run --manifest-path libraries/rust/Cargo.toml --bin deckr-rust-static-conformance -- --output /tmp/deckr-rust-report.json
```

