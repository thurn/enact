use crate::args::Options;
use crate::client::{Failure, Result, usage};
use serde_json::{Value, json};
use std::env;
use std::process::{Command, Stdio};
use std::time::Instant;

pub fn run(options: &Options, count: u32) -> Result<Value> {
    let executable = env::current_exe().map_err(usage)?;
    let mut base = vec![
        "--json".to_owned(),
        "--actor".to_owned(),
        options.actor.clone(),
        "--retry-for".to_owned(),
        options.retry_for.to_string(),
    ];
    if let Some(project) = &options.project {
        base.extend(["--project".to_owned(), project.clone()]);
    }
    let run = |args: &[&str]| {
        let output = Command::new(&executable)
            .args(&base)
            .args(args)
            .stdin(Stdio::null())
            .output()
            .map_err(usage)?;
        if output.status.success() {
            Ok(output.stdout)
        } else {
            let code = output
                .status
                .code()
                .and_then(|code| u8::try_from(code).ok())
                .unwrap_or(1);
            Err(Failure::new(
                code,
                String::from_utf8_lossy(&output.stderr).trim(),
            ))
        }
    };

    let created: Value =
        serde_json::from_slice(&run(&["create", "--title", "tk benchmark"])?).map_err(usage)?;
    let id = created["results"][0]["id"]
        .as_str()
        .ok_or_else(|| usage("create returned no ticket ID"))?;
    let timings = (|| {
        let mut reads = Vec::new();
        let mut writes = Vec::new();
        for sample in 0..count {
            let start = Instant::now();
            run(&["show", id])?;
            reads.push(start.elapsed().as_secs_f64() * 1000.0);

            let notes = format!("sample {sample}");
            let start = Instant::now();
            run(&["update", id, "--notes", &notes])?;
            writes.push(start.elapsed().as_secs_f64() * 1000.0);
        }
        let median = |mut times: Vec<f64>| {
            times.sort_by(f64::total_cmp);
            (times[(times.len() - 1) / 2] + times[times.len() / 2]) / 2.0
        };
        Ok(
            json!({"samples": count, "read_median_ms": median(reads), "write_median_ms": median(writes)}),
        )
    })();
    let cleanup = run(&["close", id, "--reason", "tk benchmark finished"]);
    let result = timings?;
    cleanup?;
    Ok(result)
}
