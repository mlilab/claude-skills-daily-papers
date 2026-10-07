---
name: daily-papers
description: >
  Build and manage the user's configured daily research-paper digest in daily-papers.
  Use for today's or a dated issue ("오늘 논문", "데일리 페이퍼", "새 논문 뭐 나왔어"),
  a named paper to summarize and add to the digest, bilingual summaries, research topics
  and priorities, the digest site, publishing, scheduling, or its Slack notification.
  Do not use for unrelated literature reviews.
---

# Daily Papers

Pipeline: **collect → select → fetch → summarize → render → publish → serve**. Scripts do the
deterministic parts; you do the judgment (relevance) and the writing (summaries).

Paths below are relative to this skill's directory (the folder containing this file); run
scripts by absolute path, because the shell's working directory can reset between commands.
Scripts need Python 3.9+ with `requests` and `pyyaml` (plus `Pillow` and `cryptography` for
publishing), and optionally poppler's `pdftotext`/`pdftoppm` (PDF fallback) and librsvg.
Use the Python interpreter reported by `setup.py status` for subsequent commands. Scheduled
runs use Claude Code through `autorun.py`; Codex can run this skill for interactive requests.

- Settings: `~/.config/daily-papers/config.yaml` (every field is explained in
  `config.example.yaml`). User data never goes into the skill folder.
- Output: `output_dir` in the config (default `~/daily-papers/`), one folder per issue date

## Start here: setup check

Run `python3 scripts/setup.py status` at the start of every request (it is cheap). It reports
whether the skill is configured, missing dependencies, the schedule, publishing and server state,
and `next_steps`.

- **Not configured** (first run after installing), or the user asks to set up / reconfigure:
  run `references/onboarding.md`. It collects interests and the optional choices for priority,
  schedule, Pages, private feedback, and Slack DM. It applies the config with `setup.py` and
  stores credentials through hidden terminal prompts. `README.md` is the user-facing setup and
  feature guide. Never include generated output or `.publish/` when distributing this skill.
- **Configured but `next_steps` is not empty** (e.g. the token or password is missing): mention
  it and offer to finish it, but still do what the user asked.

## Registering topics and priorities

When the user wants to add, change or remove a topic, edit the config → `topics`. Each topic
has a `name`, a `description` (what you judge relevance against, so make it specific:
subareas, methods, what is out of scope), `keywords` for the cheap first-pass filter (5–20
entries: synonyms, acronyms, method names). Plain keywords are whole words, case-insensitive,
hyphens = spaces; the last word also matches its plural and a glued version number (policy →
policies, ModelX → ModelX3) but nothing else, so list other inflections separately. `re:` prefix for
a regex, `title:` prefix to count a keyword only in titles (useful for model names that appear in
countless abstracts). Optional `exclude`, `authors` (+5; also makes a person's papers candidates
without keyword hits), `min_score`, `categories` (added to the shared arXiv query). Broad single
words such as "reasoning" or "agent" match hundreds of papers a day, so prefer phrases.

The config's `priority` lists authors and affiliations (display name → aliases) whose papers
are pinned to the top of their topic with a ★ badge. Edit it when the user asks to prioritize
or follow a lab, company or person.

Show the user the resulting entry. Don't run the pipeline unless they asked for it.

## Running an issue

### 1. Collect candidates

```bash
python3 scripts/fetch_candidates.py [--date YYYY-MM-DD]
```

An issue date `D` means *the arXiv listing that becomes public on D in the configured timezone*
(arXiv announces Sun–Thu 20:00 ET, i.e. 09:00 KST, or 10:00 KST in winter; there are no
listings on KST Saturday/Sunday), plus HF Daily Papers and OpenAlex for the matching days.
Without `--date` it picks the latest published issue. Relay any `notes` (e.g. "today's
listing is not out yet, using …") to the user. Exit code 2 means a source failed (see
`errors`; arXiv rate-limits bursts with HTTP 429): wait about 5 minutes and run it again. Only
if it keeps failing, continue with the sources you have and tell the user which one is missing.

