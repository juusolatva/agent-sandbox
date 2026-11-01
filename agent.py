#!/usr/bin/env python3
"""
Hybrid Resilient Sandboxed Agent Loop for LM Studio.
- Uses native OpenAI tool calling for fast, reliable command execution.
- Automatically falls back to plain-text generation if LM Studio's 'peg-native'
  grammar parser throws a 400/500 error (common when local models finish a task).
- Supports native tool_calls, <bash>...</bash> tags, and ```bash code fences.
- Supports infinite iterations (MAX_ITERATIONS=0), sliding-window context trimming,
  deterministic completion verification (VERIFY_CMD), and continuous loop mode (CONTINUOUS=1).
"""

import itertools
import json
import os
import re
import subprocess
import sys
import time
from openai import OpenAI

BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://lm-proxy:1234/v1")
API_KEY = os.environ.get("OPENAI_API_KEY", "lm-studio")
MODEL_ENV = os.environ.get("LLM_MODEL", "").strip()
MAX_ITERATIONS = int(os.environ.get("MAX_ITERATIONS", "20"))  # 0 = infinite steps
MAX_HISTORY_MESSAGES = int(os.environ.get("MAX_HISTORY_MESSAGES", "24"))
COMMAND_TIMEOUT = int(os.environ.get("COMMAND_TIMEOUT", "30"))
VERIFY_CMD = os.environ.get("VERIFY_CMD", "").strip()
CONTINUOUS = os.environ.get("CONTINUOUS", "0").strip().lower() in {"1", "true", "yes"}
MAX_OUTPUT_CHARS = 8000

