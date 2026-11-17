---
name: enact-setup
description: Install Enact and connect the current project to its ticket service.
---

1. Check that Git, Python 3.11+, and Cargo are available.
2. Run `python3 scripts/enact.py install` from the Enact checkout to link the
   skills and install the managed `tk` executable.
3. Configure `[tickets].project` and `[tickets].url` in the project's main
   `.enact.toml`. Use the Worker root URL. For an existing project, retain its
   check commands, branch names, and worktree settings.
4. Run `enact setup` from the project to register this machine's write token.
5. Run `enact backend` for the executable path, then `tk list --json` from the
   project to verify the connection. Linked checkouts inherit client enrollment
   from the main checkout.
