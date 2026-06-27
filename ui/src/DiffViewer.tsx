// DiffViewer (v0.9.0 Step 6) — a dependency-free unified-diff renderer with
// inline Risk Engine annotations. Parses artifacts/diff.patch into files →
// hunks → lines and overlays review.json concerns on the matching file
// sections, so the human reviewer sees the code and the risk signals in one
// place instead of context-switching to an IDE.
//
// No external diff library is used (the codebase keeps the npm dep set
// minimal: react + react-dom only). A unified diff is straightforward to
// parse: `diff --git` file headers, `--- `/`+++ ` paths, `@@ ` hunk
// headers, and ` `/`+`/`-` content lines.

import { useMemo } from "react";
import type { ReviewResult } from "./api";

type LineType = "context" | "add" | "remove";

interface DiffLine {
  type: LineType;
  text: string;
  oldNo: number | null;
  newNo: number | null;
}

interface DiffHunk {
  header: string;
  lines: DiffLine[];
}

interface DiffFile {
  oldPath: string;
  newPath: string;
  hunks: DiffHunk[];
}

/** Parse a hunk header `@@ -oldStart,oldCount +newStart,newCount @@ ...`. */
function parseHunkHeader(line: string): { oldStart: number; newStart: number } {
  // Matches the two -/+ ranges. Counts are optional (default 1).
  const m = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
  if (!m) return { oldStart: 1, newStart: 1 };
  return { oldStart: Number(m[1]), newStart: Number(m[2]) };
}

/** Parse a unified diff patch into a list of files with hunks and lines. */
function parseDiff(patch: string): DiffFile[] {
  const lines = patch.split("\n");
  const files: DiffFile[] = [];
  let file: DiffFile | null = null;
  let hunk: DiffHunk | null = null;
  let oldNo = 0;
  let newNo = 0;

  for (const raw of lines) {
    if (raw.startsWith("diff --git ")) {
      // Start a new file. Prefer the `b/` path (post-image).
      const m = raw.match(/^diff --git a\/(.*) b\/(.*)$/);
      const path = m ? m[2] : raw.slice("diff --git ".length).split(" ").pop() ?? "";
      file = { oldPath: path, newPath: path, hunks: [] };
      files.push(file);
      hunk = null;
      continue;
    }
    if (!file) continue;
    if (raw.startsWith("--- ")) {
      file.oldPath = raw.slice(4).replace(/^a\//, "");
      continue;
    }
    if (raw.startsWith("+++ ")) {
      file.newPath = raw.slice(4).replace(/^b\//, "");
      continue;
    }
    if (raw.startsWith("@@")) {
      const { oldStart, newStart } = parseHunkHeader(raw);
      oldNo = oldStart;
      newNo = newStart;
      hunk = { header: raw, lines: [] };
      file.hunks.push(hunk);
      continue;
    }
    if (!hunk) continue;
    // Content lines: ' ' context, '+' add, '-' remove. Anything else (e.g.
    // "\ No newline at end of file") is skipped — it isn't a content line.
    if (raw.startsWith("+")) {
      hunk.lines.push({ type: "add", text: raw.slice(1), oldNo: null, newNo: newNo++ });
    } else if (raw.startsWith("-")) {
      hunk.lines.push({ type: "remove", text: raw.slice(1), oldNo: oldNo++, newNo: null });
    } else if (raw.startsWith(" ")) {
      hunk.lines.push({ type: "context", text: raw.slice(1), oldNo: oldNo++, newNo: newNo++ });
    }
    // Lines not starting with ' '/'+'/'-' (e.g. the "\ No newline" marker)
    // are ignored — they carry no displayable content.
  }
  return files;
}

/** Concerns that reference a file path (by full path or basename substring). */
function concernsForFile(concerns: string[], filePath: string): string[] {
  const base = filePath.split("/").pop() ?? filePath;
  return concerns.filter((c) => c.includes(filePath) || c.includes(base));
}

const RISK_CLASS: Record<string, string> = {
  low: "risk-low",
  medium: "risk-medium",
  high: "risk-high",
};

interface DiffViewerProps {
  diff: string;
  review?: ReviewResult | null;
}

export function DiffViewer({ diff, review }: DiffViewerProps) {
  const files = useMemo(() => parseDiff(diff), [diff]);

  if (!diff.trim()) {
    return <div className="diff-empty">No diff captured for this task.</div>;
  }
  if (!files.length) {
    return (
      <div className="diff-empty">Diff present but could not be parsed — showing raw patch.</div>
    );
  }

  const concerns = review?.concerns ?? [];

  return (
    <div className="diff-viewer">
      {files.map((file, fi) => {
        const fileConcerns = concernsForFile(concerns, file.newPath || file.oldPath);
        const added = file.hunks.reduce((n, h) => n + h.lines.filter((l) => l.type === "add").length, 0);
        const removed = file.hunks.reduce(
          (n, h) => n + h.lines.filter((l) => l.type === "remove").length,
          0,
        );
        return (
          <div key={fi} className="diff-file">
            <div className="diff-file-header">
              <span className="diff-file-path">{file.newPath || file.oldPath}</span>
              <span className="diff-file-stat">
                <span className="diff-stat-add">+{added}</span>{" "}
                <span className="diff-stat-del">-{removed}</span>
              </span>
            </div>
            {fileConcerns.length > 0 && (
              <ul className="diff-concerns">
                {fileConcerns.map((c, ci) => (
                  <li key={ci} className="diff-concern">
                    ⚠ {c}
                  </li>
                ))}
              </ul>
            )}
            {file.hunks.map((hunk, hi) => (
              <div key={hi} className="diff-hunk">
                <div className="diff-hunk-header">{hunk.header}</div>
                <table className="diff-lines">
                  <tbody>
                    {hunk.lines.map((line, li) => (
                      <tr key={li} className={`diff-line diff-line-${line.type}`}>
                        <td className="diff-line-no diff-line-old">{line.oldNo ?? ""}</td>
                        <td className="diff-line-no diff-line-new">{line.newNo ?? ""}</td>
                        <td className="diff-line-sign">{line.type === "add" ? "+" : line.type === "remove" ? "-" : " "}</td>
                        <td className="diff-line-text">
                          <pre>{line.text}</pre>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

/** Compact risk-summary header for the Diff & Review section. */
export function RiskSummary({ review }: { review: ReviewResult | null }) {
  if (!review) return null;
  const riskClass = RISK_CLASS[review.risk] ?? "risk-low";
  return (
    <div className={`risk-summary ${riskClass}`}>
      <span className={`risk-badge ${riskClass}`}>Risk: {review.risk}</span>
      <span className="risk-rec">Recommendation: {review.recommendation}</span>
      {review.hard_block && <span className="risk-hard-block">⛔ hard block</span>}
      {review.changed_files.length > 0 && (
        <span className="risk-files">{review.changed_files.length} file(s) changed</span>
      )}
      {review.summary && <span className="risk-summary-text">{review.summary}</span>}
    </div>
  );
}
