import { parseUnifiedDiff } from "../lib/diff";

export function DiffView({ patch }: { patch: string }) {
  const files = parseUnifiedDiff(patch);
  if (files.length === 0) return <p className="muted">Patch could not be parsed as a unified diff.</p>;
  return (
    <div className="diff">
      {files.map((f) => (
        <details key={f.path} open>
          <summary>
            <code>{f.path}</code> <span className="muted">{f.status}</span>
            <span className="add">+{f.added}</span> <span className="del">−{f.deleted}</span>
          </summary>
          <table><tbody>
            {f.lines.map((l, i) => (
              <tr key={i} className={`dl dl-${l.kind}`}>
                <td className="no">{l.oldNo ?? ""}</td><td className="no">{l.newNo ?? ""}</td>
                <td className="sign">{l.kind === "add" ? "+" : l.kind === "del" ? "−" : ""}</td>
                <td><pre>{l.text}</pre></td>
              </tr>
            ))}
          </tbody></table>
        </details>
      ))}
    </div>
  );
}
