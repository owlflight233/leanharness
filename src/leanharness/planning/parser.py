"""Strict, intentionally small Markdown plan parser."""

from __future__ import annotations

import re
import uuid

from leanharness.errors import PlanFormatError
from leanharness.planning.contracts import PlanStep

MAX_PLAN_BYTES = 32 * 1024
MAX_PLAN_STEPS = 32
MAX_STEP_CHARS = 2_000
_HEADING = re.compile(r"^#\s+(.+?)\s*$")
_STEP = re.compile(r"^(\d{1,2})\.\s+(.+?)\s*$")
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_INLINE_CODE = re.compile(r"(`+)(.*?)\1")


def parse_plan_markdown(markdown: str) -> tuple[str, tuple[PlanStep, ...]]:
    if not isinstance(markdown, str) or not markdown.strip():
        raise PlanFormatError("Plan Markdown must not be blank")
    if len(markdown.encode("utf-8")) > MAX_PLAN_BYTES:
        raise PlanFormatError("Plan Markdown exceeds 32 KiB")
    lines = markdown.replace("\r\n", "\n").splitlines()
    title = "执行计划"
    steps: list[PlanStep] = []
    seen_heading = False
    expected_number = 1
    for raw_line in lines:
        if raw_line and raw_line[0].isspace():
            raise PlanFormatError("Nested or indented plan steps are unsupported")
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("```") or line.startswith("|") or line.startswith(("- ", "* ")):
            raise PlanFormatError("Plan must use a single-level ordered list")
        heading = _HEADING.match(line)
        if heading:
            if seen_heading or steps:
                raise PlanFormatError("Plan may contain at most one heading before steps")
            title = _clean_text(heading.group(1), "plan title")
            seen_heading = True
            continue
        match = _STEP.match(line)
        if not match:
            raise PlanFormatError("Expected an optional heading followed by ordered steps")
        number = int(match.group(1))
        if number != expected_number:
            raise PlanFormatError("Plan steps must be numbered consecutively from 1")
        value = _clean_text(match.group(2), "plan step")
        if len(value) > MAX_STEP_CHARS:
            raise PlanFormatError("Plan step exceeds 2,000 characters")
        step_title, instruction = _split_step(value)
        steps.append(
            PlanStep(
                id=str(uuid.uuid4()),
                sequence=number,
                title=step_title,
                instruction=instruction,
            )
        )
        expected_number += 1
        if len(steps) > MAX_PLAN_STEPS:
            raise PlanFormatError("Plan contains more than 32 steps")
    if not steps:
        raise PlanFormatError("Plan must contain at least one step")
    return title, tuple(steps)


def render_plan_markdown(plan: object, steps: tuple[PlanStep, ...]) -> str:
    title = getattr(plan, "title", "执行计划")
    lines = [f"# {title}"]
    for index, step in enumerate((item for item in steps if item.enabled), 1):
        lines.append(f"{index}. **{step.title}** - {step.instruction}")
    return "\n".join(lines)


def _split_step(value: str) -> tuple[str, str]:
    if " - " in value:
        title, instruction = value.split(" - ", 1)
        return title.strip("* ") or "步骤", instruction.strip()
    return value[:80].strip("* ") or "步骤", value


def _clean_text(value: str, label: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise PlanFormatError(f"{label} must not be blank")
    # Angle brackets are valid in technical prose and inline code, for example
    # ``>=3.12``, ``add <title>`` and ``list[T]``.  Only raw HTML outside an
    # inline-code span belongs outside the deliberately small plan protocol.
    html_candidate = _INLINE_CODE.sub("", cleaned)
    if _HTML_TAG.search(html_candidate):
        raise PlanFormatError(f"{label} contains unsupported markup")
    return cleaned
