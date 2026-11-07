import test from 'node:test';
import assert from 'node:assert/strict';
import {isSessionExpired, verifyToken} from '../../src/auth/session.ts';

test('timeout is measured in seconds', () => {
  assert.equal(isSessionExpired(0, 5000, 10), false);
  assert.equal(isSessionExpired(0, 10000, 10), true);
});
test('verification rejects an invalid token', () => {
  assert.equal(verifyToken('invalid'), false);
  assert.equal(verifyToken('demo-valid-token'), true);
});
