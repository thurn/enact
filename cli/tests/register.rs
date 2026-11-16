use sha2::{Digest, Sha256};
use std::fs;
use std::path::Path;
use std::process::{Command, Output};
use tempfile::tempdir;

fn checked(output: Output) -> String {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

fn git(directory: &Path, args: &[&str]) -> String {
    checked(
        Command::new("git")
            .current_dir(directory)
            .args(args)
            .output()
            .unwrap(),
    )
}

#[test]
fn registration_rotates_one_machine_ref_and_keeps_the_token_private() {
    let temp = tempdir().unwrap();
    let remote = temp.path().join("remote");
    let repository = temp.path().join("project");
    let home = temp.path().join("home");
    fs::create_dir(&remote).unwrap();
    fs::create_dir(&repository).unwrap();
    fs::create_dir(&home).unwrap();
    git(&remote, &["init", "--bare", "--initial-branch=main"]);
    git(&repository, &["init", "--initial-branch=main"]);
    git(&repository, &["config", "user.name", "Token test"]);
    git(
        &repository,
        &["config", "user.email", "token-test@example.invalid"],
    );
    git(&repository, &["config", "commit.gpgsign", "false"]);
    git(&repository, &["commit", "--allow-empty", "-m", "First"]);
    let first = git(&repository, &["rev-parse", "HEAD"]);
    git(&repository, &["commit", "--allow-empty", "-m", "Second"]);
    git(
        &repository,
        &["remote", "add", "project-remote", remote.to_str().unwrap()],
    );
    let other = format!("HEAD:refs/enact/tokens/other-machine/{}", "f".repeat(64));
    git(&repository, &["push", "project-remote", "main", &other]);
    fs::write(
        repository.join(".enact.toml"),
        "remote = 'project-remote'\n",
    )
    .unwrap();

    let token_path = if cfg!(windows) {
        home.clone()
    } else {
        home.join(".config")
    }
    .join("enact")
    .join("token");
    let register = |directory: &Path, flags: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_tk"))
            .current_dir(directory)
            .env(if cfg!(windows) { "APPDATA" } else { "HOME" }, &home)
            .env("ENACT_HOST", "Test-Machine")
            .args(["register", "--json"])
            .args(flags)
            .output()
            .unwrap()
    };
    let verify =
        || {
            let result = checked(register(&repository, &[]));
            assert_eq!(result.trim(), r#"{"machine":"test-machine"}"#);
            let saved = fs::read_to_string(&token_path).unwrap();
            let token = saved.trim();
            assert_eq!(token.len(), 64);
            assert!(token.bytes().all(|byte| byte.is_ascii_hexdigit()));
            let hash = format!("{:x}", Sha256::digest(token.as_bytes()));
            let refs = git(
                &remote,
                &[
                    "for-each-ref",
                    "--format=%(objectname) %(refname)",
                    "refs/enact/tokens",
                ],
            );
            let lines: Vec<_> = refs.lines().collect();
            assert_eq!(lines.len(), 2);
            assert!(lines.contains(
                &format!("{} refs/enact/tokens/test-machine/{hash}", first.trim()).as_str()
            ));
            assert!(
                lines
                    .iter()
                    .any(|line| line.ends_with(other.strip_prefix("HEAD:").unwrap()))
            );
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                assert_eq!(
                    fs::metadata(&token_path).unwrap().permissions().mode() & 0o777,
                    0o600
                );
            }
            saved
        };
    let enrolled = checked(register(&repository, &["--ensure"]));
    assert_eq!(enrolled.trim(), r#"{"machine":"test-machine"}"#);
    let saved = fs::read_to_string(&token_path).unwrap();
    let hash = format!("{:x}", Sha256::digest(saved.trim().as_bytes()));
    let reference = format!("refs/enact/tokens/test-machine/{hash}");
    assert_eq!(git(&remote, &["rev-parse", &reference]), first);

    let second_remote = temp.path().join("second-remote.git");
    let second_checkout = temp.path().join("second-checkout");
    git(
        temp.path(),
        &[
            "clone",
            "--bare",
            remote.to_str().unwrap(),
            second_remote.to_str().unwrap(),
        ],
    );
    git(
        temp.path(),
        &[
            "clone",
            second_remote.to_str().unwrap(),
            second_checkout.to_str().unwrap(),
        ],
    );
    fs::write(
        second_checkout.join(".enact.toml"),
        "[tickets]\nproject = 'en'\n",
    )
    .unwrap();
    assert!(
        git(&second_remote, &["for-each-ref", "refs/enact/tokens"]).is_empty(),
        "the second remote must start without registration refs"
    );
    let linked_worktree = temp.path().join("linked-worktree");
    git(
        &second_checkout,
        &[
            "worktree",
            "add",
            "--detach",
            linked_worktree.to_str().unwrap(),
            "HEAD",
        ],
    );
    assert!(!linked_worktree.join(".enact.toml").exists());
    checked(register(&linked_worktree, &["--ensure"]));
    assert_eq!(git(&second_remote, &["rev-parse", &reference]), first);
    assert_eq!(fs::read_to_string(&token_path).unwrap(), saved);
    git(
        &second_remote,
        &["config", "receive.advertiseAtomic", "false"],
    );
    checked(register(&second_checkout, &["--ensure"]));
    assert_eq!(fs::read_to_string(&token_path).unwrap(), saved);

    let original = verify();
    let rotated = verify();
    assert_ne!(rotated, original);

    git(&remote, &["config", "receive.advertiseAtomic", "false"]);
    let refs = git(&remote, &["for-each-ref", "refs/enact/tokens"]);
    assert!(!register(&repository, &[]).status.success());
    assert!(fs::read_to_string(&token_path).unwrap() == rotated);
    assert_eq!(git(&remote, &["for-each-ref", "refs/enact/tokens"]), refs);
}
