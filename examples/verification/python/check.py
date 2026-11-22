"""Protected verification supervisor: owns the verdict; candidate code runs only in a child.

Usage: python3 -I check.py HARNESS [ARGS...]

The harness reads one "left right" case per line on stdin and answers each with
a line "answer: <add(left, right)>". Success requires every expected answer, in
order, checked here, so a candidate that exits early (even with status 0),
crashes, or answers wrongly fails. Other output lines are ignored.
"""
import subprocess
import sys

CASES = [(2, 2), (-1, 1), (0, 0), (40, 2), (-7, -8), (123456, 654321), (-1000000, 999999)]
TIMEOUT = 300


def main(harness):
    if not harness:
        print('usage: check.py HARNESS [ARGS...]', file=sys.stderr)
        return 2
    cases = ''.join(f'{left} {right}\n' for left, right in CASES)
    try:
        result = subprocess.run(harness, input=cases, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f'Protected check failed: harness did not run: {exc}')
        return 1
    answers = [line.removeprefix('answer:').strip() for line in result.stdout.splitlines()
               if line.startswith('answer:')]
    expected = [str(left + right) for left, right in CASES]
    if result.returncode != 0 or answers != expected:
        print(f'Protected check failed: exit {result.returncode}, '
              f'{len(answers)}/{len(expected)} answers, first mismatch: '
              f'{next((pair for pair in zip(expected, answers) if pair[0] != pair[1]), None)}')
        print(result.stderr[-2000:], end='')
        return 1
    print(f'Protected checks passed ({len(expected)} cases)')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
