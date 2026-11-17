use serde_json::Value;
use std::process::{Command, Output};

fn offline(args: &[&str]) -> Output {
    let directory = tempfile::tempdir().unwrap();
    Command::new(env!("CARGO_BIN_EXE_tk"))
        .args(args)
        .current_dir(directory.path())
        .env("HOME", directory.path())
        .env("APPDATA", directory.path())
        .output()
        .unwrap()
}

#[test]
fn help_and_agent_guidance_need_no_project_or_network() {
    for args in [
        vec!["--help"],
        vec!["help", "batch"],
        vec!["prime"],
        vec!["version"],
    ] {
        let output = offline(&args);
        assert!(output.status.success(), "{args:?}: {:?}", output.stderr);
        assert!(output.stderr.is_empty());
        assert!(!output.stdout.is_empty());
        assert!(
            !output.stdout.contains(&b'\x1b'),
            "redirected help must not contain ANSI escapes"
        );
    }
    let output = offline(&["prime", "--json"]);
    assert!(output.status.success());
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    let guide = value["guide"].as_str().unwrap();
    assert!(guide.contains("results[0].id"));
    assert!(guide.contains("--if-version"));
    assert!(guide.contains("4 server unavailable"));
    assert!(
        guide.len() < 6500,
        "keep the startup guide small enough for agent context"
    );
}

#[test]
fn local_failures_have_json_and_nonzero_status() {
    // Parser, enrollment and file errors all need useful JSON, even before HTTP.
    for args in [
        vec!["--json", "unknown-command"],
        vec!["create", "--json"],
        vec!["create", "title", "-p", "9", "--json"],
        vec!["--json", "show", "Tabcd"],
        vec![
            "--json",
            "create",
            "title",
            "--description",
            "text",
            "--body-file",
            "file",
        ],
    ] {
        let output = offline(&args);
        assert_eq!(output.status.code(), Some(1), "{args:?}");
        let value: Value = serde_json::from_slice(&output.stdout)
            .unwrap_or_else(|_| panic!("{args:?}: {:?}", output));
        assert!(!value["error"].as_str().unwrap().is_empty());
        assert_eq!(value["code"], 1);
        assert!(!output.stderr.is_empty());
    }
}
