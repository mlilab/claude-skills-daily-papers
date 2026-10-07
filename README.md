# Daily Papers

Daily Papers is a Claude Code and Codex skill that finds new research papers in your areas of interest and builds a bilingual (English and Korean) digest. It collects candidates from arXiv, Hugging Face Daily Papers, and OpenAlex, then creates dated HTML pages with detailed summaries and selected figures and tables.

## Features

| Feature | What it does | Setup |
| --- | --- | --- |
| Daily digest | Selects papers by topic and separates detailed summaries from other relevant papers | Research topics |
| Scheduled runs | Runs on chosen days and times, and catches up on missed issues | Schedule and Claude Code CLI |
| Priority papers | Marks relevant papers by selected authors or institutions with ★ and moves them up the list | Authors and institutions (optional) |
| Local site | Serves English and Korean issue pages and a dated archive | Local server (optional) |
| GitHub Pages | Publishes a password-protected, encrypted copy at `https://<username>.github.io/<repository>/` | GitHub repository and token, site password (optional) |
| 👍/👎 feedback | Stores votes in a private repository and uses likes to rank future papers | Pages and a private feedback repository (optional) |
| Requested papers | Adds a paper supplied by the reader as a detailed summary on the request date, outside the per-topic limit, and automatically gives it a 👍 vote | Pages and feedback |
| Slack DM | Sends the scheduled run's success or error status, per-topic counts, and dated URL to one configured recipient | Schedule, Slack app, bot token, recipient member ID (optional) |

A requested paper is classified with your topic keywords. It goes under `Others` if no topic matches. Requests can be added on weekends, even when there is no new arXiv listing.

## Requirements and installation

- **Python 3.9+** and the packages in `requirements.txt`: `requests`, `pyyaml`, `Pillow`, and `cryptography`.
- **Git** for GitHub Pages publishing. Poppler (`pdftotext` and `pdftoppm`) improves PDF text and figure extraction; librsvg helps preview SVG figures.
- **Claude Code CLI**, installed and signed in, if you want scheduled runs. The scheduler currently starts Claude Code. You can use the skill interactively in Codex without enabling the scheduler.

The repository root contains `SKILL.md`. Clone it into the skill directory for the tool you use. For Codex:

```bash
mkdir -p "$HOME/.codex/skills"
git clone https://github.com/mlilab/claude-skills-daily-papers.git "$HOME/.codex/skills/daily-papers"
```

To use the same installation in Claude Code too:

```bash
mkdir -p "$HOME/.claude/skills"
ln -s "$HOME/.codex/skills/daily-papers" "$HOME/.claude/skills/daily-papers"
```

If you use only Claude Code, clone directly into `~/.claude/skills/daily-papers` instead. In either case, check that `SKILL.md` is directly inside the installed folder. Keep your configuration and generated issues outside this repository.

Install dependencies in a separate virtual environment, then check the setup. Change `SKILL_DIR` to the Claude Code path if that is where you installed the skill.

```bash
SKILL_DIR="$HOME/.codex/skills/daily-papers"
DP_PY="$HOME/.local/share/daily-papers/venv/bin/python"
python3 -m venv "$HOME/.local/share/daily-papers/venv"
"$DP_PY" -m pip install -r "$SKILL_DIR/requirements.txt"
"$DP_PY" "$SKILL_DIR/scripts/setup.py" status
```

Use the same virtual environment's Python for later terminal commands. The schedule records the Python path used during setup. The commands below assume `SKILL_DIR` and `DP_PY` are still set; set them again if you open a new terminal.

## First-time setup

Ask Claude Code or Codex: **“Set up Daily Papers for me.”** The skill checks the environment with `setup.py status`, helps you define topics, previews the configuration, and applies the features you choose. Tell the agent which of these you want:

1. **Required — research topics:** Describe each area and its boundaries. For example, “graph learning methods and evaluation, excluding molecular property prediction.” The agent proposes keywords and arXiv categories.
2. **Optional — priority authors and institutions:** Relevant papers from these sources appear first. Priority alone does not collect papers outside your topics.
3. **Optional — schedule:** For example, daily at 10:30 KST or weekdays at 10:30 KST. A run before the morning arXiv listing is published covers the previous issue date.
4. **Optional — GitHub Pages:** Give your GitHub username and the repository name to use as the site path (default: `papers`). Set the site password later in a hidden terminal prompt.
5. **Optional — feedback and requested-paper voting:** Enable Pages and a private feedback repository to use 👍/👎 and the automatic 👍 for requested papers.
6. **Optional — Slack DM:** Install a Slack bot to receive scheduled-run results. Enter the bot token and your member ID later in hidden terminal prompts.

