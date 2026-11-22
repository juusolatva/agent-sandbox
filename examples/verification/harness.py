import sys
sys.path.insert(0, '/home/agent/workspace')
from calculator import add
for line in sys.stdin:
    left, right = map(int, line.split())
    print(f'answer: {add(left, right)}', flush=True)
