import Image from "next/image";
import Link from "next/link";
import { notFound } from "next/navigation";

import { getWorkflowJob } from "@/lib/db";

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
  return lane;
}

function runStateTone(status: string): string {
  if (status === "running" || status === "leased") {
    return "running";
  }
  if (status === "completed") {
    return "completed";
  }
  if (status === "failed" || status === "canceled" || status === "expired") {
    return "failed";
  }
  return "queued";
}

function formatJson(value: Record<string, unknown>): string {
  return JSON.stringify(value, null, 2);
}

type JobPageProps = {
  params: Promise<{ jobId: string }>;
};

export default async function WorkflowJobPage({ params }: JobPageProps) {
  const { jobId } = await params;
  const job = await getWorkflowJob(jobId);
  if (!job) {
    notFound();
  }

  return (
    <main className="shell">
      <section className="hero compact-hero">
        <div className="brand-lockup brand-lockup-compact">
          <div className="brand-logo-frame brand-logo-frame-compact">
            <Image
              src="/logo.png"
              alt="Broken Cloud News"
              width={144}
              height={144}
              className="brand-logo brand-logo-compact"
              priority
            />
          </div>
          <div>
            <p className="eyebrow">{laneTitle(job.lane)} workflow job</p>
            <h1>Job {job.id.slice(0, 8)}</h1>
            <p className="lede">
              Created {formatDate(job.createdAt)} UTC · status {job.status} · type {job.jobType}
            </p>
          </div>
        </div>
        <Link className="inline-link" href="/">
          Back to overview
        </Link>
      </section>

      <section className="card-grid">
        <section className="panel panel-amber">
          <p className="eyebrow">Job metadata</p>
          <div className="detail-grid">
            <div>
              <span className="stat-label">Lane</span>
              <strong className={`lane-pill lane-${job.lane}`}>{laneTitle(job.lane)}</strong>
            </div>
            <div>
              <span className="stat-label">Status</span>
              <strong className={`run-state run-state-${runStateTone(job.status)}`}>
                {job.status}
              </strong>
            </div>
            <div>
              <span className="stat-label">Workflow</span>
              <strong>{job.workflowId || "n/a"}</strong>
            </div>
            <div>
              <span className="stat-label">Priority</span>
              <strong>{job.priority}</strong>
            </div>
            <div>
              <span className="stat-label">Attempts</span>
              <strong>
                {job.attemptCount}/{job.maxAttempts}
              </strong>
            </div>
            <div>
              <span className="stat-label">Source</span>
              <strong>{job.source}</strong>
            </div>
            <div>
              <span className="stat-label">Available</span>
              <strong>{formatDate(job.availableAt)}</strong>
            </div>
            <div>
              <span className="stat-label">Deadline</span>
              <strong>{formatDate(job.deadlineAt)}</strong>
            </div>
            <div>
              <span className="stat-label">Lease owner</span>
              <strong>{job.leaseOwner || "n/a"}</strong>
            </div>
            <div>
              <span className="stat-label">Lease expires</span>
              <strong>{formatDate(job.leaseExpiresAt)}</strong>
            </div>
            <div>
              <span className="stat-label">Heartbeat</span>
              <strong>{formatDate(job.heartbeatAt)}</strong>
            </div>
            <div>
              <span className="stat-label">Finished</span>
              <strong>{formatDate(job.finishedAt)}</strong>
            </div>
          </div>
        </section>

        <section className="panel panel-teal">
          <p className="eyebrow">Lane control</p>
          <div className="detail-grid">
            <div>
              <span className="stat-label">Paused</span>
              <strong>{job.control?.paused ? "yes" : "no"}</strong>
            </div>
            <div>
              <span className="stat-label">Paused at</span>
              <strong>{formatDate(job.control?.pausedAt || null)}</strong>
            </div>
            <div>
              <span className="stat-label">Updated by</span>
              <strong>{job.control?.updatedBy || "n/a"}</strong>
            </div>
            <div>
              <span className="stat-label">Reason</span>
              <strong>{job.control?.reason || "n/a"}</strong>
            </div>
            <div>
              <span className="stat-label">Notes</span>
              <strong>{job.notes || "n/a"}</strong>
            </div>
          </div>
        </section>
      </section>

      {job.errorMessage ? (
        <section className="panel panel-amber">
          <p className="eyebrow">Failure</p>
          <h2>Workflow job ended with an operator-visible error</h2>
          <p className="lane-copy">{job.errorMessage}</p>
        </section>
      ) : null}

      <section className="card-grid">
        <section className="panel">
          <div className="section-head">
            <div>
              <p className="eyebrow">Payload</p>
              <h2>Queued input</h2>
            </div>
          </div>
          <pre className="raw-json">{formatJson(job.payload)}</pre>
        </section>
        <section className="panel">
          <div className="section-head">
            <div>
              <p className="eyebrow">State</p>
              <h2>Latest persisted state</h2>
            </div>
          </div>
          <pre className="raw-json">{formatJson(job.state)}</pre>
        </section>
      </section>

      <section className="card-grid">
        <section className="panel">
          <div className="section-head">
            <div>
              <p className="eyebrow">Result</p>
              <h2>Final job result</h2>
            </div>
          </div>
          <pre className="raw-json">{formatJson(job.result)}</pre>
        </section>
        <section className="panel">
          <div className="section-head">
            <div>
              <p className="eyebrow">Attempts</p>
              <h2>Worker attempt history</h2>
            </div>
            <p className="muted">{job.attempts.length} persisted attempt(s)</p>
          </div>
          <div className="runs-table">
            <div className="jobs-row runs-head lane-state-head">
              <span>Attempt</span>
              <span>Status</span>
              <span>Worker</span>
              <span>Started</span>
              <span>Finished</span>
              <span>Error</span>
              <span></span>
            </div>
            {job.attempts.map((attempt) => (
              <div className="jobs-row lane-state-row" key={attempt.id}>
                <span>{attempt.attemptNumber}</span>
                <span className={`run-state run-state-${runStateTone(attempt.status)}`}>
                  {attempt.status}
                </span>
                <span>{attempt.workerId}</span>
                <span>{formatDate(attempt.startedAt)}</span>
                <span>{formatDate(attempt.finishedAt)}</span>
                <span>{attempt.errorMessage || "n/a"}</span>
                <details>
                  <summary>State</summary>
                  <pre className="raw-json">{formatJson(attempt.stateAfter)}</pre>
                </details>
              </div>
            ))}
          </div>
        </section>
      </section>

      <section className="panel">
        <div className="section-head">
          <div>
            <p className="eyebrow">Artifacts</p>
            <h2>Checkpoint and progress payloads</h2>
          </div>
          <p className="muted">{job.artifacts.length} artifact(s)</p>
        </div>
        <div className="selection-list">
          {job.artifacts.map((artifact) => (
            <details className="selection-item" key={artifact.id}>
              <summary>
                {artifact.artifactKey} · {artifact.artifactType} · {formatDate(artifact.createdAt)}
              </summary>
              <pre className="raw-json">{formatJson(artifact.payload)}</pre>
            </details>
          ))}
        </div>
      </section>
    </main>
  );
}
