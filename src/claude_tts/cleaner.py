"""Turn a Claude reply into something worth hearing.

Claude Code replies mix prose with fenced code, diffs, paths, tables and
markdown emphasis.  Speaking all of it is noise, so this module keeps the
prose, drops the code, and caps the result so a turn never turns into a
five minute monologue.
"""

from __future__ import annotations

import re
from typing import List

FENCE = re.compile(r"^[ \t]*```.*?(?:^[ \t]*```[ \t]*$|\Z)", re.M | re.S)
FENCE_UNCLOSED = re.compile(r"^[ \t]*```.*\Z", re.M | re.S)
INLINE_CODE = re.compile(r"`([^`\n]*)`")
IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
URL = re.compile(r"<?\b(?:https?://|www\.)\S+>?")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
HTML_TAG = re.compile(r"</?[A-Za-z][^>\n]*>")
HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.M)
SETEXT = re.compile(r"^[ \t]{0,3}(?:=+|-{2,})[ \t]*$", re.M)
BLOCKQUOTE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.M)
BULLET = re.compile(r"^[ \t]*(?:[-*+\u2022])[ \t]+", re.M)
NUMBERED = re.compile(r"^[ \t]*\d+[.)][ \t]+", re.M)
RULE = re.compile(r"^[ \t]{0,3}(?:[-*_][ \t]*){3,}$", re.M)
TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.M)
SHELL_PROMPT = re.compile(r"^[ \t]*\$[ \t]+.*$", re.M)
CHECKBOX = re.compile(r"\[[ xX]\][ \t]*")

SYMBOLS = (
    ("\u2192", " to "),  # →
    ("\u2190", " from "),
    ("\u21d2", " to "),
    ("->", " to "),
    ("=>", " to "),
    ("\u2014", ", "),  # em dash
    ("\u2013", ", "),  # en dash
    ("\u2026", "..."),
    ("&", " and "),
    ("%", " percent"),
    ("\u00b0", " degrees"),
)

SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+")


def clean_for_speech(text: str, max_chars: int = 1500) -> str:
    """Return speakable prose extracted from a markdown-ish reply."""

    if not text:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")

    # Code is the biggest source of noise: fenced blocks first, then the
    # "unclosed fence" case Claude sometimes streams at the end of a reply.
    text = FENCE.sub("\n", text)
    text = FENCE_UNCLOSED.sub("\n", text)

    text = HTML_COMMENT.sub(" ", text)
    text = HTML_TAG.sub(" ", text)

    # Tables read terribly; drop whole rows and the separator line.
    text = TABLE_ROW.sub("\n", text)

    # Shell transcripts: drop the command line, keep the surrounding prose.
    text = SHELL_PROMPT.sub("\n", text)

    # Links / images: keep the human readable label only.
    text = IMAGE.sub(r"\1", text)
    text = LINK.sub(r"\1", text)
    text = URL.sub(" ", text)

    # Inline code: keep the token, drop the backticks (it is usually a file
    # name or identifier the listener wants to hear).
    text = INLINE_CODE.sub(r"\1", text)

    text = RULE.sub("\n", text)
    text = SETEXT.sub("\n", text)
    text = HEADING.sub("", text)
    text = BLOCKQUOTE.sub("", text)
    text = CHECKBOX.sub("", text)
    text = BULLET.sub("", text)
    text = NUMBERED.sub("", text)

    for old, new in SYMBOLS:
        text = text.replace(old, new)

    # Remaining markdown emphasis / list decoration.
    text = (
        text.replace("**", "")
        .replace("__", "")
        .replace("*", " ")
        .replace("~~", "")
        .replace("|", " ")
        .replace("`", "")
    )

    text = _tidy(text)
    if not text:
        return ""

    suffix = " The rest is on screen."
    if len(text) > max_chars:
        text = _truncate(text, max(40, max_chars - len(suffix))) + suffix
    return text


def _tidy(text: str) -> str:
    lines: List[str] = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            continue
        # A line that is only punctuation / decoration carries nothing.
        if not re.search(r"[A-Za-z0-9]", line):
            continue
        lines.append(line)
    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,.;:!?])(?:\s*[,.;:!?])+", r"\1", text)
    return text.strip()


def _truncate(text: str, max_chars: int) -> str:
    """Cut on a sentence boundary when possible, otherwise a word boundary."""

    head = text[:max_chars]
    best = -1
    for match in re.finditer(r"[.!?]", head):
        best = match.start() + 1
    if best >= max_chars * 0.5:
        return head[:best].rstrip()
    space = head.rfind(" ")
    if space >= max_chars * 0.5:
        return head[:space].rstrip(" ,;:") + "..."
    return head.rstrip(" ,;:")


def split_for_segments(text: str, max_chars: int = 260) -> List[str]:
    """Split speakable text into sentence-ish chunks for the file fallback."""

    text = " ".join(text.split())
    if not text:
        return []
    sentences = [s for s in SENTENCE_END.split(text) if s]
    segments: List[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                segments.append(current)
                current = ""
            segments.extend(_pack_words(sentence, max_chars))
        elif not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars:
            current = f"{current} {sentence}"
        else:
            segments.append(current)
            current = sentence
    if current:
        segments.append(current)
    return segments


def _pack_words(text: str, max_chars: int) -> List[str]:
    out: List[str] = []
    current = ""
    for word in text.split(" "):
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current = f"{current} {word}"
        else:
            out.append(current)
            current = word
    if current:
        out.append(current)
    return out
