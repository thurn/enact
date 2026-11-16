use std::process::Command;

#[test]
fn version_prints_the_package_version() {
    let output = Command::new(env!("CARGO_BIN_EXE_tk"))
        .arg("version")
        .output()
        .expect("run tk version");

    assert!(output.status.success());
    assert_eq!(
        String::from_utf8_lossy(&output.stdout).trim_end(),
        env!("CARGO_PKG_VERSION")
    );
}
