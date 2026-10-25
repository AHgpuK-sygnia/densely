"""The hook's rewrite has to look like the tool's own output.

Claude Code validates updatedToolOutput against the tool's output schema and,
on a mismatch, logs an error and hands the model the original output. A
rewrite shaped {"type": "text", "content": ...} matches neither Bash nor Read,
so the hook used to compress, write the sidecar and record savings while the
model still received every byte. The tool_response shapes below are the ones
Claude Code 2.1.299 passes to PostToolUse hooks.
"""
import io
import json
import random

import pytest

from densely import hook


def _log(lines=400):
    rnd = random.Random(7)
    return "\n".join(
        f"2026-10-23T12:00:{i % 60:02d}Z {rnd.choice(['INFO', 'WARN', 'ERROR'])} "
        f"request_id={rnd.getrandbits(32):08x} latency_ms={rnd.randint(1, 900)}"
        for i in range(lines)
    )


def _bash(text):
    return {"tool_name": "Bash", "tool_input": {"command": "cat app.log"},
            "tool_response": {"stdout": text, "stderr": "", "interrupted": False,
                              "isImage": False, "noOutputExpected": False}}


def _read(text, path="/tmp/app.log"):
    n = text.count("\n") + 1
    return {"tool_name": "Read", "tool_input": {"file_path": path},
            "tool_response": {"type": "text", "file": {
                "filePath": path, "content": text, "numLines": n,
                "startLine": 1, "totalLines": n}}}


def _powershell(text):
    return {"tool_name": "PowerShell", "tool_input": {"command": "Get-Content app.log"},
            "tool_response": {"stdout": text, "stderr": "", "interrupted": False,
                              "isImage": False, "noOutputExpected": False}}


def _grep(text):
    return {"tool_name": "Grep", "tool_input": {"pattern": "ERROR", "output_mode": "content"},
            "tool_response": {"mode": "content", "numFiles": 0, "filenames": [],
                              "content": text, "numLines": text.count("\n") + 1}}


def _run(event, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(hook, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "PostToolUse", **event})))
    hook.main()
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["updatedToolOutput"] if out else None


def test_bash_rewrite_keeps_bash_output_shape(monkeypatch, capsys, tmp_path):
    event = _bash(_log())
    got = _run(event, monkeypatch, capsys, tmp_path)
    want = event["tool_response"]
    assert set(got) == set(want)
    assert got["stdout"].startswith("[densely] output auto-compressed")
    assert {k: got[k] for k in want if k != "stdout"} == {k: want[k] for k in want if k != "stdout"}


def test_read_rewrite_keeps_read_output_shape(monkeypatch, capsys, tmp_path):
    event = _read(_log())
    got = _run(event, monkeypatch, capsys, tmp_path)
    want = event["tool_response"]
    assert set(got) == set(want) and got["type"] == "text"
    assert set(got["file"]) == set(want["file"])
    assert got["file"]["content"].startswith("[densely] output auto-compressed")
    assert got["file"]["numLines"] == got["file"]["content"].count("\n") + 1
    for k in ("filePath", "startLine", "totalLines"):
        assert got["file"][k] == want["file"][k]


@pytest.mark.parametrize("make, key", [(_powershell, "stdout"), (_grep, "content")])
def test_output_tools_rewrite_keeps_output_shape(make, key, monkeypatch, capsys, tmp_path):
    event = make(_log())
    got = _run(event, monkeypatch, capsys, tmp_path)
    want = event["tool_response"]
    assert set(got) == set(want)
    assert got[key].startswith("[densely] output auto-compressed")
    assert {k: got[k] for k in want if k != key} == {k: want[k] for k in want if k != key}


@pytest.mark.parametrize("make", [_bash, _powershell])
def test_truncated_shell_output_passes_through(make, monkeypatch, capsys, tmp_path):
    text = (_log(2000) * 2)[:hook.SHELL_MAX_OUTPUT]
    assert _run(make(text), monkeypatch, capsys, tmp_path) is None


def test_other_tools_pass_through(monkeypatch, capsys, tmp_path):
    event = {"tool_name": "mcp__codegraph__codegraph_explore", "tool_input": {"query": "x"},
             "tool_response": _log()}
    assert _run(event, monkeypatch, capsys, tmp_path) is None


@pytest.mark.parametrize("event", [_bash("short output"), _read(_log(), path="/tmp/app.py")])
def test_small_output_and_code_pass_through(event, monkeypatch, capsys, tmp_path):
    assert _run(event, monkeypatch, capsys, tmp_path) is None
