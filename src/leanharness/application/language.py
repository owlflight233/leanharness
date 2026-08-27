"""Deterministic session-language selection and model-facing instructions."""

from __future__ import annotations

import re

SessionLanguage = str
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")
_OTHER_SCRIPT = re.compile(r"[\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff\u0600-\u06ff]")


def detect_session_language(text: str) -> SessionLanguage:
    """Prefer reliable Chinese/English detection and defer other scripts to the model."""

    if _OTHER_SCRIPT.search(text):
        return "same"
    han = len(_HAN.findall(text))
    latin = len(_LATIN.findall(text))
    if han and han >= latin / 2:
        return "zh"
    if latin:
        return "en"
    return "same"


def language_instruction(language: SessionLanguage) -> str:
    if language == "zh":
        return (
            "Use Chinese for the final answer and every public progress note, unless the "
            "current user message explicitly requests another language."
        )
    if language == "en":
        return (
            "Use English for the final answer and every public progress note, unless the "
            "current user message explicitly requests another language."
        )
    return (
        "Use the same natural language as the original user task for the final answer and "
        "every public progress note, unless the current user message explicitly requests another."
    )
