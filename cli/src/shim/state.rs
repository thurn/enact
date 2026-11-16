use super::{Options, git_config, run};
use crate::args::{Wait, WaitMode};
use crate::client::{Client, Result, read_config, token_path, usage};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::ffi::{OsStr, OsString};
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;
use std::{env, io};

const CACHE_FOR: Duration = Duration::from_secs(5);

pub(super) fn dispatch(
    args: &[OsString],
    reads: bool,
    options: impl FnOnce() -> Result<Options>,
) -> Result<u8> {
    if env::var_os("ENACT_SHIM_DRAINING").is_some() {
        return real(args);
    }
    let here = env::current_dir().map_err(usage)?;
    let Ok((_, config)) = read_config(&here) else {
        return real(args);
    };
    if config.get("tickets").is_none() {
        return real(args);
    }
    let initial = Client::load(None, "bd-shim".into(), Duration::from_secs(120))?;
    let mut gate = Gate::open(initial)?;
    loop {
        // Keep the write lock until legacy bd exits, so a drain includes any
        // direct writer that read "before" just as the pause began.
        let in_enact = env::var_os("ENACT_BD_IN_FLIGHT").is_some();
        let write_lock = if reads || in_enact {
            None
        } else {
            Some(gate.lock("writers.lock", false)?)
        };
        let snapshot = gate.snapshot(reads)?;
        match snapshot["cutover"].as_str() {
            Some("before") => return real(args),
            Some("after") => {
                drop(write_lock);
                let options = options()?;
                if !reads {
                    gate.client.actor = options
                        .actor
                        .clone()
                        .or_else(|| git_config("user.name"))
                        .unwrap_or_else(|| "bd".into());
                }
                return run(&gate.client, options);
            }
            // A legacy Enact operation that already holds the ownership
            // lock finishes before the drain can acquire that same lock.
            Some("paused") if in_enact => return real(args),
            Some("paused") => {
                drop(write_lock);
                gate.drain(&snapshot)?;
                let seq = snapshot["seq"]
                    .as_u64()
                    .ok_or_else(|| usage("cutover state has no sequence"))?;
                // Recheck state after a feed change or a wait deadline. The
                // initial cursor also catches transitions that raced the drain.
                if let Err(error) = crate::feed::wait(
                    &gate.client,
                    Wait {
                        mode: WaitMode::Change,
                        since: Some(seq),
                        timeout: Some(120.0),
                    },
                ) && error.code != 6
                {
                    return Err(error);
                }
            }
            _ => return Err(usage("invalid ticket cutover state")),
        }
    }
}

struct Gate {
    client: Client,
    directory: PathBuf,
}

impl Gate {
    fn open(client: Client) -> Result<Self> {
        let url = client.url(&["state"], &[]);
        let key = format!("{:x}", Sha256::digest(url.as_str().as_bytes()));
        let directory = token_path()?.with_file_name("bd-shim").join(key);
        fs::create_dir_all(&directory).map_err(usage)?;
        Ok(Self { client, directory })
    }

