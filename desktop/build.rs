use std::{env, path::PathBuf, process::Command};

fn main() {
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let object = out.join("notifications.o");
    assert!(Command::new("clang")
        .args([
            "-fobjc-arc",
            "-mmacosx-version-min=12.0",
            "-c",
            "src/notifications.m",
            "-o"
        ])
        .arg(&object)
        .status()
        .unwrap()
        .success());
    println!("cargo:rustc-link-arg={}", object.display());
    println!("cargo:rustc-link-lib=framework=UserNotifications");
    println!("cargo:rerun-if-changed=src/notifications.m");
    tauri_build::build()
}
