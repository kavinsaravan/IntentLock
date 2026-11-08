import test from 'node:test';
import assert from 'node:assert/strict';
import {addTax, applyDiscount} from '../../src/payments/calculator.ts';

test('discount rounds to the nearest cent, half up', () => {
  assert.equal(applyDiscount(1005, 10), 904); // 100.5 -> 101 off
  assert.equal(applyDiscount(999, 15), 849); // 149.85 -> 150 off
  assert.equal(applyDiscount(100, 0), 100);
});
test('tax rounds to the nearest cent', () => {
  assert.equal(addTax(1999, 8), 2159); // 159.92 -> 160
});
test('invalid input is rejected', () => {
  assert.throws(() => applyDiscount(-1, 10), RangeError);
  assert.throws(() => applyDiscount(100, 101), RangeError);
});
