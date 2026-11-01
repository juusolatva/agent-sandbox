#!/usr/bin/env python3
"""
Hybrid Resilient Sandboxed Agent Loop for LM Studio.
- Uses native OpenAI tool calling for fast, reliable command execution.
- Falls back to explicit text actions for peg-native parser errors.
- Executes validated native tool calls or single explicit XML actions.
- Supports infinite iterations (MAX_ITERATIONS=0), sliding-window context trimming,
  deterministic completion verification (VERIFY_CMD), and continuous loop mode (CONTINUOUS=1).
"""

import itertools
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from enum import IntEnum

try:
    from openai import OpenAI
except ModuleNotFoundError as exc:
    if exc.name != "openai":
        raise
    OpenAI = None

BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://lm-proxy:1234/v1")
API_KEY = os.environ.get("OPENAI_API_KEY", "lm-studio")
MODEL_ENV = os.environ.get("LLM_MODEL", "").strip()
MAX_ITERATIONS = int(os.environ.get("MAX_ITERATIONS", "20"))  # 0 = infinite steps
MAX_HISTORY_MESSAGES = int(os.environ.get("MAX_HISTORY_MESSAGES", "24"))
COMMAND_TIMEOUT = int(os.environ.get("COMMAND_TIMEOUT", "30"))
VERIFY_CMD = os.environ.get("VERIFY_CMD", "").strip()
CONTINUOUS = os.environ.get("CONTINUOUS", "0").strip().lower() in {"1", "true", "yes"}
MAX_OUTPUT_CHARS = 8000
MAX_ACTION_CHARS = 32000
WORKSPACE = os.environ.get("WORKSPACE", "/home/agent/workspace")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))


class Outcome(IntEnum):
    COMPLETED = 0
    FAILED = 1
    EXHAUSTED = 2
    INTERRUPTED = 130

