# MADO Bench — Comparing Strategies (v1.5)

Which strategy suits which kind of work was a matter of taste. The bench turns it into a record: one
scenario file names a request and the variants to compare; the bench runs every variant as its own
conversation and writes a comparison table. Code: [app/bench.py](file:///d:/MultiAgentDebateOrchestration/app/bench.py).

```bash
python -m app.bench run bench/design-review.json             # with the models in conf.json
python -m app.bench run bench/design-review.json --dry-run   # no LLM: checks the pipeline
python -m app.bench report data/bench/<run id>/result.json   # rewrite the report with ratings
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

Variant fields: `label` (unique), `strategy` (not `graph_debate`, which needs a graph file), `max_rounds`, `agents`,
`models` (agent key → model name; the connection settings stay those of `conf.json`), `converge_early`,
`token_budget`, `sub_debates`, `sub_debate_rounds`, `custom_instructions`. `//` keys are comments. Every problem is reported at once. Two
examples ship in `bench/`.

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

`result.json` and `report.md` per run. Columns: status, rounds used / allowed, early end, input and
output tokens, LLM time, wall time, open issues in the final ledger, failed or blocked tool calls,
agents that never answered, files changed, and the person's rating. The report links each variant to the
first one on the compare page (`/compare?a=…&b=…`); ratings given there (`TurnModel.rating`) appear
after `report` is run again.

## 4. Tests

`tests/test_bench.py`: scenario validation (including the shipped examples), a dry run end to end with a
seed and a model override, and a rating showing up in the rewritten report.
