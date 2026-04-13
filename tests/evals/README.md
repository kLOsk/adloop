# AdLoop Behavioral Evals

Prompt-and-expectation test suites for validating that an AI assistant follows
AdLoop's orchestration rules correctly.

Each JSON file contains prompts a user might ask and the expected behavioral
outcomes — which tools should be called, which safety checks must happen, and
which pitfalls must be avoided.

## Structure

| File | Covers |
|------|--------|
| `read.json` | Performance analysis, GDPR awareness, GAQL, PMax, recommendations |
| `write.json` | Campaign/ad creation, keywords, negatives, pausing, safety rules |
| `tracking.json` | Event diagnosis, consent mode, tracking code generation |
| `planning.json` | Keyword discovery, budget forecasting, match type guidance |

## Usage

These are not executable unit tests. They serve as:

1. **Evaluation prompts** for benchmarking AI tool-calling accuracy
2. **Regression checks** when updating orchestration rules
3. **Documentation** of expected behavior for each workflow

Each eval entry has:
- `prompt` — the user message
- `expected_output` — a summary of correct behavior
- `expectations[]` — specific behavioral assertions to check
