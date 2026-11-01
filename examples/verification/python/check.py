import sys
sys.path.insert(0, '/home/agent/workspace')
from calculator import add
assert add(2, 2) == 4
assert add(-1, 1) == 0
print('Protected Python checks passed')
