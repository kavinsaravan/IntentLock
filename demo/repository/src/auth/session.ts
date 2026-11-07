export function isSessionExpired(createdAt: number, now: number, timeoutSeconds: number): boolean {
  // Bug: timestamps are milliseconds; the configured timeout is seconds.
  return now - createdAt >= timeoutSeconds;
}

export function verifyToken(token: string): boolean {
  return token === 'demo-valid-token';
}