It also reads the author/affiliation block of every candidate (arXiv HTML) to find the papers by
the config's `priority` authors and affiliations. It prints a JSON summary and writes
`<issue>/candidates.md`: topics, a **★ Priority candidates** list, then every candidate with id,
title, keyword hits, sources, HF upvotes and abstract. Papers from HF Daily with no keyword
hit are listed at the end ("no keyword hit") because HF picks are often relevant in ways
keywords miss.

### 2. Select (your judgment)

Read `candidates.md` completely. It is often 30–50k tokens, so page through it with the
available file-reading tools until the last candidate rather than judging from the first page. For
each candidate, decide whether a researcher who wrote
those topic descriptions would want to see it. The user asked for **all** related papers, so be
inclusive about genuinely related work (new methods, analyses, benchmarks, surveys in the
area) and strict about keyword collisions (e.g. "diffusion" in a physics paper about heat
diffusion, "reasoning" in a legal-NLP paper when the topic is LLM reasoning methods).

Write `<issue>/selected.json`:

```json
{"date": "YYYY-MM-DD", "papers": [
  {"id": "PAPER_ID_1", "topics": ["Example Topic"], "reason": "Relevant new method", "summarize": true},
  {"id": "PAPER_ID_2", "topics": ["Example Topic"], "reason": "Related evaluation", "summarize": false}
]}
```

- `topics` use the exact topic names from config, best fit first: the index page groups
  papers by their first topic. `reason` is one short English line.
- Within a topic, list papers in the order you'd want them read (most important first);
  priority papers are moved to the top automatically.
- **Priority papers first.** Select every paper in the ★ Priority candidates list that fits one of
  the topics; skip one only when it is a plain keyword collision (nothing to do with any topic).
  Within each topic, priority papers get the detailed-summary slots before all other papers.
- Summarize at most `max_papers_per_topic` (config, default 10) papers **per topic**, counting
  each paper under its first topic. `fetch_papers.py` assigns the slots in this order:
  1. priority papers (★);
  2. when the reader has liked at least `feedback.min_likes` papers: the papers most similar to
     their 👍 votes (`pref_score`, shown as ♥ in candidates.md; similarity to 👎 papers lowers it);
  3. the papers you marked `summarize: true`, then the rest, in your listed order.
  So mark the strongest non-priority fits (central to the topic, strong results, high HF
  upvotes) with `summarize: true`, list the rest with `summarize: false`; they appear under
  "Also relevant" so nothing related is dropped silently. Use the "Your recent votes" section of
  candidates.md to understand the reader's taste when you judge relevance.
  The index shows at most `max_also_relevant_per_topic` (default 30) "Also relevant" papers
  **per topic** (priority papers first), so still list every relevant paper in order of
  importance.
- If nothing is relevant, write an empty `papers` list, render, and tell the user.

Papers added to the selection that were not candidates get their affiliations checked by
`fetch_papers.py` too. `python3 scripts/probe_affiliations.py --date D` lists the priority papers
of the selection and whether each got a summary slot.

### Reader-requested papers (separate from the daily batch)

When the user asks for a detailed summary of a specific paper, complete this path without
waiting for the scheduled digest. The default issue date is the request date in the configured
timezone, including weekends; use `--date D` when the user names another date. Do not run a full
daily candidate fetch just to add this paper. This path needs Pages publishing and a configured
private feedback repo: `finish` writes the automatic 👍 before publishing.

1. For an arXiv ID or URL, run `python3 scripts/request_paper.py prepare --arxiv ID [--date D]`.
   For another paper, verify its metadata from the paper and write a JSON record with `id`,
   `title`, `abstract`, `authors`, and `links` (`abs` and a full-text `pdf` URL when available),
   then run `prepare --record /path/to/record.json [--date D]`. A supplied local PDF can use
   `local_pdf` in that record. Do not invent missing metadata.
2. `prepare` applies the configured topic keywords to the title, abstract and authors. It
   returns the matching topic names in score order, or `Others` when none match. It saves a
   separate request record, so a later automatic selection rewrite cannot drop the paper.
   It marks the paper for a detailed summary without using a per-topic summary slot.
