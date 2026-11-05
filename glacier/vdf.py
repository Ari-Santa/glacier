"""Tiny parser for Valve's text KeyValues (VDF) format."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_TOKEN = re.compile(r'"((?:\\.|[^"\\])*)"|([{}])|(//[^\n]*)|([^\s{}"]+)')
_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), s)


class CaseInsensitiveDict(dict):
    """VDF keys are case-insensitive; keep original case but look up loosely."""

    def find(self, key: str, default: Any = None) -> Any:
        if key in self:
            return self[key]
        lower = key.lower()
        for k, v in self.items():
            if k.lower() == lower:
                return v
        return default


def loads(text: str) -> CaseInsensitiveDict:
    root = CaseInsensitiveDict()
    stack: list[CaseInsensitiveDict] = [root]
    key: str | None = None
    for m in _TOKEN.finditer(text):
        quoted, brace, comment, bare = m.groups()
        if comment is not None:
            continue
        if brace == "{":
            child = CaseInsensitiveDict()
            stack[-1][key if key is not None else ""] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        else:
            token = _unescape(quoted) if quoted is not None else bare
            if key is None:
                key = token
            else:
                stack[-1][key] = token
                key = None
    return root


def load(path: Path) -> CaseInsensitiveDict:
    return loads(Path(path).read_text(errors="replace"))