Your settings live in `~/.config/daily-papers/config.yaml`; generated pages and data go to `~/daily-papers/` by default. See [config.example.yaml](config.example.yaml) for every setting. After setup, run `"$DP_PY" "$SKILL_DIR/scripts/setup.py" status` to see missing steps and the next scheduled runs. To serve the local site, run `"$DP_PY" "$SKILL_DIR/scripts/setup.py" server`.

### GitHub Pages

1. Create an empty **public** GitHub repository (default name: `papers`). If you also want feedback, create an empty **private** repository such as `papers-feedback`.
2. Create a server-side GitHub fine-grained personal access token. Select only the repositories this installation will use. The Pages repository needs **Contents, Pages, and Administration: read and write**. If feedback is enabled, the server token must also have Contents write access to the private feedback repository. A fine-grained token's selected permissions apply to all selected repositories; if you need different permissions for each, you can configure a separate protected file through `feedback.token_file`. See [GitHub's token guide](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) and [Pages API permissions](https://docs.github.com/en/rest/pages/pages).
3. In your own terminal, run `"$DP_PY" "$SKILL_DIR/scripts/setup.py" token` to store the server token through a hidden prompt. Run `"$DP_PY" "$SKILL_DIR/scripts/publish.py" set-password` to set a site password of **at least 12 characters**.
4. Run `"$DP_PY" "$SKILL_DIR/scripts/setup.py" github`. This makes the first encrypted push and configures Pages. Open the resulting URL and confirm that the password unlocks it.

The Pages repository is public, but published pages and figures are encrypted and paper directory names are obscured. Never put a token or password in chat, the config file, or a repository. A classic token with only `public_repo` can create a public repository but may not have enough permission for the Pages API; use the fine-grained permissions above for automatic Pages setup.

### 👍/👎 feedback

- Confirm that `config.yaml` has `feedback.enabled: true` and `feedback.repo: <username>/<private-repository>`. The server token needs Contents write access to that repository.
- Run `"$DP_PY" "$SKILL_DIR/scripts/feedback.py" sync` to check server access. Its result should not contain an `error` field.
- On each browser, enter a **separate** fine-grained token limited to that private repository with Contents read and write permission in the site's token box when you first vote. The site encrypts it with the site key and stores it in that browser. Voting is available on the password-unlocked Pages site.
- The server records a requested paper's automatic 👍 without waiting for a browser token. Once you have at least `feedback.min_likes` likes (default: 3), similar papers get priority for remaining detailed-summary slots in later daily issues.

### Slack DM

1. Create an app at [Slack's app management page](https://api.slack.com/apps). Under **OAuth & Permissions → Bot Token Scopes**, add `chat:write`, `im:write`, and `users:read`, then install the app to your workspace. The feature does not read email, so `users:read.email` is unnecessary. See the [Slack `users.info` permissions](https://docs.slack.dev/reference/methods/users.info/).
2. In your terminal, run `"$DP_PY" "$SKILL_DIR/scripts/notify.py" token` and enter the **Bot User OAuth Token** in the hidden prompt.
3. Copy **your own member ID** from your Slack profile and enter it with `"$DP_PY" "$SKILL_DIR/scripts/notify.py" member`. The command checks that it belongs to an active human account and stores exactly one recipient.
4. Confirm `notify.slack.enabled: true` in `config.yaml`. Run `"$DP_PY" "$SKILL_DIR/scripts/notify.py" test` and check that the test DM reaches **only you**. To inspect a report without sending it, use `"$DP_PY" "$SKILL_DIR/scripts/notify.py" preview --date YYYY-MM-DD`.

The bot token and recipient ID are stored in `<output_dir>/.publish/` with file mode 600. Do not paste either into chat or GitHub. Automatic DMs require scheduled runs to be enabled.

## Example requests

| Ask the agent | Result |
| --- | --- |
| “Build today's paper digest.” | Builds the latest published issue |
| “Show the October 2 paper digest.” | Opens or builds a dated daily issue |
| “Summarize this paper: https://arxiv.org/abs/…” | Adds a detailed summary on the request date, records 👍, and publishes it |
| “Summarize this PDF paper.” | Verifies its metadata, then adds it to the request date |
| “Change the keywords for my graph learning topic.” | Updates topic settings |
| “Run the digest every day at 10:30 KST.” | Updates the schedule |
| “Send a Slack notification test.” | Sends a test DM to the configured recipient |

A daily issue date is the date when a new arXiv listing becomes available in your configured time zone. A requested paper uses the **request date** by default; you can specify another date. Requested papers do not consume the daily per-topic detailed-summary limit.
