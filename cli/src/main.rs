use std::{env, process::ExitCode};

fn main() -> ExitCode {
    let mut arguments = env::args_os().skip(1);
    if arguments
        .next()
        .is_some_and(|argument| argument == "version")
        && arguments.next().is_none()
    {
        println!("{}", env!("CARGO_PKG_VERSION"));
        return ExitCode::SUCCESS;
    }

    eprintln!("Usage: tk version");
    ExitCode::FAILURE
}
