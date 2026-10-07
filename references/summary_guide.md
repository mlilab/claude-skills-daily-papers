# Writing a paper summary (`summary.json`)

You are summarizing one paper for a researcher's daily digest. They read the Korean or the
English page over coffee and decide whether to open the PDF, so the summary has to be accurate,
concrete and fast to skim, and it should explain the idea well enough that they could describe
it to a labmate.

## Inputs (in the paper directory)

- `meta.json`: title, authors, links, `selected_topics`, `selection_reason`
- `content.md`: full text (or PDF text / abstract only; see its `source of this text` line).
  Figures and tables appear inline as `[Figure 3 — asset `S3.F3`] caption…`
- `assets.json`: every figure/table with `id`, `label`, `caption`, `section`, `files` / `table_file`
  (and `previews`: PNG renderings of SVG files)

Read `content.md` all the way through, paging with an available file reader when it is longer
than one call. Concentrate on the intro, method and main experiments; skim the appendix. For the
1–3 figures you plan to feature, open the PNG/JPG files with an image-capable tool so your explanation
describes what the plot actually shows (axes, trends, which curve is theirs), not just the
caption. If the image tool cannot display `.svg` files, `assets.json` lists a
PNG rendering under `previews`; open that instead. Labels drawn inside `<foreignObject>` may
be missing from a preview; if axis labels or legends are blank, grep the SVG source for them.

## Output: `<paper dir>/summary.json`

```json
{
  "id": "PAPER_ID",
  "ste": "<name> <version> from vendor/asd-ste100/SKILL.md frontmatter",
  "topics": ["Example Topic"],
  "tags": ["method", "evaluation", "analysis"],
  "figures": [
    {"asset": "S1.F1", "place": "method",
     "en": "Describe the method shown in this figure.",
     "ko": "이 그림에 나온 방법을 설명한다."},
    {"asset": "S2.T1", "place": "results",
     "en": "Compare the reported result with its baseline.",
     "ko": "보고된 결과를 baseline과 비교한다."}
  ],
  "en": {
    "tldr": "…",
    "key_points": ["…", "…", "…"],
    "problem": "…",
    "method": "…",
    "results": "…",
    "why_it_matters": "…",
    "limitations": "…",
    "relevance": "…"
  },
  "ko": {
    "title": "한국어 제목",
    "tldr": "…",
    "key_points": ["…"],
    "problem": "…",
    "method": "…",
    "results": "…",
    "why_it_matters": "…",
    "limitations": "…",
    "relevance": "…"
  }
}
```

Required: `id`, `en` and `ko` each with `tldr`, `key_points`, `problem`, `method`, `results`;
plus `ko.title`. The rest is optional but expected whenever the paper supports it.

### Field guide

| field | length | content |
|---|---|---|
| `tldr` | 1–2 sentences | What they did and the headline result. No hype words. |
| `key_points` | 3–5 bullets | The most useful facts: core idea, a key number, a surprising finding, what is released (code/data). |
| `problem` | 2–4 sentences | The gap or question, and why existing approaches fall short. |
| `method` | 1–3 short paragraphs | **The explanation part.** Walk through how it works, intuition first, then the key mechanism. Define terms a researcher in an adjacent area would not know. A key equation in LaTeX (`$...$`) is fine if it helps. |
| `results` | 1–2 paragraphs | Give the paper's exact numbers with their baselines and benchmarks. Mention ablations that explain *why* it works. |
| `why_it_matters` | 2–3 sentences | Implications for the field and for practitioners. |
| `limitations` | 1–3 sentences | Stated limitations plus obvious caveats you notice (scale, evaluation, assumptions). |
| `relevance` | 1–2 sentences | Why this paper matches the reader's topic(s) in `meta.json → selected_topics`. |

(Lengths are for the English version. The Korean version is its translation.)
| `tags` | 3–6 | Short technical keywords (English). |

Text fields accept light Markdown: `**bold**`, `*italic*`, `` `code` ``, `- ` bullet lines,
blank-line paragraphs, `$inline$` / `$$display$$` LaTeX (rendered with KaTeX).

### Figures (`figures`)

