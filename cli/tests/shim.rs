use std::process::Command;

#[test]
fn replay_bd_corpus() {
    let script = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/shim-replay.py");
    let status = Command::new("python3")
        .arg("-u")
        .arg(script)
        .arg(env!("CARGO_BIN_EXE_tk"))
        .status()
        .expect("start corpus replay");
    assert!(status.success(), "corpus replay failed");
}
