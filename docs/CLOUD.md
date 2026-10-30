# Nicole on AgentCore

This guide deploys Nicole's Temporal Workflow and Activities to your own
AgentCore Runtime in `us-east-2`, the deployment script's default region.
No Strands conversion is involved. Temporal Cloud starts versioned Workers
through the AgentCore Serverless Workers integration. This integration is
[pre-release and requires namespace access](https://docs.temporal.io/serverless-workers/agentcore).
Use the README's offline stage or local-Worker path if you do not have that access.

## Run after deploying

A fresh clone does not include the private deployment receipt or cloud resources.
Complete the deployment steps below before using the default command.

```sh
uv run nicole
```

New visits default to the deployed queue, AgentCore Memory, and fictional
screenings on the requested date. `--runtime local` selects a local Worker. The CLI connects
to Temporal Cloud and does **not** spawn a local Worker. Keep the Temporal API key
privately in `.env`; provider credentials are read by the Runtime from Secrets
Manager. The local deployment receipt is
`.demo-state/nicole/agentcore-deployment.json`. It contains resource identifiers,
not credentials. Preserve it for the CLI and subsequent deployment phases.

`/detach` leaves the Workflow open. `uv run nicole resume` recognizes an AgentCore
visit and reconnects without starting local compute. `/quit` closes that visit.
`/endnight` terminates it immediately, exits the CLI, and clears its local resume
receipts. It keeps your taste notebook. A terminated visit cannot be resumed.
A new visit starts fresh nightly state and retrieves taste using the same actor ID.
The terminal's user flag is a demo identity, not user authentication.

Sample screenings reuse captured films, cinemas, formats, and local times on the
requested date. They are labeled as demo data and need no showtimes API key.
Rechecking a sample does not verify live availability. Nicole saves a plan;
she does not buy tickets or reserve seats. See [sample data](SCREENINGS.md).

## Where information lives

- Temporal Cloud: execution progress and Event History, including conversation,
  tool observations, and nightly decisions. Large serialized payloads appear as
  references to S3 objects and hydrate automatically on Clients and Workers.
- Private S3 payload bucket: original serialized payloads above the configured
  threshold. This is separate from the deployment code bucket and AgentCore Memory.
- DynamoDB: original turn receipts, saved plans, and diagnostic call counts. A
  saved pick uses a conditional write keyed by request ID; retries verify that
  the same actor and film already own it. Original turns and local preference
  updates use an atomic transaction when the local notebook backend is selected.
- AgentCore Memory: reusable taste records, scoped to
  `/nicole/{actorId}/preferences/`. Extraction and retrieval run through Activities.
- Secrets Manager: Temporal and provider API keys. Neither the archive nor
  Workflow inputs contain these credentials.

**This deployment uses the built-in user-preference strategy with curated input.**
AWS rejected the attempted custom extraction-model configurations. The live
rehearsal also showed that feeding full exchanges to the built-in strategy could
turn tonight's venue and runtime into lasting preferences. The Runtime therefore
sets `AGENTCORE_MEMORY_INPUT=facts`: only selected, explicit film
preference excerpts in supported taste categories are submitted to AgentCore. A fact needs a matching source excerpt with lasting-preference language;
explicit favorites-question answers are also accepted. Tonight-scoped excerpts,
hypotheses, unsupported excerpts, and turns with no lasting taste are omitted. The full source exchange remains in
Temporal and DynamoDB. AgentCore extracts and consolidates the submitted user
excerpts, and Nicole displays records actually returned by retrieval.

This depends on the dialogue model correctly distinguishing lasting taste from
nightly constraints. It is not the same policy as a custom service-side extraction
prompt. The original custom-strategy template remains in `memory-strategy.json`
for a later supported model configuration. Outside this deployment, the adapter's
default conversation-input mode remains available for custom strategies.

Extraction is asynchronous. Submission does not mean a new preference is ready.
For a recording, wait for actual retrieved records before showing a new visit.
When the favorites question receives a bare list of titles, the adapter supplies
the question's meaning as `My favorite films are: <original answer>`. This keeps
the user's personal preference explicit without adding inferred taste. In a live
check, the earlier `Favorite films:` label produced zero extracted memories;
the personally attributed version produced the expected favorites record.

For extraction diagnostics, configure [AgentCore memory application logs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html).
The default group is
`/aws/vendedlogs/bedrock-agentcore/memory/APPLICATION_LOGS/<memory_id>`.
The deployment script does not enable memory log delivery automatically.
Check extraction output as well as errors: a successful
extraction can return zero memories and therefore create no record or failed job.
An already-open visit keeps its initial retrieval until another turn completes;
use `/new` to verify startup retrieval after the record becomes available.
The visit/session used for memory events is independent of the replaceable
AgentCore Runtime compute session.

## Deploy or update

You need an authenticated AWS CLI session with permission to provision the
Nicole resources, a Temporal Cloud namespace enabled for AgentCore Serverless
Workers, and its endpoint, namespace name, and API key configured in `.env`.
The current provisioning script also requires `OMDB_API_KEY` and
`OPENROUTER_API_KEY`; use `NICOLE_PROVIDER=openrouter` and an OpenRouter model ID
in `NICOLE_MODEL`. Local chat supports other providers, but the provisioning
script's credential checks currently target this deployment configuration.

The commands below create billable resources in your account. Resource names
are dedicated to Nicole but fixed within the account/region; use a separate
account or adapt the names before running a second independent deployment.
Keep the generated deployment receipt private and do not package `.env`.

Install the Runtime dependencies locally for deployment commands:

```sh
uv sync --extra agentcore --group dev
uv run --extra agentcore python scripts/deploy_agentcore.py prepare
uv run --extra agentcore python scripts/deploy_agentcore.py package
uv run --extra agentcore python scripts/deploy_agentcore.py deploy
uv run --extra agentcore python scripts/deploy_agentcore.py register
```

`prepare` creates only Nicole-owned resources and refreshes the dedicated provider
secret from your private environment. It uses the current AWS login and the
Temporal connection in `.env`. The default region is `us-east-2`. `package` builds
a wheel and installs Linux ARM64/Python 3.12 dependencies in an isolated build
folder, then uploads a credential-free ZIP to the private deployment bucket.
`deploy` creates an immutable Runtime version and a named endpoint tied to that
version. `register` creates the matching Temporal Worker Deployment Version and
makes it current for new work. Earlier endpoints are retained for pinned visits.
Do not repoint an old endpoint or use AgentCore's moving DEFAULT endpoint for
Temporal routing.

Resources are recorded in the deployment receipt. Names are dedicated to Nicole:
`nicole_film_demo_worker` (Runtime), `nicole-film-agentcore` (Temporal deployment
and task queue), `nicole-film-demo` (DynamoDB), and `nicole/film-demo/providers`
(Secrets Manager). The private code bucket includes the account ID and region.
The Runtime role can read only Nicole's secret/code, access Nicole's table and
memory, and read/write the dedicated payload bucket's `v0/*` objects. The separate invocation role lets Temporal call Nicole's Runtime.

## Claim check with S3

Nicole uses the Python SDK's built-in External Storage S3 driver (Public Preview).
`src/nicole/payloads.py` configures the Data Converter in `settings.connect_client`,
so both the terminal Client and Runtime Worker can read references. The SDK
serializes a payload, stores it in S3, and sends a small reference containing the
bucket, key, and content hash through Temporal. It retrieves and verifies the
payload when needed. Workflow code and its decision logic are unchanged.

The deployed demo threshold is **16 KiB**, deliberately below the SDK's default
256 KiB so the sample cinema listings demonstrate the feature without
invented padding. Large Activity inputs/results, including conversation context,
can also be offloaded. Small payloads stay inline. This reduces payload bytes,
not Event History event count, model context size, or in-memory transcript size.
Continue-As-New now rolls the same visit into fresh runs; see the next section.

`prepare` creates a separate private, encrypted, versioned bucket named
`nicole-film-demo-payloads-<account>-<region>`. Its objects have **no automatic
expiry**. Keep them for as long as any execution, reset, replay, or retained
history can need them. Deleting an object breaks hydration even though the
reference still exists in Temporal. The Runtime role has Get/Put access under
`v0/*`, not delete access. The terminal uses your AWS login and requires S3 access.
No AWS credentials are embedded in a claim.

The SDK's public S3 client interface wraps our existing boto3 credential chain,
including AWS CLI login support via CRT. Blocking S3 calls run in background
threads. The built-in driver supplies content-addressed keys and integrity checks.
The adapter rejects claims outside the configured bucket and `v0/` key scope.
Storage failures fail the operation instead of silently dropping data.

The manifest loads `NICOLE_PAYLOAD_BUCKET`, `NICOLE_PAYLOAD_OWNER`,
`NICOLE_PAYLOAD_REGION`, and `NICOLE_PAYLOAD_THRESHOLD` for deployed CLI runs.
Runtime receives the same settings. Local runs stay inline unless explicitly
configured. Keep the driver and bucket configuration available for old histories;
old Runtime versions/endpoints remain pinned for their existing executions.
Temporal Cloud UI shows the reference, while Nicole displays hydrated film cards.
Use the verification script below to inspect object and reference sizes without
printing conversation content or credentials.

## Continue-As-New within a movie night

New conversations use Continue-As-New after **12 completed user turns**, or when
Temporal recommends rollover. `--continue-after N` changes that threshold for a
new visit (1–100). For a short recording, use a clearly described lower threshold:

```sh
uv run nicole --runtime agentcore --provider openrouter --sample-showtimes --continue-after 6
```

The Workflow ID and movie-night session stay the same. The Run ID changes and
the new run gets fresh Event History. In Temporal Cloud, follow the run chain.
Nicole's short-term pane displays the run number and message count.
`/new` still creates a separate movie night; Continue-As-New does not.

The Workflow rolls only between completed turns, after awaited Activities,
including memory submissions. Update handlers finish before the handoff; queued
Updates are processed first so their callers receive a result. The checkpoint
carries pending Signals, all accepted turn IDs for cross-run deduplication, the
last completed turn, opening answers/deferrals,
constraints, shortlist, earlier verified films, rejected films, selected film,
showings, retrieved taste records, and save/stage state. The new run skips the
initial greeting and memory read. It does not repeat completed model decisions,
lookups, or memory submissions. CLI handles remain Workflow-ID based, so resume,
Updates, and the pending-turn retry file continue to target the same evening.
Saving uses the same request ID across the entire chain.

At each rollover, the checkpoint keeps the **full conversation**. Later model
calls receive the earlier messages without clipping or summarization. Large
checkpoint and Activity payloads use S3 claim check, so their serialized bytes
can stay outside Event History while remaining available to the next Worker.
Original turn receipts also remain in DynamoDB (SQLite locally), and previous
run histories remain subject to Temporal retention.

Continue-As-New starts fresh per-run Event History. It does not cap model input,
the carried state, or total storage: dialogue, film records, pending input, and
deduplication IDs can still grow. Claim check handles large payloads rather than
shortening the conversation.

The `nicole-full-transcript-v1` patch preserves the former clipping behavior only
when replaying histories recorded before this change. Messages already clipped
by older code are not reconstructed automatically. Existing pinned AgentCore
visits keep their previous Worker code; start a new visit after deploying the
update to retain the full conversation across future rollovers.

The Workflow change uses the `nicole-continue-as-new-v1` patch. Old histories can
replay with their original behavior, and pinned AgentCore executions keep their
old Runtime version. Start a new visit to use this deployment. Storage and
history verification tools walk all runs in the visit before replaying them.
The cloud rehearsal uses a two-turn threshold to exercise the handoff quickly.

## Worker lifecycle and rehearsal

The handler starts a Worker as an AgentCore asynchronous task and acknowledges
the invocation immediately. It prevents duplicate Workers in one Runtime session.
The Worker drains after 60 seconds with no Activity starting or completing and
no Activity in flight. Runtime session idle timeout is five minutes, with a
30-minute compute lifetime cap. Temporal retains human waits between invocations.

```sh
uv run --extra agentcore python scripts/smoke_agentcore.py
uv run --extra agentcore python scripts/verify_claim_check.py
uv run --extra agentcore python scripts/verify_agentcore_memory.py
```

This is a live, chargeable rehearsal with a separate test actor. It uses the real
model, OMDb, AgentCore Memory, and the sample schedule. It requests tomorrow's
screenings so the run does not depend on the current hour, switches from theater
to home, waits beyond the Worker’s idle-drain window, and selects a film on a
different Worker. It verifies the saved plan and call counts in DynamoDB. Evidence goes to
`.demo-state/nicole/agentcore-smoke.json`. It must not be presented as passing
unless the final `PASS` and evidence confirm it. No local Worker is started. The claim-check command verifies real listing payload
references in Event History, hydrates their actual S3 objects, and replays that
history with the configured Data Converter. The memory command waits for real extracted taste,
checks that tonight-only information was not retained, and verifies a new visit
and Event History replay.

Runtime logs are under `/aws/bedrock-agentcore/runtimes/<runtime-id>-<endpoint>`;
use the named-endpoint group, not the DEFAULT group. Worker identities and
Runtime session IDs are logged for the replacement demonstration. Provider HTTP
request logging is suppressed because OMDb URLs can contain API keys.

## Cleanup

Stop test Runtime sessions with `bedrock-agentcore:StopRuntimeSession`. Workers
also drain automatically; the deployment resources remain until removed. Before
removing an endpoint/version, ensure no pinned Workflow still needs it. For a full
teardown, remove only the identifiers recorded in Nicole's deployment receipt:
the Temporal deployment versions, Runtime endpoints/Runtime, dedicated invocation
CloudFormation stack, execution role/policy, Memory resource, DynamoDB table,
provider secret, and deployment and payload buckets/objects (including versions).
Retain payload objects until all dependent executions and retained histories are
no longer needed. Keep saved plans or memory you
want to retain. Do not delete or modify the account's other AgentCore demos.

## Sources

- [Python SDK External Storage](https://docs.temporal.io/develop/python/data-handling/external-storage)
- [Python Continue-As-New](https://docs.temporal.io/develop/python/workflows/continue-as-new)
- [External Storage and retention](https://docs.temporal.io/external-storage)
- [Temporal AgentCore Serverless Workers](https://docs.temporal.io/serverless-workers/agentcore)
- [Python Runtime implementation](https://docs.temporal.io/develop/python/workers/serverless-workers/agentcore)
- [Temporal deployment procedure](https://docs.temporal.io/production-deployment/worker-deployments/serverless-workers/agentcore)
- [AWS direct Python code deployment](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html)
- [AgentCore user-preference strategy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/user-preference-memory-strategy.html)
- [Customizing memory extraction](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-custom-strategy.html)

Verified procedures and rehearsal results are recorded in the deployment receipt
and local smoke evidence. Never commit populated credentials or deployment archives.

## Verification

Run `make test`, `make lint`, and `uv run nicole stage --auto --width 100`
for the local checks. Cloud checks are separate: the rehearsal commands above
use real services and must complete before treating the deployment as verified.

The latest live cloud checks passed October 30, 2026, on Runtime version 9.
The rehearsal verified cinema-to-home recommendations, full-transcript
Continue-As-New across two runs, a different Worker after idle drain, and a saved
plan without repeating earlier decisions or lookups. Claim check stored a
29-showing, 18,700-byte payload in S3 with a 507-byte reference in Event History;
hydration and replay passed. AgentCore extracted the submitted favorites, and a
fresh visit retrieved them with new nightly state. Local receipts are intentionally
excluded from the public repository.

Those historical results do not verify a new account's configuration, current
provider availability, or live cinema coverage. Run the rehearsal against your
own deployment after provisioning or changing it.
