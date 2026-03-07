import Link from "next/link";
import { notFound } from "next/navigation";

import { getEvaluationRun } from "@/lib/db";

export const dynamic = "force-dynamic";

function formatDate(value: string | null): string {
  if (!value) {
    return "n/a";
  }
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(new Date(value));
}

function metric(summary: Record<string, unknown>, key: string): string {
  const value = summary[key];
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(3);
  }
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  return "n/a";
}

function rubricScore(payload: Record<string, unknown>): string {
  const rubric =
    payload.rubric && typeof payload.rubric === "object"
      ? (payload.rubric as Record<string, unknown>)
      : {};
  const score = rubric.score;
  if (typeof score === "number") {
    return Number.isInteger(score) ? String(score) : score.toFixed(2);
  }
  return "n/a";
}

type RunPageProps = {
  params: Promise<{ runId: string }>;
};

export default async function RunPage({ params }: RunPageProps) {
  const { runId } = await params;
  const run = await getEvaluationRun(runId);
  if (!run) {
    notFound();
  }

  const summary = run.summary;
  const report = run.report;
  const results = Array.isArray(report.results) ? report.results : [];
  const champion =
    report.champion && typeof report.champion === "object"
      ? (report.champion as Record<string, unknown>)
      : null;
  const candidate =
    report.candidate && typeof report.candidate === "object"
      ? (report.candidate as Record<string, unknown>)
      : null;

  return (
    <main className="shell">
      <section className="hero compact-hero">
        <div>
          <p className="eyebrow">{run.lane} lane</p>
          <h1>Evaluation run {run.id.slice(0, 8)}</h1>
          <p className="lede">
            Created {formatDate(run.createdAt)} UTC · recommendation{" "}
            {metric(summary, "recommendation")} · confidence{" "}
            {metric(summary, "confidence")}
          </p>
        </div>
        <Link className="inline-link" href="/">
          Back to overview
        </Link>
      </section>

      <section className="card-grid">
        <section className="panel panel-amber">
          <p className="eyebrow">Run metadata</p>
          <div className="detail-grid">
            <div>
              <span className="stat-label">Generated</span>
              <strong>{formatDate(run.generatedAt)}</strong>
            </div>
            <div>
              <span className="stat-label">Source</span>
              <strong>{run.source}</strong>
            </div>
            <div>
              <span className="stat-label">Count</span>
              <strong>{run.count}</strong>
            </div>
            <div>
              <span className="stat-label">Workflow mode</span>
              <strong>{run.workflowMode || "n/a"}</strong>
            </div>
          </div>
        </section>

        <section className="panel panel-teal">
          <p className="eyebrow">Summary</p>
          <div className="detail-grid">
            {Object.entries(summary).slice(0, 8).map(([key, value]) => (
              <div key={key}>
                <span className="stat-label">{key}</span>
                <strong>{typeof value === "object" ? JSON.stringify(value) : String(value)}</strong>
              </div>
            ))}
          </div>
        </section>
      </section>

      {run.lane === "benchmark" ? (
        <section className="panel">
          <div className="section-head">
            <div>
              <p className="eyebrow">Case breakdown</p>
              <h2>Champion vs candidate</h2>
            </div>
            <p className="muted">
              Showing {results.length} benchmark cases from the persisted report.
            </p>
          </div>
          <div className="runs-table">
            <div className="runs-row runs-head">
              <span>Case</span>
              <span>Expected</span>
              <span>Champion</span>
              <span>Candidate</span>
              <span>Delta</span>
              <span>Pass</span>
            </div>
            {results.map((result, index) => {
              const row =
                result && typeof result === "object"
                  ? (result as Record<string, unknown>)
                  : {};
              const championRow =
                row.champion && typeof row.champion === "object"
                  ? (row.champion as Record<string, unknown>)
                  : {};
              const candidateRow =
                row.candidate && typeof row.candidate === "object"
                  ? (row.candidate as Record<string, unknown>)
                  : {};
              const championRubric =
                championRow.rubric && typeof championRow.rubric === "object"
                  ? (championRow.rubric as Record<string, unknown>)
                  : {};
              const candidateRubric =
                candidateRow.rubric && typeof candidateRow.rubric === "object"
                  ? (candidateRow.rubric as Record<string, unknown>)
                  : {};
              const championScore = Number(championRubric.score || 0);
              const candidateScore = Number(candidateRubric.score || 0);
              return (
                <div className="runs-row" key={String(row.case_id || index)}>
                  <span>{String(row.case_id || index + 1)}</span>
                  <span>{String(row.expected_decision || "n/a")}</span>
                  <span>{championScore}</span>
                  <span>{candidateScore}</span>
                  <span>{candidateScore - championScore}</span>
                  <span>
                    {String(Boolean(candidateRow.case_pass))}/{String(
                      Boolean(championRow.case_pass),
                    )}
                  </span>
                </div>
              );
            })}
          </div>
        </section>
      ) : null}

      {run.lane === "shadow" && champion && candidate ? (
        <section className="card-grid">
          <section className="panel panel-amber">
            <p className="eyebrow">Champion</p>
            <h2>{String(champion.decision || "skip")}</h2>
            <div className="detail-grid">
              <div>
                <span className="stat-label">Rubric score</span>
                <strong>{rubricScore(champion)}</strong>
              </div>
              <div>
                <span className="stat-label">Release passed</span>
                <strong>{metric(champion, "release_passed")}</strong>
              </div>
              <div>
                <span className="stat-label">Mode</span>
                <strong>{String(champion.mode || "n/a")}</strong>
              </div>
            </div>
          </section>

          <section className="panel panel-teal">
            <p className="eyebrow">Candidate</p>
            <h2>{String(candidate.decision || "skip")}</h2>
            <div className="detail-grid">
              <div>
                <span className="stat-label">Rubric score</span>
                <strong>{rubricScore(candidate)}</strong>
              </div>
              <div>
                <span className="stat-label">Release passed</span>
                <strong>{metric(candidate, "release_passed")}</strong>
              </div>
              <div>
                <span className="stat-label">Mode</span>
                <strong>{String(candidate.mode || "n/a")}</strong>
              </div>
            </div>
          </section>
        </section>
      ) : null}

      {run.lane === "shadow" && champion && candidate ? (
        <section className="card-grid">
          {(
            [
            ["Champion selection", champion.selected_items],
            ["Candidate selection", candidate.selected_items],
            ] as Array<[string, unknown]>
          ).map(([title, value]) => {
            const items = Array.isArray(value)
              ? value.filter(
                  (item): item is Record<string, unknown> =>
                    Boolean(item && typeof item === "object"),
                )
              : [];
            return (
              <section className="panel" key={title}>
                <p className="eyebrow">{title}</p>
                <h2>{items.length} items</h2>
                <div className="selection-list">
                  {items.length ? (
                    items.map((item, index) => (
                      <article className="selection-item" key={String(item.id || index)}>
                        <strong>{String(item.title || item.url || `item-${index + 1}`)}</strong>
                        <span className="muted">{String(item.url || "no url")}</span>
                      </article>
                    ))
                  ) : (
                    <p className="muted">No items selected.</p>
                  )}
                </div>
              </section>
            );
          })}
        </section>
      ) : null}

      <section className="panel">
        <details>
          <summary>Raw persisted report</summary>
          <pre className="raw-json">
            {JSON.stringify(
              {
                ...report,
                notes: run.notes,
              },
              null,
              2,
            )}
          </pre>
        </details>
      </section>
    </main>
  );
}
