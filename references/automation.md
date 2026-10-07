# Scheduled runs

The pipeline needs judgment (selection) and writing (summaries), so scheduled runs start Claude
Code headlessly through `scripts/autorun.py`. `setup.py schedule` installs the trigger from the
config's `schedule` (days + times in KST):

| platform | trigger | files |
|---|---|---|
| Linux with systemd | `daily-papers.timer` → `daily-papers.service` (oneshot), times pinned to `Asia/Seoul` | real files in `<config dir>/systemd/`, symlinked into `~/.config/systemd/user/` |
| macOS | launchd agent `com.dailypapers.autorun` (local time, converted from KST) | `~/Library/LaunchAgents/` |
| other | crontab lines tagged `# daily-papers` (local time, converted from KST) | `crontab -l` |

The systemd service allows up to 20 hours for a run that is catching up multiple issues;
each Claude build has its own 150-minute timeout, and the publish step has a 30-minute timeout.

`setup.py server` adds an always-on web server the same way (`daily-papers-web.service` or
`com.dailypapers.web`). On Linux, timers only fire while the user is logged in unless lingering
is enabled (`loginctl enable-linger $USER`); `setup.py schedule` warns when it is off. With
launchd/cron, rerun `setup.py schedule` after a daylight-saving change on this machine.

The local web server only serves the rendered site (HTML, CSS/JS, images). Hidden files such as
`.publish/` (GitHub token, site key), working data (candidates, selections, full texts, PDFs,
summary JSON) and directory listings return 404.

## What autorun.py does

1. Resolves the latest **published** issue (arXiv's listing goes public at 09:00 KST, 10:00 in
   winter; runs before that cover the previous morning's listing).
2. Skips if `<issue>/.rendered.json` says the issue is complete (every selected paper
   summarized, no validation problems, no source errors).
3. Catches up issues missed since the newest complete one (machine off, failed run, arXiv down,
   or a schedule less frequent than daily), oldest first, at most 6, and never before the day
   the schedule was installed.
4. For each issue to build, runs
   `claude -p "[daily-papers:auto] … {date} …" --permission-mode acceptEdits --allowedTools …`
   from `$HOME` with a 150-minute timeout (claude path recorded in
   `~/.config/daily-papers/state.json` by `setup.py schedule`).
5. Runs `publish.py push` when publishing is enabled (a no-op if nothing changed; it also drops
   figures older than `publish.figure_days`).
6. If Slack notification is enabled, sends one DM for the target date, including normal/error status,
   per-topic candidate/detailed/also-relevant counts, and the Pages URL. Build and publish
   failures still produce a DM with the counts available at that point. A run that was already
   complete also reports its result. A notification failure is logged and makes the service fail.

Logs:
- `<output_dir>/.autorun.log`: one JSON line per decision/result
- `<output_dir>/.autorun-claude.log`: Claude's final message per run
- systemd: `journalctl --user -u daily-papers.service`

## Slack bot setup

Create a Slack app in your workspace at https://api.slack.com/apps, add the `chat:write`,
`im:write`, and `users:read` bot token scopes under **OAuth & Permissions**, then install the app
to the workspace. `users:read` lets `notify.py member` validate the one recipient with
`users.info`; `users:read.email` is not needed because the code does not inspect email.
Copy its **Bot User OAuth Token** (`xoxb-...`). Run `python3 scripts/notify.py token` in an
interactive terminal; the token prompt is hidden, and a successful `auth.test` stores it in
`<output_dir>/.publish/slack-token` with mode 600. Do not paste the token into chat or config.
Copy your own member ID from your Slack profile (**More → Copy member ID**) and run
`python3 scripts/notify.py member` in an interactive terminal. It validates that the ID belongs
to an active human in the bot's workspace and saves it only in
`<output_dir>/.publish/slack-member-id` with mode 600. Set `notify.slack.enabled: true`. Run
`python3 scripts/notify.py test` to confirm the DM. `notify.py preview --date YYYY-MM-DD`
prints a report without sending it, and `send --date` resends a completed issue report.

## Common operations

```bash
python3 scripts/setup.py status                  # schedule, next runs, what is missing
python3 scripts/autorun.py --dry-run             # what would the next run do?
python3 scripts/autorun.py --date YYYY-MM-DD --force   # rebuild one issue now
systemctl --user start --no-block daily-papers.service # (Linux) run the scheduled job now
```

If a run stalls on a permission, look at `.autorun-claude.log` and extend `ALLOWED` in
`autorun.py` rather than skipping permissions.