Pick **2–4** assets that carry the paper: usually (1) the overview/architecture diagram, (2) the
main result plot or table, and optionally (3) a revealing ablation or qualitative example. Order
matters: the **first image figure becomes the card thumbnail** on the daily index, so put the
most representative picture first.

- `asset`: an `id` from `assets.json` that has non-empty `files` or a `table_file`.
- `place`: where to show it, one of `top`, `problem`, `method`, `results`, `why_it_matters`,
  `figures` (a gallery at the end; this is the default).
- `en` / `ko`: 1–3 sentences on what to look at and what it shows. Explain; don't
  just restate the caption (the original caption is shown separately).

If `assets.json` has no usable assets (abstract-only papers), use `"figures": []`.

## Faithfulness

Every number, claim and name must come from the paper. If the text is abstract-only or
garbled PDF text, say less rather than guess, and note it in `limitations` (e.g. "Summary based
on the abstract only."). Don't invent benchmarks, baselines or model sizes.

## Language

Write the **English version first**, then **translate it into Korean**. The Korean version is a
translation of the English one, not a separate summary.

### English: ASD-STE100 Simplified Technical English

**Required first step:** open and read `<skill_dir>/vendor/asd-ste100/SKILL.md` in full with the
available file reader before you write any English (the rules below are a reminder, not a replacement).
Then put `"ste": "<name> <version>"` from that file's frontmatter into summary.json;
`ste_check.py` reports a summary without it as a violation.

Follow that skill in its **STE-flavored** mode (explanatory prose): apply every structural rule,
treat the lexical rules as advice.
- One idea per sentence. At most **25 words** per sentence (formulas count as one word).
- Active voice. Use passive only when the actor is unknown or does not matter.
- Simple tenses (present, past, future). Keep a compound form only when it carries information,
  such as a hedge ("may have improved").
- No phrasal verbs ("set up" → "configure", "carry out" → "do"), no semicolons, no
  nominalizations ("performs an analysis of" → "analyzes"), no marketing adjectives (novel,
  powerful, robust, seamless, state-of-the-art) unless you give the number that earns the word.
- One topic per paragraph, at most 6 sentences. Use a list for 3 or more steps or items.
- Use one name for one thing in the whole summary (do not rotate synonyms).
- Keep every hedge as strong as the paper states it ("may", "suggests"). Do not upgrade a hedge
  to a fact and do not add facts.
- Keep technical terms, method names, benchmark names and math. Define a term once if a
  researcher in an adjacent area would not know it.
- Field titles and labels are not sentences; STE length rules apply to the prose.

After writing `summary.json`, run
`python3 <skill_dir>/scripts/ste_check.py <paper dir>` and fix the hard violations it lists
(long sentences, semicolons, phrasal verbs, nominalizations, marketing words, synonym
rotation). If a fix would drop a number, a condition or a scope qualifier, keep the longer
sentence. Advisory findings (passive voice, compound tenses) are fine when justified.

### Korean (`ko`): translation of the English version

Translate each English field into the matching Korean field: same facts, same numbers, same
order, same hedges. Do not add or drop content. Write natural Korean in written declarative
style (`~한다`, `~이다`, `~했다`), not word-for-word translationese.

**Keep these in English, as written in the English version:** engineering terms, mathematical
terms and notation, technical jargon, proper nouns (model, method, dataset, benchmark, library,
company and person names) and fixed expressions of the field. Do not force a Korean coinage
where researchers would say the English word. For example: "attention map을 token 단위로
계산한다", "baseline과 비교한다", "accuracy가 상승했다". Translate the surrounding
grammar and ordinary words. LaTeX stays identical.

`ko.title` is a Korean rendering of the paper title. Keep method names and technical terms in
English inside it (e.g. "Graph Transformer: 그래프 표현을 위한 새로운 attention 방식"). On every
page the **original English title is the main heading** and `ko.title`
is shown under it as a subtitle.

Figure explanations follow the same rule: `en` in STE, `ko` translated from `en`.

## Check your work

After writing, run:

```bash
python3 <skill_dir>/scripts/ste_check.py <paper dir>        # English STE rules
python3 <skill_dir>/scripts/render.py --date <DATE> --check --ids <ID>
```

It validates required fields, asset ids, and that the Korean fields are actually Korean.
Fix anything it reports.
