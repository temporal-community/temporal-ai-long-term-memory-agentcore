# AGENTS.md

Nicole is a film-recommendation agent that runs its conversation loop as a
Temporal Workflow (`MovieNight` in `src/nicole/workflow.py`), keeps long-term
taste in AgentCore Memory or a local SQLite notebook, and can run its Worker on
AgentCore Runtime. The README explains the concepts; this file is how to run,
test, and change the repo.

## Commands

| Step | Command | Success |
| --- | --- | --- |
| Setup | `make setup` (`uv sync --group dev`) | Exits 0 |
| Offline demo | `uv run nicole stage --auto --width 100` | Exits 0 after `Remembered taste. Resumed work. A lovely evening.` |
| Tests | `make test` | Ends with `197 passed` (or more) |
| Lint | `make lint` | `All checks passed!` and `files already formatted` |
| Stop the local Worker | `make failure` | The local Worker exits; the visit's Workflow stays open |
| Pick up again | `make recovery` (`uv run nicole resume`) | The same visit continues on a new Worker |
| Reset | `make reset` | Deletes `.demo-state/nicole/stage-*` only |

The tests and the offline demo need the Temporal CLI on `PATH` and no keys,
AWS account, or model. The live chat (`uv run nicole`) needs a model provider
and OMDb key in `.env`; see `.env.example`.

## Where things live

- `src/nicole/workflow.py`: the conversation loop and its handlers. Every
  Activity goes through `MovieNight.call`, which sets the timeouts, the retry
  cap, and the summary shown in the Temporal Web UI.
- `src/nicole/activities.py`: the Activities (model call, film lookup,
  screenings, memory, saving the plan).
- `src/nicole/brain.py`: the dialogue prompt and decision validation.
- `src/nicole/memory.py`: which suggested memories become long-term taste, and
  the AgentCore Memory calls.
- `src/nicole/payloads.py`: the S3 claim check (External Storage).
- `docs/CLOUD.md`: deployment, cloud checks, and cleanup.

## Always

- Run `make test` and `make lint` before handing back a change.
- Put model calls, HTTP, files, and environment reads in Activities, called
  through `MovieNight.call`.
- Keep model clients' own retries off (`max_retries=0` or the equivalent), so
  Temporal's retry policy is the only retry layer.
- Gate any change to the Workflow's commands with `workflow.patched`, as
  `nicole-continue-as-new-v1` does, so open movie nights still replay.

## Ask first

- Running anything that calls a live model, AgentCore, S3, or DynamoDB,
  including the deploy, smoke, and verify scripts in `scripts/` and
  `uv run nicole` without `stage`.
  They cost money.
- Changing the dialogue prompt, the memory filters in `memory.py`, or what the
  terminal panels show. The video and README quote them.
- Deploying or removing AgentCore resources.

## Never

- Commit `.env`, keys, or AWS credentials, or put them in Workflow inputs.
- Delete `.demo-state/nicole/agentcore-deployment.json`; resuming a deployed
  visit needs it.
- Send the model's paraphrased memories to AgentCore on the facts path
  (`AGENTCORE_MEMORY_INPUT=facts`). Only the user's quoted words go in, checked
  by `taste_facts`.
