export function formatDigest(subjects: readonly string[]): string {
  return subjects.join(" | ");
}
