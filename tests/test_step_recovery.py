"""Recovery resets the failed read step, never a write or another user's request."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.api.history.v1 import HistoryEvent

from nicole.runtime import recover_failed_lookup, recover_failed_step


def failed_request(operation, *, later_operation=None, actor="test"):
    events = [
        HistoryEvent(event_id=10, workflow_task_completed_event_attributes={}),
        HistoryEvent(
            event_id=11,
            activity_task_scheduled_event_attributes={
                "activity_type": {"name": operation}
            },
        ),
        HistoryEvent(
            event_id=14,
            activity_task_failed_event_attributes={"scheduled_event_id": 11},
        ),
    ]
    if later_operation:
        events.append(
            HistoryEvent(
                event_id=15,
                activity_task_scheduled_event_attributes={
                    "activity_type": {"name": later_operation}
                },
            )
        )
    handle = SimpleNamespace(
        id="request",
        describe=AsyncMock(
            return_value=SimpleNamespace(
                status=SimpleNamespace(name="COMPLETED"), run_id="failed-run"
            )
        ),
        result=AsyncMock(return_value={"actor": actor, "phase": "failed"}),
        fetch_history=AsyncMock(return_value=SimpleNamespace(events=events)),
    )
    client = SimpleNamespace(
        namespace="namespace",
        workflow_service=SimpleNamespace(reset_workflow_execution=AsyncMock()),
    )
    return handle, client


@pytest.mark.parametrize("operation", ["decide_next", "lookup_film"])
def test_resume_resets_before_only_the_failed_step(operation):
    handle, client = failed_request(operation)
    assert asyncio.run(recover_failed_step(handle, client, "test"))
    request = client.workflow_service.reset_workflow_execution.call_args.args[0]
    assert request.workflow_task_finish_event_id == 10
    assert request.workflow_execution.workflow_id == "request"
    assert request.workflow_execution.run_id == "failed-run"
    assert request.namespace == "namespace"
    assert operation in request.reason


@pytest.mark.parametrize(
    "operation", ["remember_turn", "save_tonight", "find_showtimes"]
)
def test_resume_does_not_reset_other_steps(operation):
    handle, client = failed_request(operation)
    assert not asyncio.run(recover_failed_step(handle, client, "test"))
    client.workflow_service.reset_workflow_execution.assert_not_called()


def test_resume_rejects_another_actor():
    handle, client = failed_request("decide_next", actor="other")
    with pytest.raises(ValueError, match="different user"):
        asyncio.run(recover_failed_step(handle, client, "test"))
    client.workflow_service.reset_workflow_execution.assert_not_called()


def test_resume_does_not_reset_an_earlier_failure_after_work_continued():
    handle, client = failed_request("decide_next", later_operation="remember_turn")
    assert not asyncio.run(recover_failed_step(handle, client, "test"))
    client.workflow_service.reset_workflow_execution.assert_not_called()


def test_old_lookup_only_entry_point_remains_lookup_only():
    handle, client = failed_request("decide_next")
    assert not asyncio.run(recover_failed_lookup(handle, client, "test"))
    client.workflow_service.reset_workflow_execution.assert_not_called()
