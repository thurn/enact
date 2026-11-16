use crate::client::{Result, read_config, token, token_path, usage};
use sha2::{Digest, Sha256};
use std::fs::{self, File};
use std::io::{ErrorKind, Write};
use std::path::Path;
use std::process::{Command, Output};
use tempfile::NamedTempFile;

pub fn run(ensure: bool) -> Result<String> {
    let here = std::env::current_dir().map_err(usage)?;
    let (config_path, config) = read_config(&here)?;
    let repository = config_path.parent().expect("config directory");
    let remote = config
        .get("remote")
        .and_then(toml::Value::as_str)
        .unwrap_or("origin");
    let machine = machine_name()?;
    let path = token_path()?;
    let directory = path.parent().expect("token directory");
    fs::create_dir_all(directory).map_err(usage)?;
    let lock = File::options()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(directory.join("register.lock"))
        .map_err(usage)?;
    lock.lock().map_err(usage)?;

    match fs::symlink_metadata(&path) {
        Ok(metadata) if !metadata.is_file() => {
            return Err(usage(format!("{} is not a regular file", path.display())));
        }
        Ok(_) if ensure => {
            publish(repository, remote, &machine, &token_hash(&token()?))?;
            return Ok(machine);
        }
        Ok(_) => {}
        Err(error) if error.kind() == ErrorKind::NotFound => {}
        Err(error) => return Err(usage(error)),
    }

    let mut random = [0_u8; 32];
    getrandom::fill(&mut random).map_err(usage)?;
    let token: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
    let hash = token_hash(&token);
    let mut pending = NamedTempFile::new_in(directory).map_err(usage)?;
    writeln!(pending, "{token}").map_err(usage)?;
    pending.as_file().sync_all().map_err(usage)?;

    publish(repository, remote, &machine, &hash)?;
    pending.persist(&path).map_err(|error| {
        usage(format!(
            "could not replace {} after publishing the ref: {}",
            path.display(),
            error.error
        ))
    })?;
    Ok(machine)
}

fn token_hash(token: &str) -> String {
    format!("{:x}", Sha256::digest(token.as_bytes()))
}

fn machine_name() -> Result<String> {
    let name = match std::env::var("ENACT_HOST")
        .ok()
        .filter(|name| !name.is_empty())
    {
        Some(name) => name,
        None => command_output(Command::new("hostname").output(), "hostname")?
            .trim()
            .split('.')
            .next()
            .unwrap_or_default()
            .to_owned(),
    };
    let name = name.to_ascii_lowercase();
    if !name.starts_with(|character: char| character.is_ascii_alphanumeric())
        || !name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"-_".contains(&byte))
    {
        return Err(usage(
            "the machine name must start with a letter or number and use only letters, numbers, hyphens and underscores",
        ));
    }
    Ok(name)
}

fn publish(repository: &Path, remote: &str, machine: &str, hash: &str) -> Result<()> {
    let prefix = format!("refs/enact/tokens/{machine}/");
    let pattern = format!("{prefix}*");
    let advertised = git(repository, &["ls-remote", "--", remote, "HEAD", &pattern])?;
    let reference = format!("{prefix}{hash}");
    let mut head = None;
    let mut registered = false;
    let mut removals = Vec::new();
    for (oid, name) in advertised.lines().filter_map(|line| line.split_once('\t')) {
        if name == "HEAD" {
            head = Some(oid);
        } else if name == reference {
            registered = true;
        } else if name.starts_with(&prefix) {
            removals.push(format!(":{name}"));
        }
    }
    if registered && removals.is_empty() {
        return Ok(());
    }
    let head = head.ok_or_else(|| usage("the Git remote has no default branch commit"))?;
    if git(repository, &["rev-parse", "--is-shallow-repository"])?.trim() == "true" {
        return Err(usage(
            "register requires a full clone to find the remote’s first commit",
        ));
    }
    if git(repository, &["cat-file", "-e", head]).is_err() {
        git(
            repository,
            &[
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--",
                remote,
                head,
            ],
        )?;
    }
    let root = git(
        repository,
        &["rev-list", "--first-parent", "--max-parents=0", head],
    )?;
    let registration = format!("{}:{reference}", root.trim());
    let mut args = vec![
        "-c",
        "push.followTags=false",
        "push",
        "--atomic",
        "--",
        remote,
        &registration,
    ];
    args.extend(removals.iter().map(String::as_str));
    git(repository, &args)?;
    Ok(())
}

fn git(repository: &Path, args: &[&str]) -> Result<String> {
    command_output(
        Command::new("git")
            .current_dir(repository)
            .args(args)
            .output(),
        "git",
    )
}

fn command_output(output: std::io::Result<Output>, program: &str) -> Result<String> {
    let output = output.map_err(|error| usage(format!("could not run {program}: {error}")))?;
    if !output.status.success() {
        let detail = String::from_utf8_lossy(&output.stderr);
        return Err(usage(format!("{program} failed: {}", detail.trim())));
    }
    String::from_utf8(output.stdout).map_err(usage)
}
