use std::process::Command;

#[test]
fn replay_bd_corpus() {
    replay(&[]);
}

#[test]
#[ignore = "requires real bd and dolt CLIs"]
fn verify_frozen_beads_audit_history() {
    replay(&["--audit"]);
}

#[test]
#[ignore = "requires real bd and dolt CLIs"]
fn rehearse_project_cutovers() {
    python("cutover-rehearsal.py", &[]);
}

fn replay(args: &[&str]) {
    python("shim-replay.py", args);
}

fn python(script: &str, args: &[&str]) {
    let script = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join(script);
    let status = Command::new("python3")
        .arg("-u")
        .arg(script)
        .arg(env!("CARGO_BIN_EXE_tk"))
        .args(args)
        .status()
        .expect("start corpus replay");
    assert!(status.success(), "corpus replay failed");
}
