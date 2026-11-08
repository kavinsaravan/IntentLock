/** Amounts are integer cents. Discounts round to the nearest cent, half up. */
export function applyDiscount(amountCents: number, percent: number): number {
  if (!Number.isInteger(amountCents) || amountCents < 0) throw new RangeError('amountCents must be a non-negative integer');
  if (percent < 0 || percent > 100) throw new RangeError('percent must be between 0 and 100');
  return amountCents - Math.round((amountCents * percent) / 100);
}

export function addTax(amountCents: number, ratePercent: number): number {
  if (!Number.isInteger(amountCents) || amountCents < 0) throw new RangeError('amountCents must be a non-negative integer');
  return amountCents + Math.round((amountCents * ratePercent) / 100);
}
