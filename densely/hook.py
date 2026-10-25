#!/usr/bin/env python3
"""PostToolUse hook: auto-compress large tool outputs with densely.

Reads the hook JSON on stdin. If the tool output is large ballast (Bash,
PowerShell or Grep output, or Read of a non-code file, >= MIN_TOKENS),
replaces it with a short preview + a .dense sidecar path, and records
savings in the ledger. Exits 0 silently (passthrough) in every other case —
including any error: a hook must never break the agent.

Register (absolute path, see README):
  PostToolUse matcher "Read|Bash|PowerShell|Grep" -> command: $(which densely-hook)
"""

import json
import os
import sys
import time

MIN_TOKENS = int(os.environ.get("DENSELY_HOOK_MIN_TOKENS", "5000"))
CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".c",
            ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".kt", ".scala",
            ".sh", ".zsh", ".sql", ".dense"}
# Tools whose output has no source file to go stale against
OUTPUT_TOOLS = {"Bash", "PowerShell", "Grep"}
# Claude Code cuts shell output to this many chars before the hook sees it
# (no marker). A sidecar of the cut text would pass for the full output, so
# such output is left alone: Claude Code persists the full one to a file.
SHELL_TOOLS = {"Bash", "PowerShell"}
SHELL_MAX_OUTPUT = int(os.environ.get("BASH_MAX_OUTPUT_LENGTH") or 30000)
STATE_DIR = os.path.join(os.path.expanduser("~"), ".densely")
LEDGER = os.path.join(STATE_DIR, "ledger.jsonl")


def extract_text(resp):
    """Tolerant extraction of the text content from tool_response."""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        for key in ("output", "stdout", "content", "text"):
            v = resp.get(key)
            if isinstance(v, str) and v:
                return v
        f = resp.get("file")
        if isinstance(f, dict) and isinstance(f.get("content"), str):
            return f["content"]
    return None


def replace_text(resp, text):
    """Copy of tool_response with the field extract_text read swapped for text.

    Claude Code validates updatedToolOutput against the tool's own output
    schema and silently keeps the original output when it doesn't match.
    """
    if isinstance(resp, str):
        return text
    resp = dict(resp)
    for key in ("output", "stdout", "content", "text"):
        v = resp.get(key)
        if isinstance(v, str) and v:
            resp[key] = text
            return resp
    f = dict(resp["file"])
    f["content"] = text
    f["numLines"] = text.count("\n") + 1
    resp["file"] = f
    return resp


def ledger_add(orig, new):
    from densely import ledger
    ledger.add("compress", saved=orig - new, orig=orig, payload=new)


def ledger_total():
    from densely import ledger
    return ledger.total_saved()


def main():
    data = json.load(sys.stdin)
    if data.get("hook_event_name") != "PostToolUse":
        return
    tool = data.get("tool_name", "")
    path = ""
    if tool == "Read":
        path = (data.get("tool_input") or {}).get("file_path", "")
        if os.path.splitext(path)[1].lower() in CODE_EXT:
            return
    elif tool not in OUTPUT_TOOLS:
        return

    resp = data.get("tool_response")
    text = extract_text(resp)
    if not text or "byte-exact recovery via expand" in text:
        return
    if tool in SHELL_TOOLS and len(text) >= SHELL_MAX_OUTPUT:
        return

    from densely import compress, ntok
    orig_tokens = ntok(text)
    if orig_tokens < MIN_TOKENS:
        return
    # Command/search output has no source file to go stale against; a Read does.
    payload = compress(text, alphabet=os.environ.get("DENSELY_ALPHABET", "claude1"),
                       source=path or None)
    payload_tokens = ntok(payload)
    if payload_tokens >= orig_tokens:
        return

    os.makedirs(os.path.join(STATE_DIR, "outputs"), exist_ok=True)
    import hashlib
    name = hashlib.sha256(text.encode()).hexdigest()[:16] + ".dense"
    sidecar = os.path.join(STATE_DIR, "outputs", name)
    with open(sidecar, "w", encoding="utf-8") as fh:
        fh.write(payload)

    rows = text.split("\n")
    preview = "\n".join(rows[:10])
    ledger_add(orig_tokens, payload_tokens)
    replacement = (
        f"[densely] output auto-compressed: {orig_tokens} -> {payload_tokens} tokens "
        f"({100 * (1 - payload_tokens / orig_tokens):.0f}% saved; "
        f"total saved on this machine: {ledger_total()}), {len(rows)} lines.\n"
        f"First 10 lines:\n{preview}\n\n"
        f"Exact full content is preserved. Use densely tools with "
        f"payload_file=\"{sidecar}\": search(pattern) to find/count lines, "
        f"expand(start_line, end_line) for exact slices."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": replace_text(resp, replacement),
        }
    }))


def entry():
    try:
        main()
    except Exception:
        sys.exit(0)  # never break the agent


if __name__ == "__main__":
    entry()
