import Image from "next/image";
import Link from "next/link";

import {
  type EvaluationRunSummary,
  type SimulationSummary,
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

function asObject(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
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
  if (lane === "replay") {
    return "Replay";
  }
  return lane;
}

function runStateLabel(run: EvaluationRunSummary | null): string {
  if (!run) {
    return "Awaiting run";
  }
  if (run.status === "running") {
    return "Running now";
  }
  if (run.status === "failed") {
    return "Failed";
  }
  return `${formatDate(run.createdAt)} UTC`;
}

function statusLabel(run: EvaluationRunSummary): string {
  if (run.status === "running") {
    return "Running";
  }
  if (run.status === "failed") {
    return "Failed";
  }
  return "Completed";
}

function replayDecision(summary: Record<string, unknown>): Record<string, unknown> {
  return asObject(summary.decision);
}

function ReplayCard({ replay }: { replay: SimulationSummary }) {
  if (!replay) {
    return (
      <section className="lane-card lane-card-replay">
        <div className="lane-card-head">
          <span className="lane-pill lane-replay">Replay</span>
          <span className="lane-timestamp">Awaiting run</span>
        </div>
        <h2>No replay baseline yet</h2>
        <p className="lane-copy">
          Run <code>bcn simulate --store-db</code> to populate historical drift data.
        </p>
        <div className="metric-grid">
          <div className="metric-cell">
            <span className="metric-label">Confidence</span>
            <strong className="metric-value">n/a</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-label">Avg delta</span>
            <strong className="metric-value">n/a</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-label">Win rate</span>
            <strong className="metric-value">n/a</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-label">Sample count</span>
            <strong className="metric-value">0</strong>
          </div>
        </div>
      </section>
    );
  }

  const summary = replay.summary;
  const decision = replayDecision(summary);
  const winLoss = asObject(summary.win_loss);

  return (
    <section className="lane-card lane-card-replay">
      <div className="lane-card-head">
        <span className="lane-pill lane-replay">Replay</span>
        <span className="lane-timestamp">{formatDate(replay.createdAt)} UTC</span>
      </div>
      <h2>{metric(decision, "recommendation")}</h2>
      <p className="lane-copy">
        Historical regression lane across stored briefings. Use it to spot drift in
        the writer stack before promotion.
      </p>
      <div className="metric-grid">
        <div className="metric-cell">
          <span className="metric-label">Confidence</span>
          <strong className="metric-value">{metric(decision, "confidence")}</strong>
        </div>
        <div className="metric-cell">
          <span className="metric-label">Avg delta</span>
          <strong className="metric-value">{metric(summary, "avg_delta")}</strong>
        </div>
        <div className="metric-cell">
          <span className="metric-label">Win rate</span>
          <strong className="metric-value">{metric(winLoss, "win_rate_no_ties")}</strong>
        </div>
        <div className="metric-cell">
          <span className="metric-label">Sample count</span>
          <strong className="metric-value">{String(replay.count)}</strong>
        </div>
      </div>
    </section>
  );
}

function EvaluationLaneCard({
  lane,
  run,
}: {
  lane: "benchmark" | "shadow";
  run: EvaluationRunSummary | null;
}) {
  if (!run) {
    return (
      <section className={`lane-card lane-card-${lane}`}>
        <div className="lane-card-head">
          <span className={`lane-pill lane-${lane}`}>{laneTitle(lane)}</span>
          <span className="lane-timestamp">Awaiting run</span>
        </div>
        <h2>No persisted run yet</h2>
        <p className="lane-copy">
          {lane === "benchmark"
            ? "Persist a benchmark report before promoting a challenger."
            : "Enable scheduled shadow runs or execute one manually to monitor today's candidate."}
        </p>
        <div className="metric-grid">
          <div className="metric-cell">
            <span className="metric-label">Confidence</span>
            <strong className="metric-value">n/a</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-label">Cases</span>
            <strong className="metric-value">0</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-label">
              {lane === "benchmark" ? "Candidate pass" : "Overlap"}
            </span>
            <strong className="metric-value">n/a</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-label">
              {lane === "benchmark" ? "Champion pass" : "Score delta"}
            </span>
            <strong className="metric-value">n/a</strong>
          </div>
        </div>
      </section>
    );
  }

  if (run.status === "running") {
    return (
      <section className={`lane-card lane-card-${lane}`}>
        <div className="lane-card-head">
          <span className={`lane-pill lane-${lane}`}>{laneTitle(lane)}</span>
          <div className="lane-head-meta">
            <span className="run-state run-state-running">Running</span>
            <span className="lane-timestamp">{formatDate(run.createdAt)} UTC</span>
          </div>
        </div>
        <h2>Run in progress</h2>
        <p className="lane-copy">
          {lane === "benchmark"
            ? "Champion and challenger are being graded against the benchmark pack."
            : "Champion and challenger are being generated against the live item pool."}
        </p>
        <div className="metric-grid">
          <div className="metric-cell">
            <span className="metric-label">Status</span>
            <strong className="metric-value">running</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-label">Started</span>
            <strong className="metric-value">{formatDate(run.createdAt)}</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-label">Source</span>
            <strong className="metric-value">{run.source}</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-label">Mode</span>
            <strong className="metric-value">{run.workflowMode || "n/a"}</strong>
          </div>
        </div>
        <div className="lane-footer">
          <span className="lane-meta">No summary yet. This row will update on completion.</span>
          <Link className="inline-link" href={`/runs/${run.id}`}>
            Open run
          </Link>
        </div>
      </section>
    );
  }

  if (run.status === "failed") {
    return (
      <section className={`lane-card lane-card-${lane}`}>
        <div className="lane-card-head">
          <span className={`lane-pill lane-${lane}`}>{laneTitle(lane)}</span>
          <div className="lane-head-meta">
            <span className="run-state run-state-failed">Failed</span>
            <span className="lane-timestamp">{formatDate(run.createdAt)} UTC</span>
          </div>
        </div>
        <h2>Run failed</h2>
        <p className="lane-copy">
          {run.errorMessage || "The latest run did not complete. Open the detail page for the failure record."}
        </p>
        <div className="metric-grid">
          <div className="metric-cell">
            <span className="metric-label">Status</span>
            <strong className="metric-value">failed</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-label">Started</span>
            <strong className="metric-value">{formatDate(run.createdAt)}</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-label">Finished</span>
            <strong className="metric-value">{formatDate(run.finishedAt)}</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-label">Source</span>
            <strong className="metric-value">{run.source}</strong>
          </div>
        </div>
        <div className="lane-footer">
          <span className="lane-meta">Latest evaluation record ended in failure.</span>
          <Link className="inline-link" href={`/runs/${run.id}`}>
            Open run
          </Link>
        </div>
      </section>
    );
  }

  const summary = run.summary;
  const detailLabel =
    lane === "benchmark" ? "Candidate pass" : "Selection overlap";
  const detailValue =
    lane === "benchmark"
      ? metric(summary, "candidate_case_pass_rate")
      : metric(summary, "selection_overlap_ratio");
  const secondaryLabel =
    lane === "benchmark" ? "Champion pass" : "Score delta";
  const secondaryValue =
    lane === "benchmark"
      ? metric(summary, "champion_case_pass_rate")
      : metric(summary, "score_delta");

  return (
    <section className={`lane-card lane-card-${lane}`}>
      <div className="lane-card-head">
        <span className={`lane-pill lane-${lane}`}>{laneTitle(lane)}</span>
        <div className="lane-head-meta">
          <span className="run-state run-state-completed">Completed</span>
          <span className="lane-timestamp">{formatDate(run.createdAt)} UTC</span>
        </div>
      </div>
      <h2>{metric(summary, "recommendation")}</h2>
      <p className="lane-copy">
        {lane === "benchmark"
          ? "Release gate for champion vs challenger on curated editorial cases."
          : "Live pre-publish challenger check against the current item pool."}
      </p>
      <div className="metric-grid">
        <div className="metric-cell">
          <span className="metric-label">Confidence</span>
          <strong className="metric-value">{metric(summary, "confidence")}</strong>
        </div>
        <div className="metric-cell">
          <span className="metric-label">Cases</span>
          <strong className="metric-value">{String(run.count)}</strong>
        </div>
        <div className="metric-cell">
          <span className="metric-label">{detailLabel}</span>
          <strong className="metric-value">{detailValue}</strong>
        </div>
        <div className="metric-cell">
          <span className="metric-label">{secondaryLabel}</span>
          <strong className="metric-value">{secondaryValue}</strong>
        </div>
      </div>
      <div className="lane-footer">
        <span className="lane-meta">
          {run.workflowMode ? `${run.workflowMode} · ` : ""}
          source {run.source}
        </span>
        <Link className="inline-link" href={`/runs/${run.id}`}>
          Open run
        </Link>
      </div>
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

  const heroSignal =
    metric(benchmark?.summary ?? {}, "recommendation") !== "n/a"
      ? metric(benchmark?.summary ?? {}, "recommendation")
      : metric(shadow?.summary ?? {}, "recommendation") !== "n/a"
        ? metric(shadow?.summary ?? {}, "recommendation")
        : metric(replayDecision(replay?.summary ?? {}), "recommendation");

  return (
    <main className="shell">
      <section className="hero">
        <div className="brand-lockup">
          <div className="brand-logo-frame">
            <Image
              src="/logo.png"
              alt="Broken Cloud News"
              width={160}
              height={160}
              className="brand-logo"
              priority
            />
          </div>
          <div>
            <p className="eyebrow">Broken Cloud News</p>
            <h1>Evaluation control room</h1>
            <p className="lede">
              Three aligned lanes for promotion safety: curated benchmark,
              pre-publish shadow, and historical replay drift.
            </p>
          </div>
        </div>
        <div className="hero-note">
          <span>Current control signal</span>
          <strong>{heroSignal || "hold"}</strong>
          <small>
            Benchmark {runStateLabel(benchmark)} · Shadow {runStateLabel(shadow)}
          </small>
        </div>
      </section>

      <section className="lane-grid">
        <EvaluationLaneCard lane="benchmark" run={benchmark} />
        <EvaluationLaneCard lane="shadow" run={shadow} />
        <ReplayCard replay={replay} />
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
            <span>Status</span>
            <span>Created</span>
            <span>Recommendation</span>
            <span>Confidence</span>
            <span>Primary signal</span>
            <span></span>
          </div>
          {runs.map((run) => {
            const summary = run.summary;
            const primarySignal =
              run.status === "running"
                ? "in progress"
                : run.status === "failed"
                  ? run.errorMessage || "failed"
                  : run.lane === "benchmark"
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
                <span className={`run-state run-state-${run.status}`}>
                  {statusLabel(run)}
                </span>
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
