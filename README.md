# Nicole: A serverless agent with long-term memory

AI agents need to keep track of a conversation while they work and carry useful
information into later conversations. If the conversation and next step live only
in the running process, stopping that process means losing progress. Serverless
applications need to handle this routinely, because compute can stop while the
agent is waiting for a reply. They also need to decide which information belongs
to the current interaction and which is worth keeping for the next one.

This repo shows how to handle both with Nicole, a film-recommendation agent built
with Temporal and Amazon Bedrock AgentCore. Temporal preserves the conversation
and execution progress so another Worker can continue where the previous one
stopped. AgentCore Memory keeps film preferences across visits, while AgentCore
Runtime hosts the Workers. Tonight’s constraints stay with the current movie
night, and lasting taste helps with future recommendations. The walkthrough
explains how that state and memory become context for each model call, so you
can use the same approach in your own serverless agent.

## How to run Nicole

Install Python 3.11+, [uv](https://docs.astral.sh/uv/), and the
[Temporal CLI](https://docs.temporal.io/cli).

### Demo rehearsal mode

Run the scripted rehearsal from the repository directory:

```sh
uv sync --group dev
uv run nicole stage
```

Press Enter at each cue. No API keys or AWS account are needed. Use a terminal
100–120 columns wide and at least 44 rows tall.

The rehearsal records favorite films, retrieves them on another visit, and saves
a selected film after killing and replacing the Worker. Dialogue and film data
are fixtures, but Temporal execution and recovery are real. No external services
are called.

Run unattended with Event History replay:

```sh
uv run nicole stage --auto --width 100
```

The rehearsal uses an isolated local server and temporary state. The pause before
saving makes Worker recovery visible at a predictable point; ordinary chat does
not need that pause.

### Run Nicole locally

Copy the example configuration without overwriting an existing file:

```sh
test -e .env || cp .env.example .env
```

For OpenRouter, fill in these values privately in `.env`:

```dotenv
NICOLE_PROVIDER=openrouter
NICOLE_MODEL=anthropic/claude-sonnet-4.6
OPENROUTER_API_KEY=your-openrouter-key
OMDB_API_KEY=your-omdb-key
```

Start Nicole:

```sh
uv run nicole --runtime local
```

Without Cloud settings, Nicole starts a local Temporal server if needed.
Local taste memory, turn records, and saved plans use SQLite under
`.demo-state/nicole/`.

For Anthropic, use `--provider anthropic`, `ANTHROPIC_API_KEY`, and an appropriate
model ID. For Bedrock, use `--provider bedrock`, a model or inference-profile ID
in `NICOLE_MODEL`, and your normal AWS credential chain. A live conversation uses
your configured model; missing keys do not silently select scripted dialogue.

To connect a local Worker to Temporal Cloud, set `TEMPORAL_MODE=cloud`,
`TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, and `TEMPORAL_API_KEY` in `.env`.
Keep `--runtime local`: Cloud stores the execution while the Worker runs locally.
An incomplete or unreachable Cloud configuration does not fall back to a local
server. [.env.example](.env.example) lists the settings.

To keep a local server and its Web UI available after chat exits, run this in
another terminal:

```sh
temporal server start-dev
```

Open [localhost:8233](http://localhost:8233) to inspect the Workflows.

### Deploy Nicole to AgentCore

Follow the [AgentCore deployment guide](docs/CLOUD.md) to provision your
resources and register the Worker with Temporal Cloud. You need AWS credentials,
a Temporal Cloud namespace enabled for the
[AgentCore Serverless Workers integration](https://docs.temporal.io/serverless-workers/agentcore),
and the provider keys listed in the guide. The integration is in pre-release.

After deployment, start Nicole:

```sh
uv run nicole
```

Keep an active AWS login with access to the payload bucket and preserve the
generated `.demo-state/nicole/agentcore-deployment.json` receipt. The default
command needs this deployment; use `stage` or `--runtime local` before deploying.

### Interact with Nicole

Answer Nicole’s questions about theater or home, mood, available time, and
company. If she asks for favorite films, give up to four or defer the question.
You can answer several questions together or ask to see options first.

Try “Something thoughtful, but not bleak, under two hours,” then ask about one of
the suggestions. The split-screen terminal shows the conversation and film cards
alongside application state, working memory, and retrieved long-term taste.
Use `--plain-chat` to append replies and cards to scrollback instead.

| Command     | What it does                                                                    |
| ----------- | ------------------------------------------------------------------------------- |
| `/notebook` | Shows the retrieved taste records                                               |
| `/showings` | Shows screenings for the selected film or current shortlist                     |
| `/new`      | Closes this evening and starts another, retrieving taste again                  |
| `/detach`   | Exits the CLI and leaves the Workflow open                                      |
| `/quit`     | Asks the Workflow to close and exits                                            |
| `/endnight` | Terminates the Workflow and clears its local resume receipt; keeps taste memory |

When you come back, `uv run nicole resume` reconnects to the saved visit with its
recorded provider, runtime, and screening backend. You can check its execution
status with `uv run nicole status`, or use `uv run nicole doctor` to check for
configuration without displaying values or calling services. To try a separate
demo profile, pass `--user alice`; this selects an identity but does not provide
authentication.

#### Cinema screenings

Nicole supports two screening sources:

| Source                                                 | Option                           |
| ------------------------------------------------------ | -------------------------------- |
| Demo schedule based on captured films and cinemas      | Default, or `--sample-showtimes` |
| International Showtimes, requiring `SHOWTIMES_API_KEY` | `--live-showtimes`               |

The sample schedule reuses captured films, cinemas, formats, and local times on
the requested date. It is labeled as demo data and does not include booking links.
Use `/showings` to view times, or choose a film, cinema, and time in one reply.
See [sample data and sources](docs/SCREENINGS.md).

### Cost to run

For an eight-turn cinema conversation with Sonnet 4.6, the current code gives
this illustrative estimate. Prices were checked October 30, 2026; these are
planning figures, not measured usage or an invoice.

| Component | Example usage | Estimated cost |
| --- | --- | --- |
| [Model](https://openrouter.ai/anthropic/claude-sonnet-4.6) | About 54,000 input tokens and 2,000–6,000 output tokens across 10 calls | $0.19–$0.25 |
| [Temporal Cloud](https://temporal.io/pricing) | About 830–840 Actions, including status polling | $0.042 |
| [AgentCore Runtime](https://aws.amazon.com/bedrock/agentcore/pricing/) | Eight isolated V1 sessions at about 6.25 session-minutes each, assuming 0.5 GB and 5 vCPU-seconds per session | $0.005 |
| [AgentCore Memory](https://aws.amazon.com/bedrock/agentcore/pricing/) | 9 retrieval calls | $0.0045 |
| **Variable subtotal** | Before storage, support, and other charges | **$0.24–$0.30** |

Opening answers use roughly 3,500–3,700 input tokens each in the offline probe;
follow-ups after cinema recommendations use around 8,500. Completing intake and
ranking screenings takes extra model calls. Token estimates use characters divided
by four, with 200–600 output tokens assumed per call.

The CLI polls every 150 ms while processing a turn. At an assumed 15-second wait,
that is about 100 billable Queries per turn, before accounting for RPC time.
AgentCore's Worker drains after 60 idle seconds, but its Runtime session can
remain for another five minutes. Nearby turns may reuse that session.

Reproduce the request sizes and cost calculations without calling paid services:

```sh
uv run python scripts/estimate_costs.py
```

The subtotal excludes storage, support, and provider subscriptions or fees.
The provider secret also costs
[about $0.40/month](https://aws.amazon.com/secrets-manager/pricing/). The offline
rehearsal uses zero model tokens and makes no paid-service calls.

## How Nicole works

### Context, long-term memory, and state

A useful way to distinguish context, long-term memory, and state is to ask who is responsible for
each one and what that responsibility involves.

| Concept           | Responsibility                                                                                                                             | Nicole’s example                                                                                                |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| Context           | The conversation loop assembles what the model sees for a particular call. The next call needs its context supplied again.                 | Your latest message, recent conversation, tonight’s constraints, retrieved taste, and film or screening results |
| Application state | The application keeps track of what has happened and where the work stands. Temporal makes this Workflow state durable across Worker loss. | Answered questions, shortlist, rejections, selected showing, saved-plan status, and the next step               |
| Long-term memory  | A memory system keeps useful information for later interactions and retrieves it when relevant.                                            | Favorite films, film-specific reviews, and recurring taste preferences                                          |

These concepts can overlap. The conversation and tool results retained for this
visit are often called **short-term memory or working memory**, and Nicole keeps
them as part of her Workflow state. When the conversation loop includes that
information in a model call, it also becomes context.

For example, saying “I have 90 minutes tonight” changes this evening’s state,
whereas “I usually prefer films that get moving quickly” can become long-term
taste. If you then ask, “Tell me more about the first one,” Nicole needs the
earlier suggestions from working memory to understand which film you mean.
In each case, the application has to bring the relevant information into context
before the model can use it.

A new visit starts a new Workflow with fresh nightly decisions and retrieves taste
for the same user. Continue-As-New, described below, starts a new **run of the same
Workflow** and carries the current evening forward.

### The Workflow and its Activities

The agent is written directly in Python with the Temporal SDK, without Strands
or another agent framework. The model chooses how to respond and which films to
suggest, while Workflow code coordinates the calls, validates the results, and
decides when a plan can be saved.

Each evening runs as a `MovieNight` Workflow, with the CLI sending your messages
as Signals and reading the `view` Query to display progress. Between turns the
Workflow waits for your next message, then calls Activities for whatever external
work is needed. It finishes when you save a plan or close the visit.

```mermaid
flowchart LR
    CLI[Terminal CLI] -->|Messages and queries| T[Temporal Service]
    T <-->|Tasks and recorded results| W[Worker: local or AgentCore Runtime]
    W --> M[Dialogue model]
    W --> F[Film facts and screenings]
    W --> L[Local notebook or AgentCore Memory]
    W --> D[SQLite or DynamoDB: turns and saved plans]
```

Both Workflow code and Activities run on a Worker, but the Workflow’s progress
lives in the Temporal Service. This means a replacement Worker can
rebuild the state through replay, using recorded events and results to get back
to where the conversation left off. The application still chooses what to send
to the model on its next call.

#### Activities

| Activity         | Work it performs                                           |
| ---------------- | ---------------------------------------------------------- |
| `retrieve_taste` | Reads the user’s taste notebook at the start of a visit    |
| `decide_next`    | Calls the model for a reply and a structured decision      |
| `lookup_film`    | Verifies a suggested film through OMDb or labeled fixtures |
| `find_showtimes` | Retrieves screenings before the model ranks cinema options |
| `remember_turn`  | Records the turn and submits eligible lasting taste        |
| `save_tonight`   | Confirms the selected plan in the domain store             |

Putting the model call inside an Activity lets Temporal record its result and
retry it if it fails. Workflow code can then use that recorded decision during
replay, without making another network call or asking the model to decide again.
The same applies to successful film lookups when another Worker picks up the
conversation.

Saving a plan is idempotent too: the domain store uses the Workflow ID as the
request key and checks that a retry matches the same user and film. Once the
write is confirmed, Nicole can tell you the plan is saved. Reserving a cinema
seat is outside the scope of this application.

Start with [workflow.py](src/nicole/workflow.py) for the loop,
[activities.py](src/nicole/activities.py) for its external calls, and
[brain.py](src/nicole/brain.py) for the dialogue prompt and decision validation.

### What becomes long-term memory

To decide what belongs in the taste notebook, Nicole’s dialogue model returns
structured memory suggestions alongside its reply. Each suggestion includes
categories, whether the preference is explicit or inferred, and an excerpt from
the user’s message to support it. This gives the application something to check
before it submits anything to the memory store.

The prompt includes examples that separate lasting taste from tonight’s mood:

> “I love stories that have wildly unhinged premises and commit to them.”

This expresses a recurring preference that could help with future recommendations.
The following request has a different scope:

> “Today I want something that can satisfy my bloodthirsty rage.”

It describes what the user wants this evening, so it belongs with tonight’s
constraints. A specific review can also be useful beyond the current visit:

> “No other film seriously wrestles with the existentialist implications of
> communication with alien life as elegantly as Contact.”

Nicole can retain that appraisal of _Contact_ without extending it into a broader
assumption about the user’s taste in films about aliens.

The Workflow awaits the memory step before marking the turn complete:

```python
receipt = await self.call(
    "remember_turn",
    {
        "id": turn["id"],
        "actor": config["actor"],
        "session": config["session"],
        "timestamp": workflow.now().isoformat(),
        "text": turn["text"],
        "reply": decision["reply"],
        "memories": decision["memories"],
        "backend": config["memory_backend"],
    },
)
```

There are two storage paths:

- **Local notebook:** stores the model’s suggested memories in SQLite, using
  stable keys so an explicit correction can replace an earlier preference.
- **Deployed AgentCore Memory:** uses `AGENTCORE_MEMORY_INPUT=facts`. Our code
  checks explicit taste categories and matching user evidence, filters temporary
  statements, and submits eligible excerpts as AgentCore events. AgentCore’s
  built-in user-preference strategy then extracts and consolidates records.

AgentCore handles extraction, consolidation, storage, and retrieval once our code
has selected the material to submit. The full exchange remains in the turn store,
while the notebook displays the records AgentCore actually returns. This keeps
the distinction between a proposed memory and a retrieved one visible in the
application. See [memory.py](src/nicole/memory.py) and the
[AgentCore memory boundary](docs/CLOUD.md#where-information-lives).

Because extraction happens asynchronously, a submitted preference may not be
ready to retrieve immediately. You also need to check the results, since even a
successful extraction can return zero records. When someone answers the favorites
question with a bare list of titles, Nicole submits it as “My favorite films
are: …” to preserve the meaning of the question along with the answer.

### Film facts and screenings

For a cinema request, the Workflow retrieves screenings before asking the model
to rank films from that set using the taste notebook. The selected showing is
checked again before the plan is saved. Sample mode regenerates the same demo
schedule for that date; only a live-provider lookup can check current listings.

OMDb verifies film identity and catalog facts; it is not the recommendation
engine or a streaming-availability service. Nicole skips catalog misses rather
than introducing suggestions that could not be verified. She does not purchase
tickets, reserve seats, take payments, or automate checkout.

### Workers on AgentCore

The terminal acts as a Temporal Client, and Temporal invokes a versioned
AgentCore endpoint when work is available. Its handler starts a Worker,
which drains after 60 seconds without Activity work once nothing is in flight.
The Workflow stays open while you decide what to say, and a later Worker can
use its history to continue the conversation.

The “Worker drained” log entry confirms that the Worker stopped, though the
underlying Runtime environment may still exist. Old versioned endpoints remain
available for executions pinned to them. Provider keys are held in Secrets
Manager, and the CLI’s AWS login also needs access to read S3 payloads.

### Claim check

Nicole uses the Python SDK’s
[External Storage](https://docs.temporal.io/develop/python/data-handling/external-storage)
S3 driver, currently in Public Preview. When a serialized payload exceeds the
configured threshold, the SDK stores it in S3 and records a reference in Event
History. It retrieves the original data when a Client or Worker needs it.

The configuration lives in [payloads.py](src/nicole/payloads.py). Its
`data_converter()` function reads the bucket, owner, region, and threshold from
process settings, creates a boto3 client, and returns this converter:

```python
return replace(
    DataConverter.default,
    external_storage=ExternalStorage(
        drivers=[S3StorageDriver(BotoS3Client(client, bucket, owner), bucket)],
        payload_size_threshold=threshold,
    ),
)
```

`BotoS3Client` adapts the existing boto3 credential chain to the SDK’s S3 driver
interface, with blocking I/O running in background threads. The driver handles
payload references and integrity checks, so Activities do not need to upload
and download their results themselves.

The shared [connection helper](src/nicole/settings.py) attaches the converter
when creating the Temporal Client:

```python
return await Client.connect(
    address,
    namespace=namespace,
    identity=identity(),
    api_key=key,
    tls=cloud or bool(key),
    data_converter=data_converter(),
)
```

Both the CLI and the [AgentCore Worker](src/nicole/agentcore.py) use this helper;
the Worker receives that Client when it starts. This gives both processes the
same storage configuration, allowing a replacement Worker to retrieve payloads
written by the previous one.

For example, a large `find_showtimes` result is stored in S3 while its reference
appears in Event History. The same mechanism applies to large conversation
payloads and the checkpoint passed through Continue-As-New. Nicole’s deployment
sets a 16 KiB threshold so the sample listings exercise this path. Smaller
payloads stay inline. Without a configured bucket, local runs use the default
converter with no external storage.

Claim check reduces payload bytes in history, while the model still receives
the full context assembled for its call. Keep the S3 objects for as long as
executions and retained histories need them. See the
[storage setup](docs/CLOUD.md#claim-check-with-s3) for configuration and retention.

### Continue-As-New

[Continue-As-New](https://docs.temporal.io/develop/python/workflows/continue-as-new)
lets Nicole keep the same movie night while starting a fresh Event History.
The Workflow ID stays the same and the Run ID changes. In
[MovieNight.run](src/nicole/workflow.py), rollover happens after 12 completed user
turns, or when Temporal recommends it. Use `--continue-after 3` to try a shorter
interval.

After a turn’s Activities finish, the Workflow checks whether to roll over and
passes its current state into the next run. This excerpt shows the path for new
executions, with older-history compatibility branches omitted:

```python
if (
    self.processed_turns >= config.get("continue_after", 12)
    or workflow.info().is_continue_as_new_suggested()
):
    await workflow.wait_condition(workflow.all_handlers_finished)
    workflow.continue_as_new(
        args=[
            config,
            {
                "state": self.state,
                "pending": self.pending,
                "ids": sorted(self.ids),
                "released": self.released,
            },
        ],
    )
```

The checkpoint keeps the full conversation and tonight’s decisions in `state`.
`pending` carries queued messages, and `ids` lets the next run recognize input it
has already accepted. `released` preserves the offline rehearsal’s pre-save
pause state. Waiting for handlers to finish allows their changes to be included
before the handoff.

The next run enters the same `run(config, checkpoint=None)` method with this
checkpoint supplied. It restores the state, merges any newly arrived messages
with the pending queue, and resumes the conversation without repeating the
initial greeting or startup memory read. `/new` does something different: it
starts a separate movie night with fresh nightly decisions.

Nicole keeps the dialogue intact across rollover, so later model calls can
still use earlier exchanges. If the checkpoint is large enough, the configured
S3 driver externalizes it automatically. Continue-As-New resets per-run history,
while claim check handles payload size; neither caps model input or total stored
data. See [the handoff details](docs/CLOUD.md#continue-as-new-within-a-movie-night)
for replay compatibility and queue handling.

## Find your way around

| File                                                                      | What to read it for                                               |
| ------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| [cli.py](src/nicole/cli.py)                                               | Chat, Signals, Queries, resume, and the offline stage             |
| [workflow.py](src/nicole/workflow.py)                                     | The conversation loop, nightly decisions, and Continue-As-New     |
| [brain.py](src/nicole/brain.py)                                           | Prompt, provider adapters, and structured decision checks         |
| [memory.py](src/nicole/memory.py)                                         | Evidence filtering, AgentCore submissions, and retrieval          |
| [films.py](src/nicole/films.py) / [showtimes.py](src/nicole/showtimes.py) | Film identity and screening adapters                              |
| [agentcore.py](src/nicole/agentcore.py)                                   | Versioned Worker startup and idle drain                           |
| [payloads.py](src/nicole/payloads.py)                                     | S3 claim check on Clients and Workers                             |
| [store.py](src/nicole/store.py) / [dynamo.py](src/nicole/dynamo.py)       | Turn receipts and idempotent plan saves                           |
| [tests/](tests/)                                                          | Recovery, replay, memory boundaries, and conversation regressions |

## Verify it

```sh
make test
make lint
uv run nicole stage --auto --width 100
```

The tests run real local Temporal executions to check Worker replacement, Event
History replay, lookup recovery, and Continue-As-New. External services are
stubbed and state is temporary, so you don’t need a live model or AWS account.
You do need the Temporal CLI on `PATH` and an environment that allows local
sockets and subprocesses.

To check your cloud deployment, run the separate
[rehearsal commands](docs/CLOUD.md#worker-lifecycle-and-rehearsal). These make
chargeable service calls to verify actual memory extraction, replacement compute,
S3 hydration, saved plans, and replay, which the offline tests cannot establish.

Local tests, lint, and formatting passed October 30, 2026 (196 tests). The
unattended offline stage last passed October 29. The last live cloud memory and fresh-visit
check was October 23. Full-transcript rollover is verified locally with claim-check
storage and Worker replacement. These changes have not been deployed to AgentCore.

## Related walkthrough

[Melanie’s agent memory and state demo](https://github.com/temporal-community/temporal-ai-agent-memory-state-stripe)
uses a refund agent to examine lost execution progress and effect state. Nicole
approaches the same questions through a conversation that continues across visits,
with a long-term memory service and serverless Workers. Together, the demos give
you two different applications to explore where an agent’s information belongs.
