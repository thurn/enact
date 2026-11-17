mod args;
mod backup;
mod bench;
mod client;
mod commands;
mod feed;
mod guidance;
mod human;
mod output_ids;
mod register;

use args::{Command, EdgeOperation, Options};
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
            if error.use_stderr() && std::env::args_os().any(|arg| arg == "--json") {
                let body = json!({"error": error.to_string(), "code": code});
                let _ = writeln!(io::stdout().lock(), "{body}");
            }
            let _ = error.print();
            return ExitCode::from(code);
        }
    };
    let json_output = options.json;
    match run(options) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            if json_output {
                let mut body = error
                    .body
                    .filter(Value::is_object)
                    .unwrap_or_else(|| json!({"error": error.message, "code": error.code}));
                output_ids::display(&mut body);
                let _ = writeln!(io::stdout().lock(), "{body}");
            }
            eprintln!("tk: {}", error.message);
            ExitCode::from(error.code)
        }
    }
}

fn run(options: Options) -> Result<()> {
    let detailed = matches!(&options.command, Command::Show { .. });
    let silent_create = matches!(&options.command, Command::Create(args) if args.silent);
    let dep_cycles = matches!(
        &options.command,
        Command::Dep {
            operation: EdgeOperation::Cycles
        }
    );
    let mut value = match options.command {
        Command::Prime => {
            if !options.json {
                return write!(io::stdout().lock(), "{}", guidance::PRIME).map_err(usage);
            }
            json!({"guide": guidance::PRIME})
        }
        Command::Version => {
            let version = env!("CARGO_PKG_VERSION");
            if !options.json {
                return writeln!(io::stdout().lock(), "{version}").map_err(usage);
            }
            json!({"version": version})
        }
        Command::Bench { samples } => bench::run(&options, samples)?,
        Command::Register { ensure } => {
            let machine = register::run(ensure)?;
            if !options.json {
                return writeln!(io::stdout().lock(), "Registered write token for {machine}")
                    .map_err(usage);
            }
            json!({"machine": machine})
        }
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
    if dep_cycles {
        output_ids::display_cycles(&mut value);
    } else {
        output_ids::display(&mut value);
    }
    if options.brief {
        output_ids::brief(&mut value);
    }
    if options.json {
        writeln!(output, "{value}").map_err(usage)
    } else if silent_create {
        let id = value["results"][0]["id"]
            .as_str()
            .ok_or_else(|| usage("create response has no ticket ID"))?;
        writeln!(output, "{id}").map_err(usage)
    } else if detailed {
        human::details(&mut output, &value).map_err(usage)
    } else if dep_cycles {
        human_cycles(&mut output, &value).map_err(usage)
    } else {
        human(&mut output, &value).map_err(usage)
    }
}

fn human_cycles(output: &mut impl Write, value: &Value) -> io::Result<()> {
    let cycles = value.as_array().expect("dependency cycles array");
    if cycles.is_empty() {
        return writeln!(output, "No dependency cycles detected.");
    }
    for cycle in cycles {
        let mut ids: Vec<_> = cycle
            .as_array()
            .expect("cycle members array")
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect();
        if let Some(first) = ids.first() {
            ids.push(first.clone());
            writeln!(output, "{}", ids.join(" → "))?;
        }
    }
    Ok(())
}

fn human(output: &mut impl Write, value: &Value) -> io::Result<()> {
    if let Some(tickets) = value["tickets"].as_array() {
        for ticket in tickets {
            if let Some(id) = ticket.as_str() {
                writeln!(output, "{id}")?;
            } else {
                human(output, ticket)?;
            }
        }
        return Ok(());
    }
    if let Some(items) = value.as_array() {
        for item in items {
            human(output, item)?;
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
    if let (Some(author), Some(text)) = (value["author"].as_str(), value["text"].as_str()) {
        return writeln!(output, "{author}: {text}");
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
