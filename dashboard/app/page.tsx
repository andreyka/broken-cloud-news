import Image from "next/image";
import Link from "next/link";

import {
  type EvaluationRunSummary,
  type SimulationSummary,
  type WorkflowQueueAlert,
  type WorkflowJobSummary,
  type WorkflowQueueSnapshot,
  getLatestEvaluationRunByLane,
  getLatestSimulationSummary,
  getRecentEvaluationRuns,
  getWorkflowQueueSnapshot,
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
  if (lane === "publish") {
    return "Publish";
  }
  if (lane === "collection") {
    return "Collection";
  }
  if (lane === "analysis") {
    return "Analysis";
  }
  if (lane === "evaluation") {
    return "Evaluation";
  }
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

function workflowStatusTone(status: WorkflowJobSummary["status"]): string {
  if (status === "leased") {
    return "running";
  }
  if (status === "completed") {
    return "completed";
  }
  if (status === "failed" || status === "canceled") {
    return "failed";
  }
  return "queued";
}

function workflowHeadline(snapshot: WorkflowQueueSnapshot): string {
  if (snapshot.alerts.some((alert) => alert.level === "critical")) {
    return "Queue attention required";
  }
  if (snapshot.leasedCount > 0) {
    return "Workers active";
  }
  if (snapshot.queuedCount > 0) {
    return "Queue backlog";
  }
  return "Queue healthy";
}

function queueAlertTone(alert: WorkflowQueueAlert): string {
  if (alert.level === "critical") {
    return "failed";
  }
  if (alert.level === "warn") {
    return "queued";
  }
  return "completed";
}

type QueueActionNotice = {
  tone: "completed" | "failed";
  message: string;
};

type SearchParams = Record<string, string | string[] | undefined>;

function searchParamValue(
  params: SearchParams,
  key: string,
): string | null {
  const value = params[key];
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  if (Array.isArray(value)) {
    const first = value.find(
      (entry) => typeof entry === "string" && entry.trim().length > 0,
    );
    return first || null;
  }
  return null;
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

function QueueActionCard({
  target,
  title,
  copy,
  buttonLabel,
  footnote,
  paused,
}: {
  target: "publish" | "analysis" | "collection";
  title: string;
  copy: string;
  buttonLabel: string;
  footnote: string;
  paused: boolean;
}) {
  return (
    <form action="/api/workflows/rerun" className="control-action-card" method="post">
      <input type="hidden" name="target" value={target} />
      <input type="hidden" name="redirectTo" value="/" />
      <div className="control-action-head">
        <span className={`lane-pill lane-${target === "collection" ? "collection" : target}`}>
          {title}
        </span>
        {paused ? (
          <span className="run-state run-state-failed">lane paused</span>
        ) : (
          <span className="run-state run-state-completed">ready</span>
        )}
      </div>
      <p className="lane-copy">{copy}</p>
      <button className="control-button" type="submit">
        {buttonLabel}
      </button>
      <span className="lane-meta">
        {paused ? "Lane is paused. The queued job will wait for resume." : footnote}
      </span>
    </form>
  );
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

function QueueCard({
  snapshot,
  notice,
}: {
  snapshot: WorkflowQueueSnapshot;
  notice: QueueActionNotice | null;
}) {
  const lanes = new Map(snapshot.lanes.map((lane) => [lane.lane, lane]));
  const publishLane = lanes.get("publish");
  const analysisLane = lanes.get("analysis");
  const collectionLane = lanes.get("collection");

  return (
    <section className="panel">
      <div className="section-head">
        <div>
          <p className="eyebrow">Workflow Queue</p>
          <h2>{workflowHeadline(snapshot)}</h2>
        </div>
        <p className="muted">
          Dedicated scheduler and lane-scoped workers now own publish,
          collection, analysis, and evaluation execution.
        </p>
      </div>

      {notice ? (
        <div className={`queue-notice queue-notice-${notice.tone}`}>
          <span className={`run-state run-state-${notice.tone}`}>{notice.tone}</span>
          <strong>{notice.message}</strong>
        </div>
      ) : null}

      <div className="metric-grid queue-metric-grid">
        <div className="metric-cell">
          <span className="metric-label">Queued</span>
          <strong className="metric-value">{String(snapshot.queuedCount)}</strong>
        </div>
        <div className="metric-cell">
          <span className="metric-label">Leased</span>
          <strong className="metric-value">{String(snapshot.leasedCount)}</strong>
        </div>
        <div className="metric-cell">
          <span className="metric-label">Publish backlog</span>
          <strong className="metric-value">{String(snapshot.publishBacklogCount)}</strong>
        </div>
        <div className="metric-cell">
          <span className="metric-label">Evaluation backlog</span>
          <strong className="metric-value">
            {String(snapshot.evaluationBacklogCount)}
          </strong>
        </div>
        <div className="metric-cell">
          <span className="metric-label">Completed 24h</span>
          <strong className="metric-value">
            {String(snapshot.completed24hCount)}
          </strong>
        </div>
        <div className="metric-cell">
          <span className="metric-label">Failed 24h</span>
          <strong className="metric-value">{String(snapshot.failed24hCount)}</strong>
        </div>
      </div>

      <section className="control-actions-shell">
        <div className="section-head">
          <div>
            <p className="eyebrow">Operator actions</p>
            <h2>Manual workflow reruns</h2>
          </div>
          <p className="muted">
            These controls enqueue real workflow jobs. Workers pick them up using
            the same queue policy as scheduled runs.
          </p>
        </div>
        <div className="control-actions-grid">
          <QueueActionCard
            target="publish"
            title="Publish"
            copy="Queue an immediate rerun of the regular daily briefing pipeline against the current analyzed pool."
            buttonLabel="Queue publish rerun"
            footnote="Creates one publish job."
            paused={Boolean(publishLane?.paused)}
          />
          <QueueActionCard
            target="analysis"
            title="Analysis"
            copy="Rerun the analyst step so newly collected items get scored and normalized without waiting for the next interval."
            buttonLabel="Queue analysis rerun"
            footnote="Creates one analysis job."
            paused={Boolean(analysisLane?.paused)}
          />
          <QueueActionCard
            target="collection"
            title="Collection"
            copy="Fan out all configured collector workflows now: GHSA, RSS, Reddit, and Twitter/X."
            buttonLabel="Queue collection rerun"
            footnote="Creates four collection jobs."
            paused={Boolean(collectionLane?.paused)}
          />
        </div>
      </section>

      <div className="card-grid queue-detail-grid">
        <section className="panel panel-amber">
          <p className="eyebrow">Alerts</p>
          {snapshot.alerts.length === 0 ? (
            <p className="lane-copy">No queue alerts. All lanes are within their age budgets.</p>
          ) : (
            <div className="selection-list">
              {snapshot.alerts.map((alert, index) => (
                <div className="selection-item" key={`${alert.lane || "global"}-${index}`}>
                  <span className={`run-state run-state-${queueAlertTone(alert)}`}>
                    {alert.level}
                  </span>
                  <strong>{alert.lane ? laneTitle(alert.lane) : "Global"}</strong>
                  <span className="lane-meta">{alert.message}</span>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="panel panel-teal">
          <p className="eyebrow">Lane status</p>
          <div className="lane-status-grid">
            {snapshot.lanes.map((lane) => (
              <article className="lane-status-card" key={lane.lane}>
                <div className="lane-status-head">
                  <span className={`lane-pill lane-${lane.lane}`}>{laneTitle(lane.lane)}</span>
                  <span className={`run-state run-state-${lane.paused ? "failed" : "completed"}`}>
                    {lane.paused ? "paused" : "active"}
                  </span>
                </div>
                <div className="lane-status-metrics">
                  <div>
                    <span className="stat-label">Queued</span>
                    <strong>{lane.queuedCount}</strong>
                  </div>
                  <div>
                    <span className="stat-label">Leased</span>
                    <strong>{lane.leasedCount}</strong>
                  </div>
                  <div>
                    <span className="stat-label">Oldest queued</span>
                    <strong>{formatDate(lane.oldestQueuedAt)}</strong>
                  </div>
                  <div>
                    <span className="stat-label">Oldest leased</span>
                    <strong>{formatDate(lane.oldestLeasedAt)}</strong>
                  </div>
                </div>
                <p className="lane-status-note">{lane.reason || "Workers active and accepting jobs."}</p>
              </article>
            ))}
          </div>
        </section>
      </div>

      <div className="jobs-table">
        <div className="jobs-row runs-head">
          <span>Lane</span>
          <span>Status</span>
          <span>Created</span>
          <span>Workflow</span>
          <span>Attempts</span>
          <span>Deadline</span>
          <span>Signal</span>
          <span></span>
        </div>
        {snapshot.jobs.map((job) => {
          const signal =
            job.status === "failed" || job.status === "canceled"
              ? job.errorMessage || job.status
              : `${job.jobType} · ${job.source}`;
          return (
            <div className="jobs-row" key={job.id}>
              <span className={`lane-pill lane-${job.lane}`}>{laneTitle(job.lane)}</span>
              <span className={`run-state run-state-${workflowStatusTone(job.status)}`}>
                {job.status}
              </span>
              <span>{formatDate(job.createdAt)}</span>
              <span>{job.workflowId || job.jobType}</span>
              <span>
                {job.attemptCount}/{job.maxAttempts}
              </span>
              <span>{formatDate(job.deadlineAt)}</span>
              <span>{signal}</span>
              <span>
                <Link className="inline-link" href={`/jobs/${job.id}`}>
                  View
                </Link>
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

type HomePageProps = {
  searchParams?: Promise<SearchParams>;
};

export default async function Home({ searchParams }: HomePageProps) {
  const resolvedSearchParams = (await searchParams) || {};
  const [benchmark, shadow, replay, runs, queue] = await Promise.all([
    getLatestEvaluationRunByLane("benchmark"),
    getLatestEvaluationRunByLane("shadow"),
    getLatestSimulationSummary(),
    getRecentEvaluationRuns(18),
    getWorkflowQueueSnapshot(8),
  ]);

  const heroSignal =
    metric(benchmark?.summary ?? {}, "recommendation") !== "n/a"
      ? metric(benchmark?.summary ?? {}, "recommendation")
      : metric(shadow?.summary ?? {}, "recommendation") !== "n/a"
        ? metric(shadow?.summary ?? {}, "recommendation")
        : metric(replayDecision(replay?.summary ?? {}), "recommendation");

  const noticeMessage = searchParamValue(
    resolvedSearchParams,
    "queueActionMessage",
  );
  const noticeStatus = searchParamValue(
    resolvedSearchParams,
    "queueActionStatus",
  );
  const queueNotice: QueueActionNotice | null = noticeMessage
    ? {
        tone: noticeStatus === "success" ? "completed" : "failed",
        message: noticeMessage,
      }
    : null;

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

      <QueueCard notice={queueNotice} snapshot={queue} />

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
