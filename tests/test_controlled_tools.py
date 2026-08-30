from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from leanharness.models import ToolCall
from leanharness.permissions.policy import PermissionMode
from leanharness.tools import ToolRegistry


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=f"call-{name}", name=name, arguments=arguments)


def test_patch_creates_modifies_and_deletes_utf8_files(tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_text("old\nkeep\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("bye\n", encoding="utf-8")
    registry = ToolRegistry(tmp_path, mode=PermissionMode.UNRESTRICTED)
    patch = """--- a/old.txt
+++ b/old.txt
@@ -1,2 +1,2 @@
-old
+new
 keep
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+hello
+世界
--- a/delete.txt
+++ /dev/null
@@ -1,1 +0,0 @@
-bye
"""

    result = registry.execute(call("workspace_patch", patch=patch))

    assert result.ok is True
    assert (tmp_path / "old.txt").read_text(encoding="utf-8") == "new\nkeep\n"
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello\n世界\n"
    assert not (tmp_path / "delete.txt").exists()
    assert result.public_metadata["created"] == 1
    assert result.public_metadata["deleted"] == 1


def test_structured_write_creates_nested_file_without_diff_syntax(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path, mode=PermissionMode.UNRESTRICTED)

    result = registry.execute(
        call(
            "workspace_write",
            path="mini-todo/app.py",
            content="print('hello')\n",
            mode="create",
            create_parents=True,
        )
    )

    assert result.ok is True
    assert (tmp_path / "mini-todo" / "app.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert result.public_metadata["created"] is True
    assert "print('hello')" not in result.to_model_content()


def test_structured_write_replace_and_edit_use_file_hashes(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    registry = ToolRegistry(tmp_path, mode=PermissionMode.UNRESTRICTED)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    edited = registry.execute(
        call(
            "workspace_edit",
            path="app.py",
            start_line=2,
            end_line=2,
            replacement="changed",
            expected_sha256=digest,
        )
    )
    assert edited.ok is True
    assert path.read_text(encoding="utf-8") == "one\nchanged\nthree\n"

    stale = registry.execute(
        call(
            "workspace_write",
            path="app.py",
            content="stale\n",
            mode="replace",
            expected_sha256=digest,
        )
    )
    assert stale.ok is False and stale.error is not None
    assert stale.error.code == "WRITE_STALE"


def test_git_inspect_rejects_parent_repository_scope(tmp_path: Path) -> None:
    os.system(f'git -C "{tmp_path}" init -q')
    child = tmp_path / "child"
    child.mkdir()
    result = ToolRegistry(child).execute(call("git_inspect", operation="status"))

    assert result.ok is False and result.error is not None
    assert result.error.code == "GIT_SCOPE_DENIED"


def test_patch_validates_all_hunks_before_writing(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    patch = """--- a/first.txt
+++ b/first.txt
@@ -1 +1 @@
-one
+changed
--- a/second.txt
+++ b/second.txt
@@ -1 +1 @@
-stale
+changed
"""

    result = ToolRegistry(tmp_path, mode=PermissionMode.UNRESTRICTED).execute(
        call("workspace_patch", patch=patch)
    )

    assert result.ok is False and result.error is not None
    assert result.error.code == "PATCH_CONTEXT_MISMATCH"
    assert first.read_text(encoding="utf-8") == "one\n"
    assert second.read_text(encoding="utf-8") == "two\n"


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ("--- a/../escape.txt\n+++ b/../escape.txt\n@@ -0,0 +1 @@\n+x\n", "PATH_OUTSIDE_WORKSPACE"),
        ("--- a/a.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-a\n+b\n", "PATCH_RENAME_DENIED"),
    ],
)
def test_patch_rejects_unsafe_paths(tmp_path: Path, patch: str, code: str) -> None:
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    result = ToolRegistry(tmp_path, mode=PermissionMode.UNRESTRICTED).execute(
        call("workspace_patch", patch=patch)
    )
    assert result.ok is False and result.error is not None and result.error.code == code


def test_patch_approval_snapshot_detects_external_change(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("before\n", encoding="utf-8")
    registry = ToolRegistry(tmp_path, mode=PermissionMode.APPROVE)
    tool_call = call(
        "workspace_patch",
        patch="--- a/source.txt\n+++ b/source.txt\n@@ -1 +1 @@\n-before\n+after\n",
    )
    preview = registry.preview(tool_call)
    source.write_text("external\n", encoding="utf-8")

    result = registry.execute_approved(
        tool_call, expected_hashes=preview["target_hashes"]  # type: ignore[arg-type]
    )

    assert result.ok is False and result.error is not None
    assert result.error.code == "PATCH_STALE"
    assert source.read_text(encoding="utf-8") == "external\n"


def test_structured_edit_approval_rechecks_prior_read_hash(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("before\n", encoding="utf-8")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    registry = ToolRegistry(tmp_path, mode=PermissionMode.APPROVE)
    tool_call = call(
        "workspace_edit",
        path="source.txt",
        start_line=1,
        end_line=1,
        replacement="after",
        expected_sha256=expected,
    )
    registry.preview(tool_call)
    source.write_text("external\n", encoding="utf-8")

    result = registry.execute_approved(tool_call)

    assert result.ok is False and result.error is not None
    assert result.error.code == "EDIT_STALE"
    assert source.read_text(encoding="utf-8") == "external\n"


def test_inspect_mode_registers_only_read_tools(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path, mode=PermissionMode.INSPECT)
    names = {definition.name for definition in registry.definitions}
    assert "git_inspect" in names
    assert "workspace_patch" not in names
    assert "workspace_command" not in names


@pytest.mark.parametrize(
    ("mode", "expected_tools"),
    [
        (PermissionMode.INSPECT, set()),
        (
            PermissionMode.APPROVE,
            {"workspace_mkdir", "workspace_patch", "workspace_write", "workspace_edit"},
        ),
        (
            PermissionMode.UNRESTRICTED,
            {"workspace_mkdir", "workspace_patch", "workspace_write", "workspace_edit"},
        ),
    ],
)
def test_permission_modes_register_structured_mutation_tools(
    tmp_path: Path,
    mode: PermissionMode,
    expected_tools: set[str],
) -> None:
    registry = ToolRegistry(tmp_path, mode=mode)
    names = {definition.name for definition in registry.definitions}

    assert names.intersection(
        {"workspace_mkdir", "workspace_patch", "workspace_write", "workspace_edit"}
    ) == expected_tools
    if mode is PermissionMode.APPROVE:
        assert registry.approval_required(
            call("workspace_write", path="new.txt", content="new\n", mode="create")
        )
    elif mode is PermissionMode.UNRESTRICTED:
        assert not registry.approval_required(
            call("workspace_write", path="new.txt", content="new\n", mode="create")
        )


def test_command_rejects_unknown_profiles_and_dangerous_arguments(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path, mode=PermissionMode.UNRESTRICTED)

    unknown = registry.execute(call("workspace_command", profile="powershell"))
    dangerous = registry.execute(
        call("workspace_command", profile="pytest", args=["tests", "&&", "whoami"])
    )
    python_code = registry.execute(
        call("workspace_command", profile="python-test", args=["-c", "print('unsafe')"])
    )

    assert unknown.error is not None and unknown.error.code == "COMMAND_NOT_ALLOWED"
    assert dangerous.error is not None and dangerous.error.code == "COMMAND_ARGUMENT_DENIED"
    assert python_code.error is not None and python_code.error.code == "COMMAND_ARGUMENT_DENIED"


def test_command_uses_minimal_environment_and_captures_nonzero(tmp_path: Path) -> None:
    os.environ["LEANHARNESS_MODEL_API_KEY"] = "must-not-be-inherited"
    registry = ToolRegistry(tmp_path, mode=PermissionMode.UNRESTRICTED)
    result = registry.execute(
        call("workspace_command", profile="python-test", args=["missing-test-file.py"])
    )
    assert result.ok is False and result.error is not None
    assert result.error.code == "COMMAND_FAILED"
    assert result.to_model_dict()["result"]["exit_code"] != 0
    assert "must-not-be-inherited" not in str(result)


def test_command_honors_runtime_cancellation(tmp_path: Path) -> None:
    (tmp_path / "test_wait.py").write_text(
        "import time\n\ndef test_wait():\n    time.sleep(30)\n",
        encoding="utf-8",
    )
    cancelled = threading.Event()
    cancelled.set()

    result = ToolRegistry(tmp_path, mode=PermissionMode.UNRESTRICTED).execute(
        call("workspace_command", profile="pytest", args=["test_wait.py"]),
        cancel_signal=cancelled,
    )

    assert result.ok is False and result.error is not None
    assert result.error.code == "COMMAND_CANCELLED"
    assert result.public_metadata["cancelled"] is True


def test_git_inspect_reports_status_and_rejects_writes(tmp_path: Path) -> None:
    os.system(f'git -C "{tmp_path}" init -q')
    (tmp_path / "tracked.txt").write_text("content\n", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    status = registry.execute(call("git_inspect", operation="status"))
    denied = registry.execute(call("git_inspect", operation="commit"))

    assert status.ok is True and status.data is not None
    assert "tracked.txt" in status.data["output"]
    assert denied.ok is False and denied.error is not None
    assert denied.error.code == "GIT_OPERATION_DENIED"


def test_git_inspect_distinguishes_non_repository(tmp_path: Path) -> None:
    result = ToolRegistry(tmp_path).execute(call("git_inspect", operation="status"))

    assert result.ok is False and result.error is not None
    assert result.error.code == "GIT_NOT_REPOSITORY"
    assert result.public_metadata["repository"] is False


def test_git_log_can_be_scoped_to_a_workspace_file(tmp_path: Path) -> None:
    os.system(f'git -C "{tmp_path}" init -q')
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("content\n", encoding="utf-8")
    os.system(f'git -C "{tmp_path}" add tracked.txt')
    os.system(
        f'git -C "{tmp_path}" -c user.name=Test -c user.email=test@example.com '
        'commit -q -m initial'
    )

    result = ToolRegistry(tmp_path).execute(
        call("git_inspect", operation="log", path="tracked.txt")
    )

    assert result.ok is True
    assert result.data is not None
    assert "initial" in result.data["output"]
