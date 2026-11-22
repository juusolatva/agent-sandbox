"""Protected verification supervisor: owns the verdict; candidate code runs only in a child.

Usage: python3 -I check.py HARNESS [ARGS...]

The harness reads one "left right" case per line on stdin and answers each with
a line "answer: <add(left, right)>". Success requires every expected answer, in
order, checked here, so a candidate that exits early (even with status 0),
crashes, or answers wrongly fails. Other output lines are ignored. Child output
is read incrementally and only bounded parts are kept, so a noisy candidate
cannot exhaust the supervisor's memory.
"""
import os
import selectors
import subprocess
import sys
import time

CASES = [(2, 2), (-1, 1), (0, 0), (40, 2), (-7, -8), (123456, 654321), (-1000000, 999999)]
TIMEOUT = 300
MAX_LINE = 4096      # Longer stdout lines are discarded (an answer line is short).
MAX_ANSWERS = len(CASES) + 1  # One extra proves "too many answers" without storing more.
ERROR_TAIL = 2000    # Diagnostics kept from the end of stderr.


class Collector:
    """Bounded view of the harness output: answer lines and a stderr tail."""

    def __init__(self):
        self.answers, self.partial, self.overlong, self.errors = [], b'', False, b''

    def stdout(self, chunk):
        lines = (self.partial + chunk).split(b'\n')
        self.partial = lines.pop()
        for line in lines:
            if not self.overlong:
                self.answer(line)
            self.overlong = False
        if len(self.partial) > MAX_LINE:
            self.partial, self.overlong = b'', True

    def answer(self, line):
        if line.startswith(b'answer:') and len(self.answers) < MAX_ANSWERS:
            self.answers.append(line.removeprefix(b'answer:').strip().decode('utf-8', 'replace'))

    def stderr(self, chunk):
        self.errors = (self.errors + chunk)[-ERROR_TAIL:]

    def finish(self):
        if self.partial and not self.overlong:
            self.answer(self.partial)


def run_harness(harness, collector):
    """Run the harness with the cases on stdin; return its exit status (124 on timeout)."""
    process = subprocess.Popen(harness, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        process.stdin.write(''.join(f'{left} {right}\n' for left, right in CASES).encode())
        process.stdin.close()
    except BrokenPipeError:
        pass  # The harness exited without reading; its missing answers fail the check.
    deadline = time.monotonic() + TIMEOUT
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, collector.stdout)
            selector.register(process.stderr, selectors.EVENT_READ, collector.stderr)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 124
                for key, _ in selector.select(min(remaining, 1)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if chunk:
                        key.data(chunk)
                    else:
                        selector.unregister(key.fileobj)
        collector.finish()
        return process.wait(timeout=max(.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return 124
    finally:
        process.kill()
        process.wait()


def main(harness):
    if not harness:
        print('usage: check.py HARNESS [ARGS...]', file=sys.stderr)
        return 2
    collector = Collector()
    try:
        code = run_harness(harness, collector)
    except OSError as exc:
        print(f'Protected check failed: harness did not run: {exc}')
        return 1
    expected = [str(left + right) for left, right in CASES]
    if code != 0 or collector.answers != expected:
        mismatch = next((pair for pair in zip(expected, collector.answers) if pair[0] != pair[1]), None)
        print(f'Protected check failed: exit {code}, {len(collector.answers)}/{len(expected)} answers, '
              f'first mismatch: {mismatch}')
        print(collector.errors.decode('utf-8', 'replace'), end='')
        return 1
    print(f'Protected checks passed ({len(expected)} cases)')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