3. Run `python3 scripts/fetch_papers.py --date D --ids ID`. **For every reader-requested paper,
   read the complete original PDF, even when the user supplies an arXiv `/abs/` URL or the fetcher
   selects HTML.** Follow the PDF procedure in `references/summary_guide.md`; `content.md` can
   truncate the appendix. If the PDF cannot be read in full, explain the obstacle and stop before
   `finish`. Then write bilingual `summary.json` using that guide and the vendored ASD-STE100
   skill. Check it with `ste_check.py` and `render.py --date D --check --ids ID`.
4. Run `python3 scripts/request_paper.py finish --date D --id ID`. This validates the summary,
   records a 👍 in the private feedback repo, renders the issue, and publishes the encrypted site.
   Verify the returned `publish.url/D/` is live and report the paper's topic and URL.

Only the reader-requested papers bypass the detailed-summary cap. The daily batch keeps its
usual limit and schedule. Repeating the same request updates one entry rather than adding a
duplicate.

### 3. Fetch full text and figures

```bash
python3 scripts/fetch_papers.py --date D
```

For each `summarize: true` paper this creates `<issue>/papers/<id>/` with `content.md`
(full text from arXiv HTML; PDF text if there is no HTML; else abstract only), `assets.json`
(figures/tables with captions), `figures/`, `tables/`, `meta.json`. Already-fetched papers are
skipped (`--force` to redo). Report papers whose source came back `abstract`.

### 4. Summarize

Summaries are the product, so give each paper real attention. When agent delegation is available,
split the papers into batches of about 3 and delegate batches in parallel. Otherwise work through
them sequentially. For 1–3 papers you may write them yourself, following the same rules.
Prompt template:

```
Write daily-digest summaries for these papers. First read, in full, both
<SKILL_DIR>/references/summary_guide.md and <SKILL_DIR>/vendor/asd-ste100/SKILL.md (required),
and follow them closely, including the language rules:
the English version follows ASD-STE100 (<SKILL_DIR>/vendor/asd-ste100/SKILL.md, STE-flavored
mode), and the Korean version is a translation of the English version that keeps technical
terms, math terms, jargon, proper nouns and fixed expressions in English.
Paper directories:
- <ISSUE_DIR>/papers/<id1>
- <ISSUE_DIR>/papers/<id2>
- <ISSUE_DIR>/papers/<id3>
For each paper: read meta.json, assets.json and content.md, open the 1–3 figure images you
will feature, write the English summary, run
python3 <SKILL_DIR>/scripts/ste_check.py <paper dir> and fix its hard violations, then translate
the English fields into the Korean fields, and save summary.json in that directory.
When done, run: python3 <SKILL_DIR>/scripts/render.py --date <D> --check --ids <id1>,<id2>,<id3>
and fix every reported problem. Reply with one line per paper: id | ok/problem | Korean title.
```

### Summary rules (always)

These rules hold for every detailed summary, whoever writes it:

