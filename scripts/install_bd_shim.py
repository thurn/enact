"""Install tk as bd and retain the current Beads executable beside it."""

import argparse
import os
from pathlib import Path
import shutil
import tempfile


def publish(source: Path, destination: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def install(tk: Path, real: Path, entry: Path) -> None:
    suffix = ".exe" if os.name == "nt" else ""
    if entry.name.lower() != f"bd{suffix}":
        raise ValueError(f"the shim entry must be named bd{suffix}")
    if not tk.is_file() or not real.is_file():
        raise ValueError("both tk and the real Beads executable must exist")
    if tk.samefile(real):
        raise ValueError("tk and the real Beads executable must be different")
    backup = entry.with_name(f"bd-real{suffix}")
    entry.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists() or not real.samefile(backup):
        publish(real, backup)
    publish(tk, entry)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tk", type=Path, help="built tk executable")
    parser.add_argument("real", type=Path, help="real Beads executable, before any recorder or shim")
    parser.add_argument("entry", type=Path, help="bd entry on PATH to create or replace")
    args = parser.parse_args()
    try:
        install(args.tk, args.real, args.entry)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
