//! Aurora's on-disk formats, byte-compatible with the Python release so both
//! can share one AURORA_HOME (documents/migration/05-DATA-COMPATIBILITY.md).

pub mod config;
pub mod keystore;
pub mod paths;

pub mod session;

#[derive(Debug, thiserror::Error)]
pub enum StorageError {
    #[error("{0}")]
    Io(String),
    #[error("{0}")]
    Format(String),
    #[error("{0}")]
    Keystore(String),
}

impl From<std::io::Error> for StorageError {
    fn from(e: std::io::Error) -> Self {
        StorageError::Io(e.to_string())
    }
}

pub type Result<T> = std::result::Result<T, StorageError>;
pub mod approvals;
pub mod bootstrap;
pub mod pathlike;
pub mod rewind;
pub mod rules;