    fn lock(&self, name: &str, exclusive: bool) -> Result<File> {
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.directory.join(name))
            .map_err(usage)?;
        if exclusive {
            lock.lock()
        } else {
            lock.lock_shared()
        }
        .map_err(usage)?;
        Ok(lock)
    }

    fn snapshot(&self, cached: bool) -> Result<Value> {
        let permanent = self.directory.join("after");
        if cached && permanent.is_file() {
            return Ok(json!({"cutover": "after"}));
        }
        let cache = self.directory.join("state.json");
        let saved = || {
            fs::read(&cache)
                .ok()
                .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        };
        if cached
            && cache
                .metadata()
                .and_then(|metadata| metadata.modified())
                .ok()
                .and_then(|time| time.elapsed().ok())
                .is_some_and(|age| age < CACHE_FOR)
            && let Some(state) = saved()
            && state["cutover"] != "paused"
        {
            return Ok(state);
        }
        let request = self
            .client
            .get(
                &self.client.url(&["state"], &[]),
                None,
                Duration::from_secs(2),
            )
            .and_then(crate::client::Reply::json);
        // A response issued before the pause may arrive after another
        // process has cached the pause. Keep the newest observed sequence.
        let _lock = self.lock("state.lock", true)?;
        let prior = saved();
        if permanent.is_file() && !matches!(&request, Ok(state) if state["cutover"] == "after") {
            return Ok(json!({"cutover": "after"}));
        }
        let state = match request {
            Ok(state)
                if matches!(
                    state["cutover"].as_str(),
                    Some("before" | "paused" | "after")
                ) =>
            {
                state
            }
            result
                if prior
                    .as_ref()
                    .is_some_and(|state| state["cutover"] == "paused") =>
            {
                return Err(result
                    .err()
                    .unwrap_or_else(|| usage("invalid paused cutover response")));
            }
            _ => return Ok(json!({"cutover": "before"})),
        };
        if let Some(prior) = prior
            && prior["seq"].as_u64() > state["seq"].as_u64()
        {
            return Ok(prior);
        }
        if state["cutover"] == "after" {
            fs::write(permanent, "").map_err(usage)?;
        }
        self.persist(&cache, &state.to_string())?;
        Ok(state)
    }

    fn persist(&self, destination: &Path, contents: &str) -> Result<()> {
        let temp = destination.with_extension(format!("{}.tmp", std::process::id()));
        fs::write(&temp, contents).map_err(usage)?;
        if let Err(error) = fs::rename(&temp, destination) {
            let _ = fs::remove_file(&temp);
            return Err(usage(error));
        }
        Ok(())
    }

    fn drain(&self, snapshot: &Value) -> Result<()> {
        let lock = self.lock("writers.lock", true)?;
        let at = snapshot["cutover_at"]
            .as_str()
            .ok_or_else(|| usage("pause has no timestamp"))?;
        let marker = self.directory.join("drained");
        if fs::read_to_string(&marker).is_ok_and(|saved| saved == at) {
            return Ok(());
        }
        let current = self.snapshot(false)?;
        if current["cutover"] != "paused" || current["cutover_at"] != at {
            return Ok(());
        }
        let helper = env::var_os("ENACT_SHIM_HELPER")
            .map(PathBuf::from)
            .or_else(|| env::home_dir().map(|home| home.join("enact/scripts/enact.py")))
            .ok_or_else(|| usage("cannot locate enact.py for the cutover drain"))?;
        let python = env::var_os("ENACT_SHIM_PYTHON").unwrap_or_else(|| "python3".into());
        let sync = Command::new(python)
            .arg(helper)
            .arg("sync")
            .env("ENACT_SYNC_NOW", "1")
            .env("ENACT_SHIM_DRAINING", "1")
            .stdin(Stdio::null())
            .output()
            .map_err(usage)?;
        if !sync.status.success() {
            return Err(usage(format!(
                "enact sync failed during the cutover drain: {}",
                String::from_utf8_lossy(&sync.stderr).trim()
            )));
        }
        let result: Value = serde_json::from_slice(&sync.stdout).map_err(usage)?;
        if result["remote"] != true || result["deferred"] == true {
            return Err(usage("enact sync did not publish the legacy store"));
        }
        let state = self.snapshot(false)?;
        if state["cutover"] != "paused" || state["cutover_at"] != at {
            return Ok(());
        }
        self.client
            .post(
                &self.client.url(&["events"], &[]),
                &json!({"kind": "machine", "data": {"drained": true, "cutover_at": at}}),
                false,
            )?
            .json()?;
        self.persist(&marker, at)?;
        drop(lock);
        Ok(())
    }
}

pub(crate) fn real_binary() -> Result<PathBuf> {
    let current = env::current_exe()
        .and_then(fs::canonicalize)
        .map_err(usage)?;
    if let Some(binary) = env::var_os("ENACT_REAL_BD") {
        let candidate = fs::canonicalize(binary).map_err(usage)?;
        if candidate == current {
            return Err(usage("ENACT_REAL_BD points to the bd shim"));
        }
        return Ok(candidate);
    }
    let paired_name = format!("bd-real{}", env::consts::EXE_SUFFIX);
    let paired = |entry: &Path| {
        fs::canonicalize(entry.with_file_name(&paired_name))
            .ok()
            .filter(|candidate| candidate.is_file() && *candidate != current)
    };
    if super::invoked_as_bd()
        && let Some(sibling) = paired(&current)
    {
        return Ok(sibling);
    }
    let path = env::var_os("PATH").unwrap_or_default();
    let name = format!("bd{}", env::consts::EXE_SUFFIX);
    env::split_paths(&path)
        .map(|dir| dir.join(&name))
        .filter(|entry| entry.is_file())
        .find_map(|entry| {
            paired(&entry).or_else(|| {
                fs::canonicalize(entry)
                    .ok()
                    .filter(|candidate| *candidate != current)
            })
        })
        .ok_or_else(|| usage("bd: cannot find the real Beads binary; set ENACT_REAL_BD"))
}

fn real(args: &[impl AsRef<OsStr>]) -> Result<u8> {
    let status = Command::new(real_binary()?)
        .args(args)
        .status()
        .map_err(|error: io::Error| usage(format!("bd: {error}")))?;
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(signal) = status.signal() {
            return Ok((128 + signal) as u8);
        }
    }
    Ok(status.code().unwrap_or(1) as u8)
}
