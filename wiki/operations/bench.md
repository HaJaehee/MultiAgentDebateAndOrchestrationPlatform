# MADO Bench — Comparing Strategies (v1.5)

Which strategy suits which kind of work was a matter of taste. The bench turns it into a record: one
scenario file names a request and the variants to compare; the bench runs every variant as its own
conversation and writes a comparison table. Code: [app/bench.py](file:///d:/MultiAgentDebateOrchestration/app/bench.py).

```bash
python -m app.bench run bench/design-review.json             # with the models in conf.json
python -m app.bench run bench/design-review.json --dry-run   # no LLM: checks the pipeline
python -m app.bench report data/bench/<run id>/result.json   # rewrite the report with ratings
python -m app.bench diff <old result.json> <new result.json> # compare two runs; exit 1 if a variant got worse
python -m app.bench --config other.json run ...              # another configuration (and DB)
```

## 1. Scenario file

```json
{
  "name": "설계 검토 — 주문 서비스 캐시",
  "prompt": "…",
  "seed": "seeds/sales-csv",
  "variants": [
    {"label": "순차 3라운드", "strategy": "sequential_debate", "max_rounds": 3},
    {"label": "디베이트", "strategy": "adversarial_debate", "max_rounds": 3,
     "models": {"critic": "openai/other-model"}},
    {"label": "조기 종료 끔", "strategy": "sequential_debate", "converge_early": false}
  ]
}
```

Variant fields: `label` (unique), `strategy`, `max_rounds`, `agents`,
`models` (agent key → model name; the connection settings stay those of `conf.json`), `converge_early`,
`token_budget`, `sub_debates`, `sub_debate_rounds`, `custom_instructions`, and two added by
[ADR-055](../../lectures/05-adr/ADR-055-bench-checks-and-judged-rubric.md):

* `graph` — the id of a graph saved from the editor (`data/graphs/<id>.json`). Required for `graph_debate`, optional
  for `adaptive_graph` (the base graph it adapts), refused for the other strategies.
* `matrix` — agent key → list of model names. The variant becomes one variant per combination, named
  `<label> · critic=qwen3-32b, coder=…`, each with `models` filled in. Three lists of three are 27 turns.

A `models` or `matrix` key that is not an agent in `conf.json` is refused when the file is read (it used to be
ignored). `//` keys are comments. Every problem is reported at once. Two examples ship in `bench/`.

### 1.1. Checks and a rubric

```json
{
  "checks": [
    {"name": "summarize.py가 있음", "file_exists": "summarize.py"},
    {"name": "README에 합계", "file_contains": {"path": "README.md", "text": "합계"}},
    {"name": "결론이 무효화를 다룸", "conclusion_contains": {"regex": "무효화|invalidat"}},
    {"name": "스크립트가 정상 실행됨", "command": {"argv": ["{python}", "summarize.py"], "stdout_contains": "서울", "timeout": 60}},
    {"name": "미해결 쟁점이 남지 않음", "max_open_issues": 0}
  ],
  "rubric": {
    "judge": "critic",
    "scale": ["미흡", "보통", "충실"],
    "criteria": [{"name": "위험 요소", "question": "잠재 위험 요소를 구체적으로 적었는가?"}]
  }
}
```

**Checks are deterministic.** They run after a variant ends, on that variant's workspace and conclusion, and the
same output always gets the same verdict. `command` runs `argv` in the workspace without a shell (`{python}` is the
interpreter running the bench) and passes on `expect_exit` (default 0) and, if given, `stdout_contains`; this is
what tells "reported as written" from "runs". Paths outside the workspace fail. A check that raises fails alone.
A scenario file is chosen by the person typing the command, so `command` carries that person's trust and nothing
on a page or the REST API can supply one.

**A rubric is a model's opinion.** `judge` is an agent key from `conf.json`. A judge on a decision LLM gets one
`score` question per criterion in a single request; any other judge is asked for one line of JSON,
`{"scores": {"<criterion>": <step>}}`, without tools or the thinking protocol. The judge sees the request and the
conclusion and nothing about the variant (label, strategy, models). The score is the sum of steps over the maximum,
0 to 1. If any criterion cannot be read the variant has no score and says `판정 실패` with the reason: a score is
never made up. A dry run does not judge. Judge calls are not counted in the variant's usage. Read scores as
differences between variants judged by the same judge, and pick a judge with a generous `max_tokens`: on
`deepseek-v4-flash` with 1,500 the first answer was all reasoning and the app had to ask again.

## 2. What a run does

* Each variant gets its own workspace under `data/bench/<run id>/workspaces/` (a copy of `seed` if given)
  — variants never see each other's files.
* A conversation titled `[bench] <scenario> · <variant>` is created in the app's database, so it can be
  opened, compared and rated in the app. With `models`, the agent configuration is frozen up front with
  the overridden model (a locked conversation does not re-read `conf.json`).
* Turns run unattended ([Headless API §2](headless-api.md)): no plan approval, no tool questions.
* `--dry-run` swaps in `DryRunCaller`, which replaces only `_complete_unthrottled`; the tool loop,
  records, checkpoints and usage metering run for real.

## 3. Results

`result.json` and `report.md` per run. Columns: status, rounds used / allowed, early end, checks passed (`3/4`),
rubric score (`0.75`, `판정 실패`, or `채점 안 함` on a dry run), input and
output tokens, LLM time, wall time, open issues in the final ledger, failed or blocked tool calls,
tool context (`도구 컨텍스트`: tool definitions loaded on demand, file reads compacted, and reads repeated after
compaction — [LLM Integration §6](../agents/llm-integration.md#6-usage-metering-v15)), agents that never answered, files changed, and the person's rating. The report links each variant to the
first one on the compare page (`/compare?a=…&b=…`); ratings given there (`TurnModel.rating`) appear
after `report` is run again. Under each variant's heading the report lists its models, every check with its
reason, and the step the judge chose for each criterion.

### 3.1. Comparing two runs

`python -m app.bench diff old/result.json new/result.json` matches variants by label and prints status, checks,
score, tokens and wall time side by side. A variant is **worse** when it completed before and does not now, when
fewer checks pass, or when the score fell by 0.15 or more; any such variant makes the exit code 1, which is what
a script after a model or prompt change can test. A smaller score change is treated as the judge's noise. Tokens
up by more than 25 % are noted in the row and not counted as worse. Variants on one side only are listed, and a
different request in the two files is called out. The command reads the two files and nothing else, so results
carried out of a closed network can be compared anywhere.

## 4. Tests

`tests/test_bench.py`: scenario validation (including the shipped examples), a dry run end to end with a
seed and a model override, a rating showing up in the rewritten report, each kind of check against a real
workspace, the judge's blindness and its refusal to invent a score, a decision-model judge, the matrix expansion,
a graph variant, and `diff` with its exit codes. `real_llm_tests/test_45_bench.py` runs one small scenario on the
real models.
