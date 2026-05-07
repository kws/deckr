use std::path::{Path, PathBuf};

use serde_json::Value;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ArtifactError {
    #[error("could not find contract/v1 from the Rust source checkout")]
    MissingContractRoot,
    #[error("failed to read {path}: {source}")]
    Read {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("failed to parse JSON in {path}: {source}")]
    Json {
        path: PathBuf,
        #[source]
        source: serde_json::Error,
    },
}

pub fn default_contract_root() -> Result<PathBuf, ArtifactError> {
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    for base in manifest_dir.ancestors() {
        let candidate = base.join("contract").join("v1");
        if candidate.join("manifest.json").exists() {
            return Ok(candidate);
        }
    }
    let cwd = std::env::current_dir().map_err(|source| ArtifactError::Read {
        path: PathBuf::from("."),
        source,
    })?;
    for base in cwd.ancestors() {
        let candidate = base.join("contract").join("v1");
        if candidate.join("manifest.json").exists() {
            return Ok(candidate);
        }
    }
    Err(ArtifactError::MissingContractRoot)
}

pub fn read_json(path: impl AsRef<Path>) -> Result<Value, ArtifactError> {
    let path = path.as_ref();
    let data = std::fs::read_to_string(path).map_err(|source| ArtifactError::Read {
        path: path.to_path_buf(),
        source,
    })?;
    serde_json::from_str(&data).map_err(|source| ArtifactError::Json {
        path: path.to_path_buf(),
        source,
    })
}

pub fn load_manifest(contract_root: impl AsRef<Path>) -> Result<Value, ArtifactError> {
    read_json(contract_root.as_ref().join("manifest.json"))
}
