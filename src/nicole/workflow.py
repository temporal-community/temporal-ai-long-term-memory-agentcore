"""A movie night spans Worker lifetimes and bounded Event History runs."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from nicole.opening import (
    QUESTIONS,
    has_favorites,
    new_opening,
    question_text,
    validate_answers,
)

RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1))


def same_film(left: dict, right: dict) -> bool:
    if left.get("imdb_id") and left.get("imdb_id") == right.get("imdb_id"):
        return True

    def title(movie):
        return "".join(c for c in movie["title"].casefold() if c.isalnum())

    return title(left) == title(right) and (
        not left.get("year")
        or not right.get("year")
        or str(left["year"]) == str(right["year"])
    )


@workflow.defn
class MovieNight:
    def __init__(self):
        self.pending = []
        self.ids = set()
        self.closed = False
        self.released = False
        self.processed_turns = 0
        self.can_continue = False
        self.state = {
            "phase": "opening",
            "next_action": "retrieve taste",
            "messages": [],
            "memories": [],
            "candidates": [],
            "checked": [],
            "unavailable": [],
            "rejected": [],
            "selected": None,
            "saved": False,
            "constraints": {},
            "completed_turn": None,
            "memory_status": "Not yet retrieved",
            "error": None,
            "outing": None,
            "listings": [],
            "showtimes_status": None,
            "chosen_showtime": None,
        }

    @workflow.signal
    def say(self, turn: dict):
        if turn["id"] not in self.ids and not self.closed:
            self.ids.add(turn["id"])
            self.pending.append(turn)

    @workflow.signal
    def close(self):
        self.closed = True

    @workflow.signal
    def release(self):
        self.released = True

    @workflow.query
    def view(self) -> dict:
        return self.state

    async def call(self, name: str, data: dict, timeout: int = 30):
        return await workflow.execute_activity(
            name,
            data,
            start_to_close_timeout=timedelta(seconds=timeout),
            schedule_to_close_timeout=timedelta(seconds=timeout * 4),
            retry_policy=RETRY,
        )

    @workflow.run
    async def run(self, config: dict, checkpoint: dict | None = None) -> dict:
        self.can_continue = workflow.patched("nicole-continue-as-new-v1")
        self.state["request_id"] = workflow.info().workflow_id
        self.state["actor"] = config["actor"]
        self.state["mode"] = config
        try:
            if checkpoint and self.can_continue:
                # Signals delivered before run() must not duplicate carried input.
                incoming = [t for t in self.pending if t["id"] not in checkpoint["ids"]]
                self.state = checkpoint["state"]
                self.pending = checkpoint["pending"] + incoming
                self.ids.update(checkpoint["ids"])
                self.released = checkpoint["released"]
                self.state["run_number"] += 1
            else:
                self.state["memories"] = await self.call(
                    "retrieve_taste",
                    {
                        "request_id": self.state["request_id"],
                        "actor": config["actor"],
                        "backend": config["memory_backend"],
                    },
                )
                self.state["memory_status"] = (
                    "Retrieved from " + config["memory_backend"]
                )
                if config.get("guided_opening"):
                    favorites_intake = workflow.patched("nicole-favorites-intake-v1")
                    self.state["opening"] = new_opening(
                        needs_favorites=favorites_intake
                        and config.get("favorites_intake", True)
                        and not has_favorites(self.state["memories"])
                    )
                greeting = (
                    "Welcome back, darling. "
                    if self.state["memories"]
                    else "Hello, darling. "
                ) + (
                    QUESTIONS["venue"]
                    if config.get("guided_opening")
                    else "What would you like to watch today?"
                )
                self.state["messages"].append(
                    {
                        "role": "nicole",
                        "text": greeting,
                    }
                )
                self.state.update(phase="waiting", next_action="hear your thoughts")
                if config.get("guided_opening"):
                    self.state["next_action"] = "answer venue question"
                if self.can_continue:
                    self.state.update(
                        run_number=1,
                        total_turns=0,
                        message_offset=0,
                        working_memory_summary="",
                    )
            if self.can_continue:
                self.state["run_id"] = workflow.info().run_id
            while not self.closed and (self.can_continue or self.processed_turns < 24):
                await workflow.wait_condition(lambda: bool(self.pending) or self.closed)
                if self.closed:
                    break
                turn = self.pending.pop(0)
                await self.process_turn(turn, config)
                self.processed_turns += 1
                if self.can_continue:
                    self.state["total_turns"] += 1
                if self.state["saved"]:
                    self.state["phase"] = "complete"
                    return self.state
                if (
                    self.can_continue
                    and not self.closed
                    and (
                        self.processed_turns >= config.get("continue_after", 12)
                        or workflow.info().is_continue_as_new_suggested()
                    )
                ):
                    # process_turn has awaited every Activity, including memory writes.
                    await workflow.wait_condition(workflow.all_handlers_finished)
                    if not workflow.patched("nicole-full-transcript-v1"):
                        self._compact_legacy_working_memory()
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
            self.state.update(phase="closed", next_action="start another evening")
        except ActivityError:
            self.state.update(
                phase="failed",
                error="A service could not complete its step. "
                "Your completed progress remains in Temporal.",
                next_action="inspect service configuration",
            )
        return self.state

    def _compact_legacy_working_memory(self):
        """Replay pre-full-transcript histories with their original checkpoint.

        New runs carry all messages. Keep this only until older histories no
        longer need the checkpoint shape recorded before this patch.
        """
        messages = self.state["messages"]
        older, recent = messages[:-8], messages[-8:]
        if older:
            excerpts = self.state.get("working_memory_summary", "").splitlines()
            excerpts.extend(
                message["role"] + ": " + " ".join(message["text"].split())[:240]
                for message in older
            )
            while len("\n".join(excerpts)) > 2000:
                excerpts.pop(0)
            self.state["working_memory_summary"] = "\n".join(excerpts)
            self.state["message_offset"] += len(older)
            self.state["messages"] = recent

    async def process_turn(self, turn: dict, config: dict):
        self.state["messages"].append({"role": "you", "text": turn["text"]})
        self.state.update(phase="thinking", next_action="consider your request")
        if config.get("cinema_support"):
            # The Activity can resolve relative dates without consulting its own clock.
            turn = {**turn, "time_utc": workflow.now().isoformat()}
        decision = await self.call(
            "decide_next",
            {
                "request_id": self.state["request_id"],
                "config": config,
                "turn": turn,
                "state": self.state,
            },
            timeout=60,
        )
        opening = self.state.get("opening")
        if opening and not opening["complete"]:
            decision = await self.collect_opening(turn, config, decision)
        viewing_changes = config.get("cinema_support") and workflow.patched(
            "nicole-viewing-change-v1"
        )
        excluded = []
        filtered_recommendations = False
        if viewing_changes:
            decision, excluded, filtered_recommendations = await self.change_viewing(
                turn, config, decision
            )
        self.state["constraints"] = decision["constraints"]
        if decision["intent"] == "cinema" and config.get("cinema_support"):
            decision = await self.cinema_decision(turn, config, decision)
        if decision["intent"] == "recommend":
            self.state["candidates"] = []
            self.state["selected"] = None
            self.state["chosen_showtime"] = None
            # A home-viewing request leaves the cinema outing behind.
            if decision.get("outing") is None:
                self.state["outing"] = None
            unavailable = []
            for candidate in decision["candidates"]:
                self.state.update(
                    phase="looking", next_action="check " + candidate["title"]
                )
                if self.state["outing"]:
                    movie = next(
                        (
                            dict(m)
                            for m in self.state["listings"]
                            if m["title"] == candidate["title"]
                        ),
                        {"unavailable": True},
                    )
                else:
                    movie = await self.call(
                        "lookup_film",
                        {
                            **candidate,
                            "request_id": self.state["request_id"],
                            "live": config["live_films"],
                        },
                    )
                if movie.get("unavailable"):
                    unavailable.append(candidate["title"])
                    self.state["unavailable"].append(candidate["title"])
                    continue
                limit = self.state["constraints"].get("max_minutes")
                rejected = {
                    item.get("imdb_id") or item.get("movie_id")
                    for item in self.state["rejected"]
                }
                eligible = (
                    movie.get("imdb_id") or movie.get("movie_id")
                ) not in rejected and (
                    limit is None
                    or (movie["minutes"] is not None and movie["minutes"] <= limit)
                )
                if viewing_changes and any(
                    same_film(movie, old)
                    for old in [*self.state["rejected"], *excluded]
                ):
                    eligible = False
                    filtered_recommendations = True
                movie["reason"] = candidate["reason"]
                self.state["checked"].append({**movie, "eligible": eligible})
                if eligible:
                    self.state["candidates"].append(movie)
            if unavailable or filtered_recommendations:
                choices = ", ".join(m["title"] for m in self.state["candidates"])
                if workflow.patched("nicole-verified-recommendation-dialogue-v1"):
                    decision["reply"] = (
                        f"Darling, here are some possibilities for tonight: {choices}. "
                        "Shall we take a closer look?"
                        if choices
                        else "Let’s try a different direction for tonight, darling. "
                        "What else are you in the mood for?"
                    )
                    if decision.get("venue_change") == "home":
                        decision["reply"] = "Home it is. " + decision["reply"]
                else:
                    decision["reply"] = (
                        f"Darling, I couldn't verify {', '.join(unavailable)} "
                        "in the film "
                        "catalog. "
                        + (
                            f"We still have {choices}. Shall we explore one of those?"
                            if choices
                            else "Shall we try a different direction for tonight?"
                        )
                    )
        elif decision["intent"] == "reject":
            title = decision.get("rejected_title")
            movie = next(
                (m for m in self.state["candidates"] if m["title"] == title), None
            )
            if movie:
                self.state["rejected"].append(
                    {
                        **movie,
                        "rejection_reason": decision.get("rejection_reason", "Passed"),
                    }
                )
                self.state["candidates"] = [
                    m
                    for m in self.state["candidates"]
                    if (m.get("imdb_id") or m.get("movie_id"))
                    != (movie.get("imdb_id") or movie.get("movie_id"))
                ]
        elif decision["intent"] == "select":
            earlier_selection = workflow.patched("nicole-earlier-film-selection-v1")
            self.state["selected"] = next(
                (
                    m
                    for m in self.state["candidates"]
                    if m["title"] == decision.get("selected_title")
                ),
                None,
            )
            if (
                earlier_selection
                and self.state["selected"] is None
                and not self.state["outing"]
            ):
                # checked is already durable session history. Do not reinterpret
                # old screening records as home films or resurrect availability.
                limit = self.state["constraints"].get("max_minutes")
                rejected = {
                    m.get("imdb_id") or m.get("movie_id")
                    for m in self.state["rejected"]
                }
                matches = {}
                for movie in self.state["checked"]:
                    identity = movie.get("imdb_id") or movie.get("movie_id")
                    if (
                        movie["title"] == decision.get("selected_title")
                        and movie.get("eligible")
                        and not movie.get("showtimes")
                        and identity not in rejected
                        and (
                            limit is None
                            or (
                                movie.get("minutes") is not None
                                and movie["minutes"] <= limit
                            )
                        )
                    ):
                        matches[identity or (movie["title"], movie["year"])] = movie
                if len(matches) == 1:
                    self.state["selected"] = {
                        k: v
                        for k, v in next(iter(matches.values())).items()
                        if k != "eligible"
                    }
            if self.state["selected"] is None:
                decision["reply"] = (
                    "Which of our verified films shall we choose, darling?"
                )
            elif self.state["outing"]:
                self.state["chosen_showtime"] = next(
                    (
                        s
                        for s in self.state["selected"].get("showtimes", [])
                        if s["id"] == decision.get("selected_showtime_id")
                    ),
                    None,
                )
                if self.state["chosen_showtime"] is None:
                    decision["reply"] = (
                        f"{self.state['selected']['title']} it is, darling. "
                        "Which time and cinema shall we choose?"
                        if workflow.patched("nicole-film-before-showing-v1")
                        else "A lovely choice. Which showing shall we save, darling?"
                    )
        self.state["messages"].append({"role": "nicole", "text": decision["reply"]})
        self.state.update(phase="remembering", next_action="retain useful preferences")
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
        self.state["memories"] = receipt["records"]
        self.state["memory_status"] = receipt["status"]
        self.state["completed_turn"] = turn["id"]
        if self.state["selected"] and (
            not self.state["outing"] or self.state["chosen_showtime"]
        ):
            if self.state["outing"]:
                if not await self.refresh_selection(config):
                    self.state.update(
                        phase="waiting", next_action="choose another showing"
                    )
                    return
            self.state.update(phase="ready_to_save", next_action="save tonight's pick")
            if config["stage_pause"]:
                await self.call("stage_checkpoint", self.state)
                await workflow.wait_condition(lambda: self.released or self.closed)
                if self.closed:
                    return
            self.state.update(phase="saving", next_action="save tonight's pick")
            await self.call(
                "save_tonight",
                {
                    "request_id": self.state["request_id"],
                    "actor": config["actor"],
                    "movie": {
                        **self.state["selected"],
                        **(
                            {"showtime": self.state["chosen_showtime"]}
                            if self.state["chosen_showtime"]
                            else {}
                        ),
                    },
                },
            )
            self.state["saved"] = True
            self.state["messages"].append(
                {
                    "role": "nicole",
                    "text": (
                        "Your evening plan is saved, darling. "
                        f"{self.state['selected']['title']} "
                        f"at {self.state['chosen_showtime']['cinema']}, "
                        f"{self.state['chosen_showtime']['display_time']}. "
                        + (
                            "This is a demo showing, "
                            "not a booking or verified availability."
                            if config.get("showtimes_backend") == "sample"
                            else "Use the ticket link when you're ready."
                            if self.state["chosen_showtime"].get("ticket_url")
                            else "The listing doesn't include a ticket link."
                        )
                        if self.state["chosen_showtime"]
                        else f"{self.state['selected']['title']} it is, darling. "
                        "Your evening is settled. Tonight's pick is saved."
                    ),
                }
            )
            self.state["next_action"] = "enjoy the film"
        else:
            self.state.update(phase="waiting", next_action="hear your thoughts")
            if (
                self.state["selected"]
                and self.state["outing"]
                and not self.state["chosen_showtime"]
                and workflow.patched("nicole-film-before-showing-v1")
            ):
                self.state["next_action"] = (
                    "choose a showing for " + self.state["selected"]["title"]
                )
            if opening and not opening["complete"]:
                self.state["next_action"] = (
                    "answer " + opening["next_question"] + " question"
                )

    async def change_viewing(self, turn: dict, config: dict, decision: dict):
        previous = list(self.state["candidates"])
        if decision.get("reject_current"):
            for movie in previous:
                if not any(same_film(movie, m) for m in self.state["rejected"]):
                    self.state["rejected"].append(
                        {**movie, "rejection_reason": "Not what I want tonight"}
                    )
        venue = decision.get("venue_change")
        # Also handle decisions from models that express home via the existing schema.
        if (
            venue is None
            and self.state["outing"]
            and decision["intent"] == "recommend"
            and decision.get("outing") is None
        ):
            venue = "home"
        if venue is None and decision["intent"] == "cinema":
            venue = "theater"
        if venue and self.state.get("opening", {}).get("complete"):
            self.state["opening"]["answers"]["venue"] = {
                "value": venue,
                "text": turn["text"],
                "answered_at": workflow.now().isoformat(),
                "turn_id": turn["id"],
            }
        excluded = previous if venue == "home" and self.state["outing"] else []
        if venue == "home":
            self.state.update(
                outing=None,
                listings=[],
                showtimes_status=None,
                candidates=[],
                selected=None,
                chosen_showtime=None,
            )
            decision.update(intent="recommend", outing=None, venue_change="home")
            decision["constraints"] = {
                **self.state["constraints"],
                **decision["constraints"],
            }
            self.state["constraints"] = decision["constraints"]
        elif decision.get("reject_current"):
            decision["intent"] = "cinema" if self.state["outing"] else "recommend"
        if decision["intent"] != "recommend":
            return decision, excluded, False

        def fresh(candidates):
            return [
                c
                for c in candidates
                if not any(
                    same_film(c, old) for old in [*self.state["rejected"], *excluded]
                )
            ]

        candidates = decision.get("candidates", [])
        allowed = fresh(candidates)
        filtered = len(allowed) != len(candidates)
        if venue == "home" and not allowed:
            # Repair a stale first decision once, with the corrected state in context.
            ranked = await self.call(
                "decide_next",
                {
                    "request_id": self.state["request_id"],
                    "config": config,
                    "turn": {
                        **turn,
                        "phase": "home_recommendations",
                        "excluded_candidates": excluded,
                    },
                    "state": self.state,
                },
                timeout=60,
            )
            allowed = fresh(ranked.get("candidates", []))
            filtered = (
                filtered
                or not allowed
                or len(allowed) != len(ranked.get("candidates", []))
            )
            decision = {
                **ranked,
                "intent": "recommend",
                "outing": None,
                "venue_change": "home",
                "constraints": decision["constraints"],
                "memories": decision["memories"],
            }
        decision["candidates"] = allowed
        return decision, excluded, filtered

    async def collect_opening(self, turn: dict, config: dict, decision: dict) -> dict:
        opening = self.state["opening"]
        flexible = workflow.patched("nicole-flexible-opening-v1")
        answers = validate_answers(decision.get("opening_answers", {}))
        for key, value in answers.items():
            opening["answers"][key] = {
                "value": value,
                "text": turn["text"],
                "answered_at": workflow.now().isoformat(),
                "turn_id": turn["id"],
            }
        opening["next_question"] = next(
            (
                key
                for key in opening.get("questions", QUESTIONS)
                if key not in opening["answers"]
            ),
            None,
        )
        opening["complete"] = opening["next_question"] is None
        if flexible and decision.get("opening_browse") and not opening["complete"]:
            opening["deferred"] = {
                "questions": [
                    key
                    for key in opening.get("questions", QUESTIONS)
                    if key not in opening["answers"]
                ],
                "text": turn["text"],
                "requested_at": workflow.now().isoformat(),
            }
            opening["complete"] = True
            opening["next_question"] = None
        constraints = dict(self.state["constraints"])
        for key in ("mood", "max_minutes", "company"):
            if key in opening["answers"]:
                constraints[key] = opening["answers"][key]["value"]
        constraints["mood"] = constraints.get("mood") or ""
        self.state["constraints"] = constraints
        if not opening["complete"]:
            acknowledgment = (
                decision.get("opening_acknowledgment", "") if flexible else ""
            )
            if flexible and answers.get("venue") == "undecided" and not acknowledgment:
                acknowledgment = "Of course—we can keep both doors open."
            return {
                **decision,
                "intent": "talk",
                "candidates": [],
                "constraints": constraints,
                "reply": (acknowledgment + " " if acknowledgment else "")
                + question_text(opening["next_question"]),
            }
        ranked = await self.call(
            "decide_next",
            {
                "request_id": self.state["request_id"],
                "config": config,
                "turn": {**turn, "phase": "opening_complete"},
                "state": self.state,
            },
            timeout=60,
        )
        ranked["memories"] = decision["memories"]
        ranked["constraints"] = constraints
        if opening["answers"].get("venue", {}).get("value") == "theater":
            ranked["intent"] = "cinema"
        return ranked

    async def search_cinemas(self, config: dict) -> dict:
        self.state.update(phase="looking", next_action="check Oakland screenings")
        return await self.call(
            "find_showtimes",
            {
                **self.state["outing"],
                "backend": config["showtimes_backend"],
                "now": workflow.now().isoformat(),
                "request_id": self.state["request_id"],
            },
            timeout=60,
        )

    def keep_listings(self, receipt: dict):
        self.state["showtimes_status"] = {
            k: v for k, v in receipt.items() if k != "showtimes"
        }
        grouped = {}
        for showing in receipt["showtimes"]:
            movie = showing["movie"]
            key = showing["movie_id"]
            if key not in grouped:
                grouped[key] = {**movie, "showtimes": []}
            grouped[key]["showtimes"].append(
                {k: v for k, v in showing.items() if k != "movie"}
            )
        self.state["listings"] = list(grouped.values())

    async def cinema_decision(self, turn: dict, config: dict, decision: dict) -> dict:
        self.state["outing"] = decision.get("outing") or {
            "date": None,
            "after": None,
            "radius_miles": 10,
        }
        self.state["candidates"] = []
        self.state["selected"] = None
        self.state["chosen_showtime"] = None
        receipt = await self.search_cinemas(config)
        self.keep_listings(receipt)
        if (
            receipt["status"] in {"not_configured", "unavailable"}
            or not receipt["showtimes"]
        ):
            decision["intent"] = "talk"
            decision["reply"] = {
                "not_configured": "Live Oakland showtimes aren't connected yet. "
                "We can still find a film for home tonight.",
                "unavailable": "The showtimes service is unavailable, darling. "
                "Shall we try the listings again or find something for home?",
            }.get(
                receipt["status"],
                "No screenings came back for that window, darling. "
                "Shall we try another time or a wider search?",
            )
            return decision
        ranked = await self.call(
            "decide_next",
            {
                "request_id": self.state["request_id"],
                "config": config,
                "turn": {**turn, "phase": "rank_showtimes"},
                "state": self.state,
            },
            timeout=60,
        )
        # Extraction belongs to the initial user-message decision, not the ranking pass.
        ranked["memories"] = decision["memories"]
        ranked["constraints"] = decision["constraints"]
        ranked["outing"] = self.state["outing"]
        if ranked["intent"] != "recommend":
            ranked["intent"] = "talk"
        return ranked

    async def refresh_selection(self, config: dict) -> bool:
        chosen = self.state["chosen_showtime"]
        receipt = await self.search_cinemas(config)
        self.keep_listings(receipt)
        fresh = next(
            (
                s
                for s in receipt["showtimes"]
                if s["id"] == chosen["id"]
                and s["starts_at"] == chosen["starts_at"]
                and s["movie_id"] == chosen["movie_id"]
                and s["cinema_id"] == chosen["cinema_id"]
            ),
            None,
        )
        if fresh:
            self.state["chosen_showtime"] = {
                k: v for k, v in fresh.items() if k != "movie"
            }
            return True
        self.state["chosen_showtime"] = None
        # Remove stale choices when refresh cannot establish current listings.
        self.state["candidates"] = []
        self.state["selected"] = None
        self.state["messages"].append(
            {
                "role": "nicole",
                "text": "I couldn't verify that showing just now, darling. "
                "Your plan hasn't been saved. Shall we refresh the choices?",
            }
        )
        return False
