//! R480: a `keys.enc` written by Python's `keystore._encfile_save` (the
//! fixture, passphrase `fixture-passphrase`) opens here unchanged.

use std::path::PathBuf;

use aurora_storage::keystore::{read_encrypted, Keystore};

fn fixture() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/keystore")
}

#[test]
fn decrypts_a_python_key_store() {
    let keys = read_encrypted(&fixture(), "fixture-passphrase").unwrap();
    assert_eq!(keys["EXAMPLE_API_KEY"], "not-a-real-key-olá");
    assert!(read_encrypted(&fixture(), "wrong").is_err());
}

#[test]
fn asks_for_the_passphrase_once_and_never_when_not_interactive() {
    let asked = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let a2 = asked.clone();
    let ks = Keystore::new(fixture(), Box::new(move |_| {
        a2.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        "fixture-passphrase".into()
    }));
    assert_eq!(ks.get("AURORA_T_NOT_SET_EXAMPLE", false), None);
    assert_eq!(ks.get("EXAMPLE_API_KEY", true).as_deref(), Some("not-a-real-key-olá"));
    assert_eq!(ks.get("OTHER_TOKEN", true).as_deref(), Some("x"));
    assert_eq!(asked.load(std::sync::atomic::Ordering::SeqCst), 1);
}
