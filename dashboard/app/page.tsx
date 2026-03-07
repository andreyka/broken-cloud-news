import Link from "next/link";

import {
  type EvaluationRunSummary,
  getLatestEvaluationRunByLane,
  getLatestSimulationSummary,
  getRecentEvaluationRuns,
} from "@/lib/db";

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

function laneTitle(lane: string): string {
  if (lane === "benchmark") {
    return "Benchmark";
  }
  if (lane === "shadow") {
    return "Shadow";
  }
  return lane;
}

function replayRecommendation(summary: Record<string, unknown>): string {
  const decision =
    summary.decision && typeof summary.decision === "object"
      ? (summary.decision as Record<string, unknown>)
      : {};
  return String(decision.recommendation || "hold");
}

function SummaryCard({
  title,
  run,
  accent,
}: {
  title: string;
  run: EvaluationRunSummary | null;
  accent: "amber" | "teal";
}) {
  if (!run) {
    return (
      <section className={`panel panel-${accent}`}>
        <p className="eyebrow">{title}</p>
        <h2>No runs yet</h2>
        <p className="muted">This lane has not written a persisted run yet.</p>
      </section>
    );
  }

  const summary = run.summary;
  const detailLabel =
    run.lane === "benchmark" ? "Candidate pass rate" : "Selection overlap";
  const detailValue =
    run.lane === "benchmark"
      ? metric(summary, "candidate_case_pass_rate")
      : metric(summary, "selection_overlap_ratio");
  return (
    <section className={`panel panel-${accent}`}>
      <p className="eyebrow">{title}</p>
      <h2>{String(summary.recommendation || "hold")}</h2>
      <p className="muted">
        {formatDate(run.createdAt)} UTC
        {run.workflowMode ? ` · ${run.workflowMode}` : ""}
      </p>
      <div className="stat-grid">
        <div>
          <span className="stat-label">Confidence</span>
          <strong>{metric(summary, "confidence")}</strong>
        </div>
        <div>
          <span className="stat-label">Cases</span>
          <strong>{run.count}</strong>
        </div>
        <div>
          <span className="stat-label">{detailLabel}</span>
          <strong>{detailValue}</strong>
        </div>
        <div>
          <span className="stat-label">
            {run.lane === "benchmark" ? "Champion pass rate" : "Score delta"}
          </span>
          <strong>
            {run.lane === "benchmark"
              ? metric(summary, "champion_case_pass_rate")
              : metric(summary, "score_delta")}
          </strong>
        </div>
      </div>
      <Link className="inline-link" href={`/runs/${run.id}`}>
        Open run
      </Link>
    </section>
  );
}

export default async function Home() {
  const [benchmark, shadow, replay, runs] = await Promise.all([
    getLatestEvaluationRunByLane("benchmark"),
    getLatestEvaluationRunByLane("shadow"),
    getLatestSimulationSummary(),
    getRecentEvaluationRuns(18),
  ]);

  return (
    <main className="shell">
      <section className="hero">
        <div>
          <p className="eyebrow">Broken Cloud News</p>
          <h1>Evaluation control room</h1>
          <p className="lede">
            One place to check whether the challenger is safe, whether today’s
            shadow run stayed clean, and whether the replay lane is drifting.
          </p>
        </div>
        <div className="hero-note">
          <span>Replay lane</span>
          <strong>
            {replay ? replayRecommendation(replay.summary) : "n/a"}
          </strong>
          <small>
            {replay
              ? `latest run ${formatDate(replay.createdAt)} · count=${replay.count}`
              : "No simulation run stored yet"}
          </small>
        </div>
      </section>

      <section className="card-grid">
        <SummaryCard title="Latest benchmark" run={benchmark} accent="amber" />
        <SummaryCard title="Latest shadow" run={shadow} accent="teal" />
      </section>

      <section className="panel">
        <div className="section-head">
          <div>
            <p className="eyebrow">Recent runs</p>
            <h2>Persisted benchmark and shadow history</h2>
          </div>
          <p className="muted">Read-only view from Postgres on the SBC.</p>
        </div>
        <div className="runs-table">
          <div className="runs-row runs-head">
            <span>Lane</span>
            <span>Created</span>
            <span>Recommendation</span>
            <span>Confidence</span>
            <span>Primary signal</span>
            <span></span>
          </div>
          {runs.map((run) => {
            const summary = run.summary;
            const primarySignal =
              run.lane === "benchmark"
                ? `pass ${metric(summary, "candidate_case_pass_rate")} vs ${metric(
                    summary,
                    "champion_case_pass_rate",
                  )}`
                : `score ${metric(summary, "score_delta")} · overlap ${metric(
                    summary,
                    "selection_overlap_ratio",
                  )}`;
            return (
              <div className="runs-row" key={run.id}>
                <span className={`lane-pill lane-${run.lane}`}>{laneTitle(run.lane)}</span>
                <span>{formatDate(run.createdAt)}</span>
                <span>{metric(summary, "recommendation")}</span>
                <span>{metric(summary, "confidence")}</span>
                <span>{primarySignal}</span>
                <span>
                  <Link className="inline-link" href={`/runs/${run.id}`}>
                    View
                  </Link>
                </span>
              </div>
            );
          })}
        </div>
      </section>
    </main>
  );
}