SYSTEM_PROMPT = """You are an autonomous AI agent running inside an isolated Linux Docker sandbox without internet access.
Your working directory is /home/agent/workspace.
Use the `run_bash` tool (or wrap commands in <bash>...</bash>) to inspect files, write scripts, and accomplish the user's goal.
When you have verified that the task is complete, call `finish_task` (or wrap your final summary in <done>...</done>)."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Execute a bash command inside the sandbox workspace and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to run (e.g. 'ls -la', './hello.sh').",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": "Signal that the task is complete and provide the final summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Final summary of what was accomplished.",
                    }
                },
                "required": ["summary"],
            },
        },
    },
]

BASH_TAG_RE = re.compile(r"<bash>\s*(.*?)\s*</bash>", re.DOTALL | re.IGNORECASE)
DONE_TAG_RE = re.compile(r"<done>\s*(.*?)\s*</done>", re.DOTALL | re.IGNORECASE)
CODE_FENCE_RE = re.compile(r"```(?:bash|sh)\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def get_active_model(client: OpenAI) -> str:
    """Return the configured model name or auto-detect a chat model from LM Studio."""
    if MODEL_ENV:
        return MODEL_ENV
    try:
        models = client.models.list()
        chat_models = [
            m.id for m in models.data if not m.id.startswith("text-embedding")
        ]
        if chat_models:
            return chat_models[0]
    except Exception as exc:
        print(f"[!] Could not reach LM Studio at {BASE_URL}: {exc}", file=sys.stderr)
        sys.exit(1)
    print("[!] No chat models appear to be available in LM Studio.", file=sys.stderr)
    sys.exit(1)


def execute_bash(command: str, label: str = "sandbox $") -> tuple[int, str]:
    """Run a shell command with a timeout and truncate excessive output."""
    print(f"\n[{label}] {command}")
    try:
        result = subprocess.run(
            ["/bin/bash", "-c", command],
            cwd="/home/agent/workspace",
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        output = output.strip() or "(no output)"
        full_output = f"Exit code: {result.returncode}\n{output}"
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        full_output = f"Error: Command timed out after {COMMAND_TIMEOUT} seconds."
        returncode = 124
    except Exception as exc:
        full_output = f"Error executing command: {exc}"
        returncode = 1

    if len(full_output) > MAX_OUTPUT_CHARS:
        full_output = (
            full_output[:MAX_OUTPUT_CHARS]
            + f"\n... [output truncated to {MAX_OUTPUT_CHARS} chars]"
        )
    print(full_output)
    return returncode, full_output


def check_verification_gate() -> tuple[bool, str]:
    """If VERIFY_CMD is set, run it to verify whether the task is truly complete."""
    if not VERIFY_CMD:
        return True, ""
    code, output = execute_bash(VERIFY_CMD, label="verify $")
    if code == 0:
        print("[*] Verification check passed (exit code 0).")
        return True, output
    print(f"[!] Verification check failed (exit code {code}). Continuing loop...")
    return (
        False,
        f"Task completion rejected because VERIFY_CMD (`{VERIFY_CMD}`) failed:\n{output}\nPlease fix the issue and try again.",
    )


def trim_history(messages: list[dict]) -> list[dict]:
    """Keep system + initial user task + most recent messages so infinite loops never overflow context."""
    if MAX_HISTORY_MESSAGES <= 0 or len(messages) <= MAX_HISTORY_MESSAGES + 2:
        return messages
    head = messages[:2]
    tail = messages[-MAX_HISTORY_MESSAGES:]
    # Avoid starting tail on an orphaned 'tool' response without its preceding 'assistant' tool_calls
    while tail and tail[0].get("role") == "tool":
        tail = tail[1:]
    return head + tail


def flatten_messages_for_fallback(messages: list[dict]) -> list[dict]:
    """Convert tool-call history into strictly alternating user/assistant messages for tool-free fallback."""
    flat: list[dict] = []

    def append_merged(role: str, text: str) -> None:
        if flat and flat[-1]["role"] == role:
            flat[-1]["content"] = f"{flat[-1]['content']}\n\n{text}".strip()
        else:
            flat.append({"role": role, "content": text})

    for m in messages:
        role = m.get("role")
        if role in {"system", "user"}:
            append_merged(role, m.get("content") or "")
        elif role == "assistant":
            parts = []
            if m.get("content"):
                parts.append(m["content"])
            for tc in m.get("tool_calls", []):
                fn = tc.get("function", {})
                parts.append(f"Called {fn.get('name')}({fn.get('arguments')})")
            append_merged("assistant", "\n".join(parts) or "OK")
        elif role == "tool":
            append_merged("user", f"Tool output:\n{m.get('content', '')}")

    append_merged(
        "user",
        "If you need to run another command, wrap it in <bash>...</bash>. If the task is complete, wrap your final summary in <done>...</done>.",
    )
    return flat


def call_llm_resilient(client: OpenAI, model: str, messages: list[dict]):
    """
    Try calling LM Studio with native tools first.
    If LM Studio's peg-native parser throws a 400/500 error, automatically fall back
    to tool-free completion so the loop never crashes.
    """
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        return response.choices[0].message, False
    except Exception as exc:
        err_detail = getattr(getattr(exc, "response", None), "text", str(exc))
        print(f"    [info] Native tool parser error ({err_detail[:120]}...); falling back to text mode.")
        fallback_msgs = flatten_messages_for_fallback(messages)
        for attempt in range(1, 4):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=fallback_msgs,
                )
                return response.choices[0].message, True
            except Exception as retry_exc:
                if attempt == 3:
                    print(f"[!] Fallback API error: {retry_exc}", file=sys.stderr)
                    return None, True
                time.sleep(1.0 * attempt)
    return None, True


def run_agent_loop(client: OpenAI, model: str, task: str) -> None:
    """Run the Reason -> Act -> Observe loop with hybrid tool + tag execution."""
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    step_iter = itertools.count(1) if MAX_ITERATIONS <= 0 else range(1, MAX_ITERATIONS + 1)
    max_label = "∞" if MAX_ITERATIONS <= 0 else str(MAX_ITERATIONS)

    for step in step_iter:
        messages = trim_history(messages)
        print(f"\n--- Step {step}/{max_label} (Model: {model}) ---")
        msg, _ = call_llm_resilient(client, model, messages)
        if msg is None:
            print("[!] Skipping step after failed retries.")
            time.sleep(2.0)
            continue

        content = (msg.content or "").strip()
        if not content:
            extra = getattr(msg, "model_extra", {}) or {}
            content = (extra.get("reasoning_content") or "").strip()

        if content and not getattr(msg, "tool_calls", None):
            print(f"\n[Agent]: {content}")

        # Case 1: Model returned native tool_calls
        if getattr(msg, "tool_calls", None):
            assistant_msg = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
            messages.append(assistant_msg)

            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                if fn_name == "finish_task":
                    passed, verify_msg = check_verification_gate()
                    if passed:
                        summary = args.get("summary", "Task complete.")
                        print(f"\n[Task Complete]: {summary}")
                        return
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": verify_msg,
                        }
                    )
                    continue

                if fn_name == "run_bash":
                    cmd = args.get("command", "")
                    _, tool_output = execute_bash(cmd)
                else:
                    tool_output = f"Unknown tool: {fn_name}"

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_output,
                    }
                )
            continue

        # Case 2: Text response (either normal or fallback from peg-native error)
        bash_match = BASH_TAG_RE.search(content) or CODE_FENCE_RE.search(content)
        if bash_match:
            cmd = bash_match.group(1).strip()
            _, tool_output = execute_bash(cmd)
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": f"Command output:\n{tool_output}",
                }
            )
            continue

        done_match = DONE_TAG_RE.search(content)
        if done_match or content:
            summary = done_match.group(1).strip() if done_match else content
            passed, verify_msg = check_verification_gate()
            if passed:
                print(f"\n[Task Complete]: {summary}")
                return
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": verify_msg})
            continue

        # Empty response guard
        messages.append(
            {
                "role": "user",
                "content": "Your last response was empty. Use `run_bash` (or <bash>...</bash>) to run a command, or `finish_task` (or <done>...</done>) to finish.",
            }
        )
    else:
        print(f"\n[!] Reached maximum iterations ({MAX_ITERATIONS}). Stopping loop.")


def main() -> None:
    client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
    model = get_active_model(client)
    print(f"[*] Connected to LM Studio via {BASE_URL}")
    print(f"[*] Using model: {model}")

    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:])
        cycle = 0
        while True:
            cycle += 1
            if CONTINUOUS:
                print(f"\n================ CONTINUOUS CYCLE #{cycle} ================")
            try:
                run_agent_loop(client, model, task)
            except Exception as exc:
                print(f"\n[!] Unexpected error in task loop (recovered): {exc}", file=sys.stderr)
            if not CONTINUOUS:
                break
            time.sleep(2.0)
        return

    while True:
        try:
            task = input("\nEnter task for sandboxed agent (or 'exit' to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break
        if not task:
            continue
        if task.lower() in {"exit", "quit"}:
            break
        try:
            run_agent_loop(client, model, task)
        except Exception as exc:
            print(f"\n[!] Unexpected error in task loop (recovered): {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
