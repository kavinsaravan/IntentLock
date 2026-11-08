export function invoiceTotalCents(items: readonly number[]): number {
  return items.reduce((total, amount) => total + amount, 0);
}
