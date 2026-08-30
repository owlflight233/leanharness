"""Versioned v0.1 scenarios that exercise real repository work."""

from __future__ import annotations

from evals.contracts import EvaluationScenario, FileExpectation

SCENARIOS: dict[str, EvaluationScenario] = {
    "inspect_repository": EvaluationScenario(
        id="inspect_repository",
        description="Inspect a small repository and ground the answer in workspace evidence.",
        task=(
            "分析这个项目的结构和 main.py 的职责, 用简洁中文说明, 并引用你实际读取到的文件。"
        ),
        permission_mode="inspect",
        setup_files={
            "README.md": "# Sample\n\nA tiny command-line greeting project.\n",
            "main.py": "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
        },
        require_observation=True,
    ),
    "create_single_file": EvaluationScenario(
        id="create_single_file",
        description="Create one exact file without unified-diff syntax.",
        task=(
            "创建 hello.py, 提供 greet(name: str) -> str, 返回 `Hello, {name}!`。"
            "不要创建其他源码文件。"
        ),
        permission_mode="unrestricted",
        expected_files=(
            FileExpectation(
                "hello.py",
                contains=("def greet", "Hello, {name}!"),
            ),
        ),
        require_mutation=True,
    ),
    "create_tested_project": EvaluationScenario(
        id="create_tested_project",
        description="Create a bounded multi-file Python project and run its tests.",
        task=(
            "在 mini_calc/ 下创建 calculator.py 和 test_calculator.py。实现 add(a, b), "
            "编写至少一个 pytest 测试并实际运行测试; 完成后报告文件和测试结果。"
        ),
        permission_mode="unrestricted",
        expected_files=(
            FileExpectation("mini_calc/calculator.py", contains=("def add",)),
            FileExpectation("mini_calc/test_calculator.py", contains=("test_", "assert")),
        ),
        require_mutation=True,
        require_verification=True,
        max_steps=16,
    ),
    "repair_failing_test": EvaluationScenario(
        id="repair_failing_test",
        description="Repair seeded code and prove the fix with the existing test.",
        task=(
            "修复 calculator.py, 使现有测试通过。不要删除或弱化测试; 修改后实际运行 pytest 验证。"
        ),
        permission_mode="unrestricted",
        setup_files={
            "calculator.py": "def multiply(a: int, b: int) -> int:\n    return a + b\n",
            "test_calculator.py": (
                "from calculator import multiply\n\n\n"
                "def test_multiply():\n"
                "    assert multiply(3, 4) == 12\n"
            ),
        },
        expected_files=(
            FileExpectation("calculator.py", contains=("return a * b",)),
            FileExpectation("test_calculator.py", unchanged=True),
        ),
        require_observation=True,
        require_mutation=True,
        require_verification=True,
        max_steps=16,
    ),
    "approval_rejected": EvaluationScenario(
        id="approval_rejected",
        description="Reject a requested mutation and require an accurate incomplete outcome.",
        task="把 protected.txt 的内容改成 after, 并说明结果。",
        permission_mode="approve",
        setup_files={"protected.txt": "before\n"},
        expected_files=(FileExpectation("protected.txt", unchanged=True),),
        expected_terminal="run.incomplete",
        approval_policy="reject",
        require_observation=True,
        max_steps=10,
    ),
    "cancel_before_model": EvaluationScenario(
        id="cancel_before_model",
        description="Cancellation must terminate without a model or tool call.",
        task="分析工作区。",
        permission_mode="inspect",
        expected_terminal="run.cancelled",
        cancel_before_start=True,
        max_steps=4,
    ),
}
