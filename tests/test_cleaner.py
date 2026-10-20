from claude_tts.cleaner import clean_for_speech, split_for_segments


def test_keeps_prose_and_drops_code():
    reply = """### Fix applied
Updated `src/auth.py` to refresh tokens.

```python
def refresh():
    return token
```

Tests pass."""
    out = clean_for_speech(reply)
    assert "Fix applied" in out
    assert "src/auth.py" in out
    assert "def refresh" not in out
    assert "```" not in out
    assert "Tests pass" in out
    assert "refresh tokens" in out


def test_drops_unclosed_fence():
    out = clean_for_speech("Here is the patch:\n```diff\n+ x = 1\n")
    assert "Here is the patch" in out
    assert "x = 1" not in out


def test_strips_links_images_tables_and_rules():
    reply = """See [the docs](https://example.com/x) or https://example.com.

![screenshot](img.png)

| a | b |
|---|---|
| 1 | 2 |

---

Done."""
    out = clean_for_speech(reply)
    assert "the docs" in out
    assert "example.com" not in out
    assert "screenshot" in out
    assert "|" not in out
    assert "---" not in out
    assert out.rstrip().endswith("Done.")


def test_bullets_and_emphasis():
    out = clean_for_speech("- **one** thing\n- *two* things\n\n1. third\n")
    assert out == "one thing two things third"


def test_code_only_reply_is_empty():
    assert clean_for_speech("```bash\nls -la\n```") == ""
    assert clean_for_speech("   \n\n") == ""


def test_truncation_keeps_sentence_boundary():
    reply = "First sentence is here. " * 100
    out = clean_for_speech(reply, max_chars=120)
    assert len(out) <= 120
    assert out.endswith("The rest is on screen.")
    assert out.startswith("First sentence is here.")


def test_short_text_untouched():
    assert clean_for_speech("Tests pass.") == "Tests pass."


def test_split_for_segments():
    text = "One short sentence. " * 30
    segments = split_for_segments(text, 120)
    assert segments
    assert all(len(s) <= 121 for s in segments)
    assert "".join(segments).replace(" ", "") == text.replace(" ", "")