1. **English version = ASD-STE100.** Write it in Simplified Technical English using the vendored
   skill `vendor/asd-ste100/` (from github.com/danyuchn/asd-ste100-skill, MIT) in STE-flavored
   mode: sentences of at most 25 words, active voice, simple tenses, no phrasal verbs, no
   semicolons, no nominalizations, no marketing adjectives, hedges kept as strong as the paper
   states them. Every summary writer must open and read `vendor/asd-ste100/SKILL.md` in full
   first; summary.json records it in `"ste"` (name and version from that file's frontmatter),
   and `scripts/ste_check.py` flags a summary without it. Check every summary with it.
2. **Korean version = translation of the English version.** Translate field by field. Same
   facts, numbers, order and hedges; nothing added or dropped.
3. **Titles: English original first.** On Korean pages too, the original English paper title is
   the main heading and the Korean translated title (`ko.title`) is the subtitle under it.
   The renderer does this; do not put the Korean title in place of the English one.
4. **Keep terms in English when translating.** Engineering terms, mathematical terms and
   notation, technical jargon, proper nouns (models, methods, datasets, benchmarks, libraries,
   organizations, people) and the field's fixed expressions stay in English. Do not try to
   translate everything into Korean. Translate the grammar and the ordinary words around them.

### 5. Render

```bash
python3 scripts/render.py --date D
```

This writes the daily index (`<issue>/ko.html`, `en.html`, plus an `index.html` that redirects
by language; one section per topic, priority papers first), per-paper pages
(`papers/<id>/{ko,en}.html`) and the archive (`<output_dir>/index.html`). Its JSON output lists `problems` per paper. Fix them (edit the
JSON or re-dispatch that paper) and render again. `--all` re-renders every issue, e.g. after a
template change.

### 6. Publish (GitHub Pages)

```bash
python3 scripts/publish.py push
```

If `publish.enabled` in the config, this pushes a password-protected copy to the user's GitHub
Pages site (`publish.url`, e.g. `https://<user>.github.io/papers/`): every page and figure is
encrypted, paper folders get opaque names, and only the last `publish.figure_days` days of
figures are included. It is a no-op when nothing changed. Automatic runs publish by themselves
(autorun.py), so skip this step when the prompt starts with `[daily-papers:auto]`. To change the
password: the user runs `publish.py set-password`, then `publish.py push`. Store tokens only via
the hidden terminal prompts in protected local files; never place a raw password or token in
source code, config, commits, or chat summaries.

### 7. Serve and report

`setup.py server` installs an always-on local web server (systemd user service or launchd
agent) for `output_dir`. Check with `python3 scripts/serve.py --status`; if nothing is running,
run `setup.py server` to start the managed service. Then report to the
user, in their language:
- issue date, how many candidates → selected → summarized (and any "also relevant")
- 3–5 highlights (priority papers first): Korean title plus one line each
- where to open it: the published URL (if publish ran), `http://localhost:<port>/<D>/` and the
  local file path `<output_dir>/<D>/ko.html`. If host is `127.0.0.1` and this is a remote
  machine, mention `ssh -L <port>:localhost:<port> <server>` or `serve.host: 0.0.0.0`.
- any papers that failed or were summarized from the abstract only

## Other requests

- **A past date / several days**: run the pipeline per issue date (`--date`). Weekend KST
  dates have no listing; use the Friday or Monday issue.
- **Just re-render or open**: `render.py --date D` / `serve.py`.
- **Change the schedule**: edit `schedule` in the config (`days`: daily / weekdays / list of
  days, `times`: quoted "HH:MM" in KST, `enabled`), then `setup.py schedule`. Show the `next_runs` it
  prints. Times before ~10:30 KST cover the previous morning's arXiv listing.
- **Turn publishing on later or change the site**: set `publish` in the config, then follow the
  publishing steps in `references/onboarding.md` (token → password → `setup.py github`).
- **👍/👎 feedback** (`feedback` in config): detailed summaries on the published site carry 👍/👎
  buttons. Votes are files in the private repo `feedback.repo`, written by the browser with a
  fine-grained token that exists only in the reader's browser (encrypted with the site key).
  `fetch_candidates.py` syncs them (`scripts/feedback.py`) into `<output_dir>/.feedback/` and
  scores candidates. Never ask for, print or store the reader's browser token; the server reads
  votes with its own token. `publish.py` refuses to publish if a token, this machine's hostname,
  home path or a local server address appears in any published file.
- **Slack DM** (`notify.slack`): after every scheduled run, `autorun.py` sends one DM for the target date,
  through a Slack app bot (`scripts/notify.py`; token and member ID only in `<output_dir>/.publish/`,
  mode 600). The message has normal/error status, per-topic candidate/detailed/also-relevant counts,
  and the public site link, and passes the same leak check as publishing. Setup instructions are in
  `references/automation.md`. `notify.py test` sends a test DM; `notify.py send --date D` resends one.
- **Scheduled runs**: `scripts/autorun.py` (started by the scheduler) builds the latest published
  issue if it isn't complete, catches up missed issues, then publishes. Logs are in
  `<output_dir>/.autorun.log`. To debug, see `references/automation.md`.
- **Automatic runs** (the prompt starts with `[daily-papers:auto]`): nobody is watching, so
  never ask questions; make every decision yourself and finish all steps through render.
