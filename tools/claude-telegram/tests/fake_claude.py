#!/usr/bin/env python3
"""Stand-in for the `claude` CLI that prints canned stream-json events."""

import json
import os
import sys

args = sys.argv[1:]
prompt = sys.stdin.read()
session = args[args.index("--resume") + 1] if "--resume" in args else "11111111-2222-3333-4444-555555555555"

with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd()}) + "\n")

if session == "missing":
    print("No conversation found with session ID: missing", file=sys.stderr)
    sys.exit(1)


def emit(event):
    print(json.dumps(event), flush=True)


emit({"type": "system", "subtype": "init", "session_id": session})
emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
    {"type": "text", "text": "Let me look."},
    {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
]}})
emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
    {"type": "text", "text": "Here you go:\n```sh\nls <dir>\n```\nUse **this**."},
]}})
emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "total_cost_usd": 0.01,
      "session_id": session, "result": "Here you go:\n```sh\nls <dir>\n```\nUse **this**."})
