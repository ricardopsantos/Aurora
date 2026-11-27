//! API keys (Python `aurora/keystore.py`, R22). Resolution: environment
//! variable → OS keyring → `keys.enc` → prompt. This build reads the
//! environment and `keys.enc` (Fernet, PBKDF2-SHA256 × 600 000 over
//! `keys.salt`, so a file Python wrote opens here unchanged). The OS keyring
//! and storing keys are not ported yet: `aurora key set` stays Python's.

use std::collections::HashMap;
use std::path::Path;
use std::sync::Mutex;

use base64::Engine as _;
use serde_json::Value;

use crate::{Result, StorageError};

const ENC_FILE: &str = "keys.enc";
const SALT_FILE: &str = "keys.salt";
const KDF_ROUNDS: u32 = 600_000;

/// Decrypt the whole `keys.enc` in `home` with `passphrase`.
pub fn read_encrypted(home: &Path, passphrase: &str) -> Result<HashMap<String, String>> {
    let enc = home.join(ENC_FILE);
    if !enc.exists() {
        return Ok(HashMap::new());
    }
    let salt = std::fs::read(home.join(SALT_FILE)).map_err(|e| StorageError::Keystore(format!("keys.salt: {e}")))?;
    let mut key = [0u8; 32];
    pbkdf2::pbkdf2_hmac::<sha2::Sha256>(passphrase.as_bytes(), &salt, KDF_ROUNDS, &mut key);
    let fernet = fernet::Fernet::new(&base64::engine::general_purpose::URL_SAFE.encode(key))
        .ok_or_else(|| StorageError::Keystore("bad derived key".into()))?;
    let token = std::fs::read_to_string(&enc)?;
    let plain = fernet
        .decrypt(token.trim())
        .map_err(|_| StorageError::Keystore("the key store did not open: wrong passphrase?".into()))?;
    let map: HashMap<String, Value> =
        serde_json::from_slice(&plain).map_err(|e| StorageError::Keystore(format!("keys.enc: {e}")))?;
    Ok(map.into_iter().filter_map(|(k, v)| v.as_str().map(|s| (k, s.to_string()))).collect())
}

/// Key lookup for one process. The passphrase is asked once and held in
/// memory only; a wrong one is dropped so the next lookup asks again.
pub struct Keystore {
    home: std::path::PathBuf,
    prompter: Box<dyn Fn(&str) -> String + Send + Sync>,
    passphrase: Mutex<Option<String>>,
}

impl Keystore {
    pub fn new(home: std::path::PathBuf, prompter: Box<dyn Fn(&str) -> String + Send + Sync>) -> Self {
        Self { home, prompter, passphrase: Mutex::new(None) }
    }

    pub fn get(&self, env_var: &str, interactive: bool) -> Option<String> {
        if let Ok(v) = std::env::var(env_var) {
            if !v.is_empty() {
                return Some(v);
            }
        }
        if !self.home.join(ENC_FILE).exists() {
            return None;
        }
        let mut cached = self.passphrase.lock().expect("passphrase lock");
        if cached.is_none() {
            if !interactive {
                return None;
            }
            let entered = (self.prompter)("Aurora key-store passphrase: ");
            if entered.is_empty() {
                return None;
            }
            *cached = Some(entered);
        }
        match read_encrypted(&self.home, cached.as_deref().unwrap_or_default()) {
            Ok(keys) => keys.get(env_var).cloned(),
            Err(_) => {
                *cached = None;
                None
            }
        }
    }
}
