# Onboarding: first run and reconfiguration

This is the agent's setup procedure. Talk in the user's language. Ask for preferences once, use defaults for unspecified optional features, show the resolved setup, and apply the choices already authorized by the user. Never ask for tokens, passwords, or member IDs in chat. For the technical command sequence and user-facing explanation, use [README.md](../README.md).

## 1. Inspect the machine

Run `python3 <skill_dir>/scripts/setup.py status` with the skill's absolute path. Use the reported `skill_dir` and `python` for later commands.

- `requests` and `pyyaml` are required; `Pillow` and `cryptography` are needed for publishing. Install missing packages from `requirements.txt` using the same Python interpreter and rerun status.
- Scheduled builds require the authenticated `claude` CLI. Codex registration alone does not change `autorun.py`'s Claude runner.
- Missing poppler or librsvg reduces PDF/figure extraction quality but does not block setup.

## 2. Collect the choices

If the user already supplied a choice, keep it. Ask for the remaining choices together. Do not ask for credential values.

```text
Daily Papers를 설정할게요. 다음을 알려주세요. 선택 항목은 건너뛰어도 됩니다.

1. 관심 연구 주제와 포함·제외 범위 (필수)
2. 우선순위 기관·저자 (선택)
3. 자동 실행: 예) 매일 10:30 KST / 평일 10:30 / 사용 안 함 (선택)
4. GitHub Pages 게시: GitHub 아이디와 사이트 경로(기본 papers), 또는 사용 안 함 (선택)
5. 👍/👎와 특정 논문 요청 시 자동 좋아요: 사용 여부 (Pages가 필요, 선택)
6. 예약 실행 결과를 본인에게 보내는 Slack DM: 사용 여부 (자동 실행이 필요, 선택)
```

The site password is set later in a hidden terminal prompt. The Slack member ID is also entered there. Default `schedule.enabled`, `publish.enabled`, `feedback.enabled`, and `notify.slack.enabled` to false when the user does not select them. Manual paper summaries that must publish and receive automatic 👍 require both `publish` and `feedback`.

## 3. Design the config

**Topics.** For each interest set `name`, a 2–5 sentence `description` with included and excluded work, and 8–20 useful `keywords` (acronyms, phrases, methods, benchmarks). Avoid broad single words. Plain keywords use whole words without case sensitivity; hyphens match spaces; the last word matches plurals and attached version numbers. Use `title:` for title-only matches or `re:` for variants. Add `exclude`, topic `authors`, `min_score`, or `categories` when useful. Choose arXiv categories that cover the user's areas without making the fetch needlessly broad. `init --dry-run` checks keyword syntax.

**Priority.** `priority.affiliations` maps display name to precise aliases. `priority.authors` uses full names. Priority moves already relevant papers to the top. To collect an author's work without keyword hits, also add the name to the appropriate topic's `authors`.

**Schedule.** `days` is `daily`, `weekdays`, or a weekday list; `times` are quoted `HH:MM` strings in `Asia/Seoul`. Recommend 10:30 KST or later so the morning arXiv listing has appeared. If the user chooses an earlier time, explain that the run uses the previous published issue. If schedule is disabled, there is no automatic Slack DM.

**Pages.** Set `publish.enabled`, `github_user`, and `repo` only if requested. The public Pages repo contains encrypted pages and opaque paper folders. For a smooth first setup, have the user create the empty public repo before creating a fine-grained server token. A token selected for that repo needs Contents, Pages, and Administration read/write. The `setup.py github` command can create the public repo when its token permits it, but a classic token with only `public_repo` can fail at the Pages API step; do not promise full automation with that scope. See [GitHub Pages API permissions](https://docs.github.com/en/rest/pages/pages).

**Feedback.** Set `feedback.enabled: true`, `feedback.repo: <owner>/<private-repo>`, and optionally `min_likes` when selected. It requires Pages. Create the private repo before token setup. The server token must read and write its Contents because scheduled vote sync and reader-requested auto 👍 run on the server. A single fine-grained server token can select both the Pages and feedback repos, with sufficient permissions; GitHub applies its chosen permissions to each selected repo. If separate tokens are desired, configure `feedback.token_file` to a protected local file. The browser gets a different fine-grained token limited to the private feedback repo, Contents read/write. It is entered on the encrypted site once per browser, not in chat.

**Slack.** Set `notify.slack.enabled: true` only when selected. The bot needs `chat:write`, `im:write`, and `users:read`: the last scope is used by `notify.py member` to validate the recipient. Email access is not used, so `users:read.email` is unnecessary. See [Slack `users.info`](https://docs.slack.dev/reference/methods/users.info/). Only one member ID is stored; the bot opens a DM only to that ID.

## 4. Preview and apply

Write the answers JSON to a temporary file outside the skill folder. The schema is in `scripts/setup.py`'s docstring; it accepts `feedback` and `notify.slack` as well as topics, priority, schedule, and publish. Run `setup.py init --answers <file> --dry-run`. Review the resolved `config` and `next_runs`, then show the user a compact summary: topic names/keywords, priority, next run, public URL, feedback and Slack choices. If the user asked for setup and choices are clear, run `setup.py init --answers <file>` without another approval cycle. For an existing config being replaced, use `--force`; it backs up the old file and preserves existing feedback/Slack choices when omitted from answers.

Run `setup.py schedule` if enabled. Report `next_runs` and any warning about Linux user lingering. Run `setup.py server` if the user wants the local site served automatically; this is independent of Pages.

For Pages, carry out the steps in this order:

1. Confirm that the needed GitHub repos exist (public Pages repo, and private feedback repo when selected).
2. In the user's terminal, run `setup.py token` (hidden prompt) for the server GitHub token, then `publish.py set-password` (hidden prompt, 12+ characters). Never put credentials in a command argument, output, answer file, config, chat, or repository.
3. Run `setup.py github`, which makes the first encrypted push and enables Pages. If API setup fails due to permissions, use the manual Pages settings instruction it returns and rerun. Verify the URL opens with the site password.
4. If feedback is enabled, confirm `feedback.repo` names the private repo and run `feedback.py sync` to check server read access (`error` must be absent). The server also needs Contents write for automatic 👍. Explain the separate browser token setup from README. Manual paper requests cannot complete their automatic 👍 without working server access.

For Slack, create/install the app with the three bot scopes above. In the user's terminal run `notify.py token`, then `notify.py member`; both use hidden prompts and keep values in `<output_dir>/.publish/` with mode 600. Run `notify.py test` and verify that the configured user receives the test DM. Do not send a test to any other member.

Finish with `setup.py status`. Report only nonsecret settings: config path, topics, next run, local URL if served, Pages URL if enabled, feedback state, and Slack test result. Offer to make the first digest if the user wants one now.

## Reconfiguration

For one topic, priority, or schedule change, edit `config.yaml` directly and rerun `setup.py schedule` when schedule changes. For a complete restart, repeat onboarding with `setup.py init --force`. Include explicit `feedback.enabled: false` or `notify.slack.enabled: false` in answers when the user wants to turn either feature off; omitted values retain the existing choice. Keep credential files and generated issues outside the distributable skill folder.
