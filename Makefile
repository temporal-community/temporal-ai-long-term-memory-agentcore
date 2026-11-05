.PHONY: setup run chat failure recovery reset test lint

NICOLE_USER ?= demo-user

setup:
	uv sync --group dev

run:
	uv run nicole stage

chat:
	uv run nicole

# Stop the local Worker while a visit is open, as serverless compute does. Run it
# in a second terminal during `uv run nicole --runtime local`. The visit's
# Workflow stays open in Temporal with no Worker.
failure:
	uv run nicole failure --user "$(NICOLE_USER)"

# Start a new Worker and return to the same visit. Exit the chat first (/detach
# or Ctrl+C). The conversation and tonight's decisions come back from Event
# History, and finished model calls are not made again.
recovery:
	uv run nicole resume --user "$(NICOLE_USER)"

# Delete the offline demo's runs. Keeps your taste notebook, open visits, and the
# AgentCore deployment receipt.
reset:
	rm -rf .demo-state/nicole/stage-*
	@echo "Offline demo runs removed. To end an open visit: uv run nicole resume, then /endnight."

test:
	uv run --group dev pytest -q

lint:
	uv run --group dev ruff check .
	uv run --group dev ruff format --check .
