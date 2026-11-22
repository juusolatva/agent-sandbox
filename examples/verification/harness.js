const {add} = require('/home/agent/workspace/calculator.js');
const lines = require('node:fs').readFileSync(0, 'utf8').split('\n').filter(Boolean);
for (const line of lines) {
  const [left, right] = line.split(' ').map(Number);
  process.stdout.write(`answer: ${add(left, right)}\n`);
}
