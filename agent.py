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
# Estimated prompt-token budget for the retained history; 0 disables it.
MAX_CONTEXT_TOKENS = int(os.environ.get("MAX_CONTEXT_TOKENS", "12000"))
CHARS_PER_TOKEN = 3  # conservative estimate; no tokenizer is available offline
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

def text_action_body(text: str, tag: str):
    """Return the body of a reply that is exactly one <tag>...</tag> pair, else None.

    Plain prefix/suffix checks stay linear on degenerate model output; a
    backtracking regex with whitespace-overlapping groups can stall the controller.
    """
    opening, closing = f"<{tag}>", f"</{tag}>"
    lowered = text.lower()
    if len(text) < len(opening) + len(closing):
        return None
    if not (lowered.startswith(opening) and lowered.endswith(closing)):
        return None
    return text[len(opening):-len(closing)]


def get_active_model() -> str:
    """Return the explicitly configured model; never guess one from the server."""
    if MODEL_ENV:
        return MODEL_ENV
    print("[!] Set LLM_MODEL to the LM Studio model ID to use.", file=sys.stderr)
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


def estimate_tokens(message: dict) -> int:
    """Roughly estimate the prompt tokens of one message, including tool-call arguments."""
    size = len(message.get("content") or "")
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        size += len(str(function.get("name", ""))) + len(str(function.get("arguments", "")))
    return size // CHARS_PER_TOKEN + 4


def trim_history(messages: list[dict]) -> list[dict]:
    """Keep system + task, then the most recent messages within the message and estimated-token limits.

    The newest message and its assistant tool call are always kept, and the window never starts
    on a 'tool' result, so an oversized newest exchange can still exceed the budget.
    """
    head, rest = messages[:2], messages[2:]
    start = 0
    if MAX_HISTORY_MESSAGES > 0:
        start = max(0, len(rest) - MAX_HISTORY_MESSAGES)
    if MAX_CONTEXT_TOKENS > 0 and rest:
        remaining = MAX_CONTEXT_TOKENS - sum(estimate_tokens(m) for m in head)
        budget_start = len(rest)
        for index in range(len(rest) - 1, start - 1, -1):
            remaining -= estimate_tokens(rest[index])
            if remaining < 0 and budget_start < len(rest):
                break
            budget_start = index
        start = budget_start
    # Include the assistant tool call that a leading 'tool' result belongs to.
    while 0 < start < len(rest) and rest[start].get("role") == "tool":
        start -= 1
    return head + rest[start:]


def assistant_text(message: dict) -> str:
    """Render an assistant turn, including native tool calls, as plain text."""
    parts = [message["content"]] if message.get("content") else []
    for tc in message.get("tool_calls", []):
        fn = tc.get("function", {})
        parts.append(f"Called {fn.get('name')}({fn.get('arguments')})")
    return "\n".join(parts) or "OK"


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
            append_merged("assistant", assistant_text(m))
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


def dispatch(name: str, args: dict, execute, verify) -> tuple[bool, str]:
    """Run one validated action; return (accepted completion, model-visible output)."""
    if name == "run_bash":
        _, output = execute(args["command"])
        return False, output
    passed, output = verify()
    if passed:
        print(f"\n[Task Complete]: {args['summary']}")
    return passed, output


def handle_tool_calls(content: str, calls, messages: list[dict], execute, verify) -> bool:
    """Dispatch native tool calls, appending each result; return True on accepted completion."""
    messages.append({"role": "assistant", "content": content, "tool_calls": [
        {"id": c.id, "type": "function", "function": {
            "name": c.function.name, "arguments": c.function.arguments}} for c in calls]})
    for call in calls:
        try:
            name = call.function.name
            args = validate_action(name, call.function.arguments)
            if name == "finish_task" and len(calls) != 1:
                raise ValueError("finish_task must be the only action in a turn")
            completed, output = dispatch(name, args, execute, verify)
            if completed:
                return True
        except (ValueError, TypeError) as exc:
            output = f"Invalid action: {exc}"
        messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
    return False


def parse_text_action(content: str) -> tuple[str, dict] | None:
    """Return a validated (name, args) for a reply that is exactly one action tag, else None."""
    text = content.strip()
    for tag, name, field in (("bash", "run_bash", "command"), ("done", "finish_task", "summary")):
        body = text_action_body(text, tag)
        if body is None:
            continue
        value = body.strip()
        if any(t in value.lower() for t in ("<bash>", "</bash>", "<done>", "</done>")):
            raise ValueError("Multiple or nested actions are not supported")
        return name, validate_action(name, json.dumps({field: value}))
    return None


def handle_text_action(content: str, messages: list[dict], execute, verify) -> bool:
    """Dispatch a text action, appending feedback; return True on accepted completion."""
    # Only a complete, single tag is an action. Prose, fences and reasoning
    # are never interpreted as commands or implicit success.
    messages.append({"role": "assistant", "content": content})
    output = "Use one explicit run_bash/finish_task call or one <bash>/<done> action."
    try:
        action = parse_text_action(content)
        if action is not None:
            completed, output = dispatch(*action, execute, verify)
            if completed:
                return True
    except ValueError as exc:
        output = f"Invalid action: {exc}"
    messages.append({"role": "user", "content": output})
    return False


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
            completed = handle_tool_calls(content, calls, messages, execute, verify)
        else:
            completed = handle_text_action(content, messages, execute, verify)
        if completed:
            return Outcome.COMPLETED
    print(f"[!] Reached maximum iterations ({limit}).")
    return Outcome.EXHAUSTED


def main() -> int:
    if OpenAI is None:
        raise RuntimeError("Install requirements-controller.txt to use inference")
    client = OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=LLM_TIMEOUT, max_retries=2)
    model = get_active_model()
    print(f"[*] Connected to LM Studio via {BASE_URL}")
    print(f"[*] Using model: {model}")

    if len(sys.argv) > 1:
        return run_task_cycles(client, model, " ".join(sys.argv[1:]))
    run_interactive(client, model)
    return 0


def run_recovered(client: OpenAI, model: str, task: str) -> Outcome:
    """Run one task, reporting unexpected harness errors as a failed outcome."""
    try:
        return run_agent_loop(client, model, task)
    except Exception as exc:
        print(f"\n[!] Unexpected error in task loop (recovered): {exc}", file=sys.stderr)
        return Outcome.FAILED


def run_task_cycles(client: OpenAI, model: str, task: str) -> int:
    """Run a task once, or repeatedly in continuous mode until it stops completing."""
    for cycle in itertools.count(1):
        if CONTINUOUS:
            print(f"\n================ CONTINUOUS CYCLE #{cycle} ================")
        outcome = run_recovered(client, model, task)
        print(f"[*] Run outcome: {outcome.name.lower()}")
        if not CONTINUOUS or outcome != Outcome.COMPLETED:
            return int(outcome)
        time.sleep(2.0)


def run_interactive(client: OpenAI, model: str) -> None:
    """Prompt for tasks until EOF or an explicit exit."""
    while True:
        try:
            task = input("\nEnter task for sandboxed agent (or 'exit' to quit): ").strip()
        except EOFError:
            print("\nExiting.")
            return
        if task.lower() in {"exit", "quit"}:
            return
        if task:
            run_recovered(client, model, task)


def interrupt(signum, frame):
    """Unwind active command cleanup on container termination."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupt)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(Outcome.INTERRUPTED)