SYSTEM_PROMPT = """You are an autonomous AI agent running inside an isolated Linux Docker sandbox without internet access.
Your working directory is /home/agent/workspace.
Use the `run_bash` tool (or emit a single <bash>...</bash> action) to inspect files, write scripts, and accomplish the user's goal.
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
    """Drain merged output with bounded storage and clean up the process group."""
    print(f"\n[{label}] {command}")
    retained = bytearray()
    truncated = False
    process = None
    try:
        process = subprocess.Popen(
            ["/bin/bash", "-c", command], cwd=WORKSPACE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
        deadline = time.monotonic() + COMMAND_TIMEOUT
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, COMMAND_TIMEOUT)
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = MAX_OUTPUT_CHARS - len(retained)
                    retained.extend(chunk[:available])
                    truncated |= len(chunk) > available
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        returncode = process.returncode
        status = f"Exit code: {returncode}"
    except subprocess.TimeoutExpired:
        returncode = 124
        status = f"Error: Command timed out after {COMMAND_TIMEOUT} seconds."
    except Exception as exc:
        returncode = 1
        status = f"Error executing command: {exc}"
    finally:
        if process is not None:
            # Also remove background children after a normally completed shell.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
    output = retained.decode("utf-8", errors="replace").strip() or "(no output)"
    full_output = f"{status}\n{output}"
    if truncated:
        full_output += "\n... [output truncated]"
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
    """Keep system + initial user task + most recent messages with a bounded message count (not a token budget)."""
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
    to tool-free completion with bounded retries.
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
        if "peg-native" not in err_detail.lower():
            print(f"[!] Inference API error: {err_detail[:200]}", file=sys.stderr)
            return None, False
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


def validate_action(name: str, arguments: str) -> dict:
    """Validate untrusted tool arguments before dispatch."""
    fields = {"run_bash": "command", "finish_task": "summary"}
    if name not in fields:
        raise ValueError(f"Unknown tool: {name}")
    if not isinstance(arguments, str) or len(arguments) > MAX_ACTION_CHARS:
        raise ValueError("Arguments must be a bounded JSON string")
    args = json.loads(arguments)
    field = fields[name]
    if not isinstance(args, dict) or set(args) != {field}:
        raise ValueError(f"Expected an object containing only {field}")
    if not isinstance(args[field], str) or not args[field].strip():
        raise ValueError(f"{field} must be a nonempty string")
    return args


def run_agent_loop(client: OpenAI, model: str, task: str, *, execute=None, verify=None, infer=None, steps=None) -> Outcome:
    """Dispatch explicit actions; completion is a verified, explicit outcome."""
    execute = execute or execute_bash
    verify = verify or check_verification_gate
    infer = infer or call_llm_resilient
    limit = MAX_ITERATIONS if steps is None else steps
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task}]
    step_numbers = itertools.count(1) if limit <= 0 else range(1, limit + 1)
    for step in step_numbers:
        messages = trim_history(messages)
        print(f"\n--- Step {step} (Model: {model}) ---")
        msg, _ = infer(client, model, messages)
        if msg is None:
            return Outcome.FAILED
        content = msg.content or ""
        calls = getattr(msg, "tool_calls", None)
        if calls:
            messages.append({"role": "assistant", "content": content, "tool_calls": [
                {"id": c.id, "type": "function", "function": {
                    "name": c.function.name, "arguments": c.function.arguments}} for c in calls]})
            for call in calls:
                try:
                    name = call.function.name
                    args = validate_action(name, call.function.arguments)
                    if name == "finish_task":
                        if len(calls) != 1:
                            raise ValueError("finish_task must be the only action in a turn")
                        passed, output = verify()
                        if passed:
                            print(f"\n[Task Complete]: {args['summary']}")
                            return Outcome.COMPLETED
                    else:
                        _, output = execute(args["command"])
                except (ValueError, TypeError) as exc:
                    output = f"Invalid action: {exc}"
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
            continue
        # Only a complete, single tag is an action. Prose, fences and reasoning
        # are never interpreted as commands or implicit success.
        messages.append({"role": "assistant", "content": content})
        bash = BASH_TAG_RE.fullmatch(content.strip())
        done = DONE_TAG_RE.fullmatch(content.strip())
        output = "Use one explicit run_bash/finish_task call or one <bash>/<done> action."
        try:
            if bash or done:
                match = bash or done
                name = "run_bash" if bash else "finish_task"
                field = "command" if bash else "summary"
                value = match.group(1).strip()
                if any(tag in value.lower() for tag in ("<bash>", "</bash>", "<done>", "</done>")):
                    raise ValueError("Multiple or nested actions are not supported")
                args = validate_action(name, json.dumps({field: value}))
                if bash:
                    _, output = execute(args[field])
                else:
                    passed, output = verify()
                    if passed:
                        print(f"\n[Task Complete]: {value}")
                        return Outcome.COMPLETED
        except ValueError as exc:
            output = f"Invalid action: {exc}"
        messages.append({"role": "user", "content": output})
    print(f"[!] Reached maximum iterations ({limit}).")
    return Outcome.EXHAUSTED


def main() -> int:
    if OpenAI is None:
        raise RuntimeError("Install requirements-controller.txt to use inference")
    client = OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=LLM_TIMEOUT, max_retries=2)
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
                outcome = run_agent_loop(client, model, task)
            except Exception as exc:
                print(f"\n[!] Unexpected error in task loop (recovered): {exc}", file=sys.stderr)
                outcome = Outcome.FAILED
            print(f"[*] Run outcome: {outcome.name.lower()}")
            if not CONTINUOUS or outcome != Outcome.COMPLETED:
                return int(outcome)
            time.sleep(2.0)

    while True:
        try:
            task = input("\nEnter task for sandboxed agent (or 'exit' to quit): ").strip()
        except EOFError:
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

    return 0


def interrupt(signum, frame):
    """Unwind active command cleanup on container termination."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupt)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(Outcome.INTERRUPTED)
