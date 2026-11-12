# Enact role: 🎖️ Marshal (retired)

Enact no longer schedules a Marshal sweep. Weeks of hourly sweeps never
had to recover, nudge or release anything, so the role was removed. This
file remains only so that scheduled tasks created before the removal shut
themselves down.

If a scheduled task or automation sent you here:

1. Title your session `🎖️ <project> sweep retired`.
2. Delete the scheduled task or automation that started this run, and only
   that one: the one whose saved prompt names this file and this project's
   absolute path. In Claude use the scheduled-task tools; in Codex use
   `automation_update(mode: "delete", id: <ID>)`. Never delete another
   project's task, and do nothing else: no session lookups, nudges,
   releases, archiving or CI.
3. Retitle `✅ <project> sweep retired`, or `⛔ <project> sweep retired`
   if you could not delete it. In that case tell the user which task to
   delete by hand.
