use super::*;
use std::ffi::OsStr;

#[test]
fn reads_only_login_path_despite_banner() {
    let path = shell_path(
        OsStr::new("/bin/sh"),
        "printf 'welcome\\n'; export PATH='/my tools:/bin'; printf '\\0%s\\0' \"$PATH\"",
        Duration::from_secs(2),
    );
    assert_eq!(path.unwrap(), OsStr::new("/my tools:/bin"));
}

#[test]
fn failed_or_stuck_login_returns_promptly() {
    for script in ["exit 1", "sleep 10"] {
        let start = std::time::Instant::now();
        assert!(shell_path(OsStr::new("/bin/sh"), script, Duration::from_millis(250)).is_none());
        assert!(start.elapsed() < Duration::from_secs(3));
    }
}

#[test]
fn accepts_only_the_ready_message_with_a_real_loopback_port() {
    assert_eq!(
        ready_port("atelier serve running at http://127.0.0.1:58123"),
        Some(58123)
    );
    for line in [
        "http://127.0.0.1:8000",
        "atelier serve running at http://127.0.0.1:0",
        "atelier serve running at http://example.org:443",
    ] {
        assert_eq!(ready_port(line), None);
    }
}
