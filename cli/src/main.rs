mod args;
mod bench;
mod client;
mod commands;
mod feed;
mod migration;

use args::{Command, Options};
use clap::Parser;
use client::{Client, Result, usage};
use serde_json::{Value, json};
use std::io::{self, Write};
use std::process::ExitCode;
use std::time::Duration;

fn main() -> ExitCode {
    let options = match Options::try_parse() {
        Ok(options) => options,
        Err(error) => {
            let code = u8::from(error.use_stderr());
            let _ = error.print();
            return ExitCode::from(code);
        }
    };
    let json_output = options.json;
    match run(options) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            if json_output && let Some(body) = error.body {
                let _ = writeln!(io::stdout().lock(), "{body}");
            }
            eprintln!("tk: {}", error.message);
            ExitCode::from(error.code)
        }
    }
}

fn run(options: Options) -> Result<()> {
    let value = match options.command {
        Command::Version => {
            let version = env!("CARGO_PKG_VERSION");
            if !options.json {
                return writeln!(io::stdout().lock(), "{version}").map_err(usage);
            }
            json!({"version": version})
        }
        Command::Bench { samples } => bench::run(&options, samples)?,
        command => {
            let client = Client::load(
                options.project.as_deref(),
                options.actor,
                Duration::from_secs_f64(options.retry_for),
            )?;
            if matches!(command, Command::Export) {
                return commands::export(&client);
            }
            commands::run(&client, command)?
        }
    };
    let mut output = io::stdout().lock();
    if options.json {
        writeln!(output, "{value}").map_err(usage)
    } else {
        human(&mut output, &value).map_err(usage)
    }
}

fn human(output: &mut impl Write, value: &Value) -> io::Result<()> {
    if let Some(tickets) = value["tickets"].as_array().or_else(|| value.as_array()) {
        for ticket in tickets {
            human(output, ticket)?;
        }
        return Ok(());
    }
    if let (Some(id), Some(title)) = (value["id"].as_str(), value["title"].as_str()) {
        return writeln!(
            output,
            "{id}  {}  {title}",
            value["status"].as_str().unwrap_or("")
        );
    }
    if let Some(results) = value["results"].as_array() {
        for result in results {
            match result["id"].as_str() {
                Some(id) => writeln!(output, "{id}")?,
                None => writeln!(output, "{result}")?,
            }
        }
        return Ok(());
    }
    if let (Some(read), Some(write)) = (
        value["read_median_ms"].as_f64(),
        value["write_median_ms"].as_f64(),
    ) {
        return writeln!(
            output,
            "Median read {read:.2} ms, write {write:.2} ms ({} samples)",
            value["samples"]
        );
    }
    if let (Some(sequence), Some(cutover)) = (value["seq"].as_u64(), value["cutover"].as_str()) {
        return writeln!(output, "{cutover} (sequence {sequence})");
    }
    writeln!(output, "{value}")
}
