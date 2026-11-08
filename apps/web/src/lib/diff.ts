export type LineKind = "add" | "del" | "ctx" | "hunk";
export interface DiffLine { kind: LineKind; text: string; oldNo: number | null; newNo: number | null }
export interface FileDiff { path: string; status: "modified" | "created" | "deleted" | "renamed"; added: number; deleted: number; lines: DiffLine[] }

/** Display-only unified-diff parser. It never decides anything security-relevant. */
export function parseUnifiedDiff(patch: string): FileDiff[] {
  const files: FileDiff[] = [];
  let cur: FileDiff | null = null;
  let oldNo = 0, newNo = 0, inHunk = false;
  for (const line of patch.split("\n")) {
    if (line.startsWith("diff --git ")) {
      const m = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
      cur = { path: m ? m[2] : line.slice(11), status: "modified", added: 0, deleted: 0, lines: [] };
      files.push(cur); inHunk = false; continue;
    }
    if (!cur) continue;
    if (!inHunk) {
      if (line.startsWith("new file mode")) cur.status = "created";
      else if (line.startsWith("deleted file mode")) cur.status = "deleted";
      else if (line.startsWith("rename to ")) { cur.status = "renamed"; cur.path = line.slice(10); }
    }
    const h = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if (h) { oldNo = +h[1]; newNo = +h[2]; inHunk = true; cur.lines.push({ kind: "hunk", text: line, oldNo: null, newNo: null }); continue; }
    if (!inHunk) continue;
    if (line.startsWith("+")) { cur.added++; cur.lines.push({ kind: "add", text: line.slice(1), oldNo: null, newNo: newNo++ }); }
    else if (line.startsWith("-")) { cur.deleted++; cur.lines.push({ kind: "del", text: line.slice(1), oldNo: oldNo++, newNo: null }); }
    else if (line.startsWith(" ")) cur.lines.push({ kind: "ctx", text: line.slice(1), oldNo: oldNo++, newNo: newNo++ });
  }
  return files;
}
