const assert = require('node:assert/strict');
const {add} = require('/home/agent/workspace/calculator.js');
assert.equal(add(2, 2), 4);
assert.equal(add(-1, 1), 0);
console.log('Protected Node checks passed');
