import json

import pytest

from leanharness.context import ContextBudgetError, ContextStore
from leanharness.models import ModelMessage, ToolCall


def test_context_compacts_old_tool_results_into_evidence_capsules() -> None:
    store = ContextStore(max_chars=16_000)
    store.append(ModelMessage(role="system", content="rules"))
    store.append(ModelMessage(role="user", content="inspect"))
    for index in range(20):
        call = ToolCall(
            id=f"call-{index}",
            name="workspace_read",
            arguments={"path": f"src/file-{index}.py"},
        )
        store.append(ModelMessage(role="assistant", content="inspect", tool_calls=(call,)))
        store.append(
            ModelMessage(
                role="tool",
                tool_call_id=call.id,
                content=json.dumps(
                    {
                        "ok": True,
                        "tool": "workspace_read",
                        "result": {
                            "path": f"src/file-{index}.py",
                            "start_line": 1,
                            "line_count": 200,
                            "content": "x" * 900,
                        },
                    }
                ),
            )
        )

    result = store.compact()

    assert result.compressed_messages > 0
    assert result.saved_chars > 0
    tool_messages = [message for message in store.messages if message.role == "tool"]
    capsule = next(message for message in tool_messages if "evidence_capsule" in message.content)
    details = json.loads(capsule.content)["evidence_capsule"]
    assert details["tool"] == "workspace_read"
    assert details["path"].startswith("src/file-")
    assert details["line_count"] == 200
    assert details["re_read"] is True
    assert capsule.tool_call_id is not None
    assert all(
        message.tool_calls or message.role != "assistant" or message.content == "inspect"
        for message in store.messages
    )


def test_context_does_not_silently_drop_uncompressible_messages() -> None:
    store = ContextStore(max_chars=4_096)
    store.append(ModelMessage(role="system", content="s" * 5_000))

    with pytest.raises(ContextBudgetError):
        store.compact()


def test_context_can_checkpoint_completed_plan_step() -> None:
    store = ContextStore()
    store.append(ModelMessage(role="system", content="rules"))
    store.append(ModelMessage(role="user", content="old task"))
    store.append(ModelMessage(role="tool", tool_call_id="call-1", content="old evidence"))

    store.replace(
        [
            message
            for message in store.messages
            if message.role == "system"
        ]
        + [ModelMessage(role="user", content="bounded step summary")]
    )

    assert [message.role for message in store.messages] == ["system", "user"]
    assert store.messages[-1].content == "bounded step summary"
