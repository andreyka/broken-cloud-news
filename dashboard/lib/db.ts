import { Pool } from "pg";

type JsonObject = Record<string, unknown>;

export type EvaluationRunSummary = {
  id: string;
  lane: string;
  status: "running" | "completed" | "failed";
  createdAt: string;
  generatedAt: string | null;
  finishedAt: string | null;
  source: string;
  count: number;
  reportPath: string | null;
  packPath: string | null;
  workflowMode: string | null;
  errorMessage: string | null;
  candidateOverrides: JsonObject;
  summary: JsonObject;
};

export type EvaluationRunDetail = EvaluationRunSummary & {
  notes: string | null;
  report: JsonObject;
};

export type SimulationSummary = {
  id: string;
  createdAt: string;
  generatedAt: string | null;
  count: number;
  summary: JsonObject;
} | null;

export type WorkflowJobSummary = {
  id: string;
  lane: string;
  jobType: string;
  workflowId: string | null;
  status: "queued" | "leased" | "completed" | "failed" | "canceled";
  createdAt: string;
  availableAt: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  deadlineAt: string | null;
  source: string;
  priority: number;
  attemptCount: number;
  maxAttempts: number;
  errorMessage: string | null;
};

export type WorkflowLaneControlSummary = {
  lane: string;
  paused: boolean;
  reason: string | null;
  updatedBy: string | null;
  pausedAt: string | null;
  updatedAt: string | null;
};

export type WorkflowQueueLaneSummary = WorkflowLaneControlSummary & {
  queuedCount: number;
  leasedCount: number;
  oldestQueuedAt: string | null;
  oldestLeasedAt: string | null;
};

export type WorkflowQueueAlert = {
  level: "info" | "warn" | "critical";
  lane: string | null;
  message: string;
};

export type WorkflowQueueSnapshot = {
  queuedCount: number;
  leasedCount: number;
  failed24hCount: number;
  completed24hCount: number;
  publishBacklogCount: number;
  evaluationBacklogCount: number;
  lanes: WorkflowQueueLaneSummary[];
  alerts: WorkflowQueueAlert[];
  jobs: WorkflowJobSummary[];
};

export type WorkflowJobAttemptSummary = {
  id: number;
  attemptNumber: number;
  workerId: string;
  status: string;
  startedAt: string;
  finishedAt: string | null;
  errorMessage: string | null;
  stateBefore: JsonObject;
  stateAfter: JsonObject;
  result: JsonObject;
};

export type WorkflowJobArtifactSummary = {
  id: number;
  attemptId: number | null;
  artifactKey: string;
  artifactType: string;
  createdAt: string;
  updatedAt: string | null;
  payload: JsonObject;
};

export type WorkflowJobDetail = WorkflowJobSummary & {
  leaseOwner: string | null;
  leaseExpiresAt: string | null;
  heartbeatAt: string | null;
  notes: string | null;
  payload: JsonObject;
  state: JsonObject;
  result: JsonObject;
  control: WorkflowLaneControlSummary | null;
  attempts: WorkflowJobAttemptSummary[];
  artifacts: WorkflowJobArtifactSummary[];
};

const DEFAULT_WORKFLOW_LANES = [
  "publish",
  "collection",
  "analysis",
  "evaluation",
] as const;

const QUEUE_AGE_WARN_SECONDS: Record<string, number> = {
  publish: 15 * 60,
  collection: 3 * 60 * 60,
  analysis: 2 * 60 * 60,
  evaluation: 6 * 60 * 60,
};

const LEASE_AGE_WARN_SECONDS: Record<string, number> = {
  publish: 60 * 60,
  collection: 2 * 60 * 60,
  analysis: 90 * 60,
  evaluation: 8 * 60 * 60,
};

const globalForPg = globalThis as typeof globalThis & {
  __bcnDashboardPool?: Pool;
};

function getPool(): Pool {
  if (globalForPg.__bcnDashboardPool) {
    return globalForPg.__bcnDashboardPool;
  }
  const databaseUrl =
    process.env.DASHBOARD_DATABASE_URL ||
    process.env.DATABASE_URL ||
    process.env.BCN_DATABASE_URL ||
    "";
  if (!databaseUrl) {
    throw new Error(
      "DATABASE_URL, DASHBOARD_DATABASE_URL, or BCN_DATABASE_URL must be set for the dashboard.",
    );
  }
  const pool = new Pool({
    connectionString: databaseUrl,
    max: 4,
  });
  if (process.env.NODE_ENV !== "production") {
    globalForPg.__bcnDashboardPool = pool;
  }
  return pool;
}

function asObject(value: unknown): JsonObject {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as JsonObject;
  }
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as JsonObject;
      }
    } catch {
      return {};
    }
  }
  return {};
}

function asDateString(value: unknown): string | null {
  if (value instanceof Date) {
    return value.toISOString();
  }
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  return null;
}

function mapWorkflowJob(row: Record<string, unknown>): WorkflowJobSummary {
  return {
    id: String(row.id),
    lane: String(row.lane),
    jobType: String(row.job_type || "unknown"),
    workflowId: row.workflow_id ? String(row.workflow_id) : null,
    status: String(row.status || "queued") as WorkflowJobSummary["status"],
    createdAt: asDateString(row.created_at) || "",
    availableAt: asDateString(row.available_at),
    startedAt: asDateString(row.started_at),
    finishedAt: asDateString(row.finished_at),
    deadlineAt: asDateString(row.deadline_at),
    source: String(row.source || "scheduler"),
    priority: Number(row.priority || 0),
    attemptCount: Number(row.attempt_count || 0),
    maxAttempts: Number(row.max_attempts || 0),
    errorMessage: row.error_message ? String(row.error_message) : null,
  };
}

function mapWorkflowLane(row: Record<string, unknown>): WorkflowQueueLaneSummary {
  return {
    lane: String(row.lane),
    paused: Boolean(row.paused),
    reason: row.reason ? String(row.reason) : null,
    updatedBy: row.updated_by ? String(row.updated_by) : null,
    pausedAt: asDateString(row.paused_at),
    updatedAt: asDateString(row.updated_at),
    queuedCount: Number(row.queued_count || 0),
    leasedCount: Number(row.leased_count || 0),
    oldestQueuedAt: asDateString(row.oldest_queued_at),
    oldestLeasedAt: asDateString(row.oldest_leased_at),
  };
}

function mapWorkflowAttempt(row: Record<string, unknown>): WorkflowJobAttemptSummary {
  return {
    id: Number(row.id || 0),
    attemptNumber: Number(row.attempt_number || 0),
    workerId: String(row.worker_id || ""),
    status: String(row.status || "running"),
    startedAt: asDateString(row.started_at) || "",
    finishedAt: asDateString(row.finished_at),
    errorMessage: row.error_message ? String(row.error_message) : null,
    stateBefore: asObject(row.state_before),
    stateAfter: asObject(row.state_after),
    result: asObject(row.result),
  };
}

function mapWorkflowArtifact(row: Record<string, unknown>): WorkflowJobArtifactSummary {
  return {
    id: Number(row.id || 0),
    attemptId: row.attempt_id === null || row.attempt_id === undefined ? null : Number(row.attempt_id),
    artifactKey: String(row.artifact_key || ""),
    artifactType: String(row.artifact_type || ""),
    createdAt: asDateString(row.created_at) || "",
    updatedAt: asDateString(row.updated_at),
    payload: asObject(row.payload),
  };
}

function mergeWorkflowLaneRows(
  rows: WorkflowQueueLaneSummary[],
): WorkflowQueueLaneSummary[] {
  const mapped = new Map(rows.map((row) => [row.lane, row]));
  for (const lane of DEFAULT_WORKFLOW_LANES) {
    if (!mapped.has(lane)) {
      mapped.set(lane, {
        lane,
        paused: false,
        reason: null,
        updatedBy: null,
        pausedAt: null,
        updatedAt: null,
        queuedCount: 0,
        leasedCount: 0,
        oldestQueuedAt: null,
        oldestLeasedAt: null,
      });
    }
  }
  const laneIndex = (lane: string): number => {
    const index = DEFAULT_WORKFLOW_LANES.indexOf(
      lane as (typeof DEFAULT_WORKFLOW_LANES)[number],
    );
    return index >= 0 ? index : DEFAULT_WORKFLOW_LANES.length;
  };
  return [...mapped.values()].sort(
    (left, right) => laneIndex(left.lane) - laneIndex(right.lane),
  );
}

function ageSeconds(value: string | null): number | null {
  if (!value) {
    return null;
  }
  const millis = Date.now() - new Date(value).getTime();
  return Number.isFinite(millis) ? Math.max(0, Math.floor(millis / 1000)) : null;
}

function buildWorkflowQueueAlerts(snapshot: Omit<WorkflowQueueSnapshot, "alerts">): WorkflowQueueAlert[] {
  const alerts: WorkflowQueueAlert[] = [];
  for (const lane of snapshot.lanes) {
    if (lane.paused) {
      alerts.push({
        level: lane.lane === "publish" ? "critical" : "info",
        lane: lane.lane,
        message: `${lane.lane} lane is paused${lane.reason ? `: ${lane.reason}` : ""}`,
      });
    }
    const queuedAge = ageSeconds(lane.oldestQueuedAt);
    const queuedThreshold = QUEUE_AGE_WARN_SECONDS[lane.lane] || 3600;
    if (lane.queuedCount > 0 && queuedAge !== null && queuedAge >= queuedThreshold) {
      alerts.push({
        level: lane.lane === "publish" ? "critical" : "warn",
        lane: lane.lane,
        message: `${lane.lane} backlog has been queued for ${Math.floor(queuedAge / 60)}m`,
      });
    }
    const leasedAge = ageSeconds(lane.oldestLeasedAt);
    const leaseThreshold = LEASE_AGE_WARN_SECONDS[lane.lane] || 3600;
    if (lane.leasedCount > 0 && leasedAge !== null && leasedAge >= leaseThreshold) {
      alerts.push({
        level: lane.lane === "publish" ? "critical" : "warn",
        lane: lane.lane,
        message: `${lane.lane} worker has held a job for ${Math.floor(leasedAge / 60)}m`,
      });
    }
  }
  if (snapshot.failed24hCount > 0) {
    alerts.push({
      level: "warn",
      lane: null,
      message: `${snapshot.failed24hCount} workflow job(s) failed in the last 24h`,
    });
  }
  return alerts;
}

function isMissingRelationError(error: unknown): boolean {
  if (!error || typeof error !== "object") {
    return false;
  }
  return "code" in error && error.code === "42P01";
}

function mapEvaluationRun(row: Record<string, unknown>): EvaluationRunSummary {
  return {
    id: String(row.id),
    lane: String(row.lane),
    status: String(row.status || "completed") as EvaluationRunSummary["status"],
    createdAt: asDateString(row.created_at) || "",
    generatedAt: asDateString(row.generated_at),
    finishedAt: asDateString(row.finished_at),
    source: String(row.source || "cli"),
    count: Number(row.count || 0),
    reportPath: row.report_path ? String(row.report_path) : null,
    packPath: row.pack_path ? String(row.pack_path) : null,
    workflowMode: row.workflow_mode ? String(row.workflow_mode) : null,
    errorMessage: row.error_message ? String(row.error_message) : null,
    candidateOverrides: asObject(row.candidate_overrides),
    summary: asObject(row.summary),
  };
}

export async function getRecentEvaluationRuns(
  limit = 20,
): Promise<EvaluationRunSummary[]> {
  const result = await getPool().query(
    `
      SELECT
        id,
        lane,
        status,
        created_at,
        generated_at,
        finished_at,
        source,
        count,
        report_path,
        pack_path,
        workflow_mode,
        error_message,
        candidate_overrides,
        summary
      FROM evaluation_runs
      ORDER BY created_at DESC
      LIMIT $1
    `,
    [Math.max(1, limit)],
  );
  return result.rows.map((row) => mapEvaluationRun(row as Record<string, unknown>));
}

export async function getLatestEvaluationRunByLane(
  lane: "benchmark" | "shadow",
): Promise<EvaluationRunSummary | null> {
  const result = await getPool().query(
    `
      SELECT
        id,
        lane,
        status,
        created_at,
        generated_at,
        finished_at,
        source,
        count,
        report_path,
        pack_path,
        workflow_mode,
        error_message,
        candidate_overrides,
        summary
      FROM evaluation_runs
      WHERE lane = $1
      ORDER BY created_at DESC
      LIMIT 1
    `,
    [lane],
  );
  const row = result.rows[0];
  return row ? mapEvaluationRun(row as Record<string, unknown>) : null;
}

export async function getEvaluationRun(
  runId: string,
): Promise<EvaluationRunDetail | null> {
  const result = await getPool().query(
    `
      SELECT
        id,
        lane,
        status,
        created_at,
        generated_at,
        finished_at,
        source,
        count,
        report_path,
        pack_path,
        workflow_mode,
        error_message,
        candidate_overrides,
        summary,
        notes,
        report
      FROM evaluation_runs
      WHERE id = $1
      LIMIT 1
    `,
    [runId],
  );
  const row = result.rows[0];
  if (!row) {
    return null;
  }
  const summary = mapEvaluationRun(row as Record<string, unknown>);
  return {
    ...summary,
    notes: row.notes ? String(row.notes) : null,
    report: asObject(row.report),
  };
}

export async function getLatestSimulationSummary(): Promise<SimulationSummary> {
  const result = await getPool().query(
    `
      SELECT id, created_at, generated_at, count, summary
      FROM simulation_runs
      ORDER BY created_at DESC
      LIMIT 1
    `,
  );
  const row = result.rows[0];
  if (!row) {
    return null;
  }
  return {
    id: String(row.id),
    createdAt: asDateString(row.created_at) || "",
    generatedAt: asDateString(row.generated_at),
    count: Number(row.count || 0),
    summary: asObject(row.summary),
  };
}

export async function getWorkflowQueueSnapshot(
  limit = 10,
): Promise<WorkflowQueueSnapshot> {
  const rowLimit = Math.max(1, limit);
  try {
    const [summaryResult, jobsResult, lanesResult] = await Promise.all([
      getPool().query(
        `
          SELECT
            COUNT(*) FILTER (WHERE status = 'queued') AS queued_count,
            COUNT(*) FILTER (WHERE status = 'leased') AS leased_count,
            COUNT(*) FILTER (
              WHERE status = 'failed'
                AND COALESCE(finished_at, updated_at, created_at) >= NOW() - INTERVAL '24 hours'
            ) AS failed_24h_count,
            COUNT(*) FILTER (
              WHERE status = 'completed'
                AND COALESCE(finished_at, updated_at, created_at) >= NOW() - INTERVAL '24 hours'
            ) AS completed_24h_count,
            COUNT(*) FILTER (WHERE status = 'queued' AND lane = 'publish') AS publish_backlog_count,
            COUNT(*) FILTER (WHERE status = 'queued' AND lane = 'evaluation') AS evaluation_backlog_count
          FROM workflow_jobs
        `,
      ),
      getPool().query(
        `
          SELECT
            id,
            lane,
            job_type,
            workflow_id,
            status,
            created_at,
            available_at,
            started_at,
            finished_at,
            deadline_at,
            source,
            priority,
            attempt_count,
            max_attempts,
            error_message
          FROM workflow_jobs
          ORDER BY created_at DESC
          LIMIT $1
        `,
        [rowLimit],
      ),
      getPool().query(
        `
          WITH lane_stats AS (
            SELECT
              lane,
              COUNT(*) FILTER (WHERE status = 'queued') AS queued_count,
              COUNT(*) FILTER (WHERE status = 'leased') AS leased_count,
              MIN(created_at) FILTER (WHERE status = 'queued') AS oldest_queued_at,
              MIN(started_at) FILTER (WHERE status = 'leased') AS oldest_leased_at
            FROM workflow_jobs
            GROUP BY lane
          )
          SELECT
            COALESCE(stats.lane, controls.lane) AS lane,
            COALESCE(stats.queued_count, 0) AS queued_count,
            COALESCE(stats.leased_count, 0) AS leased_count,
            stats.oldest_queued_at,
            stats.oldest_leased_at,
            COALESCE(controls.paused, FALSE) AS paused,
            controls.reason,
            controls.updated_by,
            controls.paused_at,
            controls.updated_at
          FROM lane_stats AS stats
          FULL OUTER JOIN workflow_lane_controls AS controls
            ON controls.lane = stats.lane
        `,
      ),
    ]);

    const summary = (summaryResult.rows[0] || {}) as Record<string, unknown>;
    const lanes = mergeWorkflowLaneRows(
      lanesResult.rows.map((row) => mapWorkflowLane(row as Record<string, unknown>)),
    );
    const snapshotWithoutAlerts = {
      queuedCount: Number(summary.queued_count || 0),
      leasedCount: Number(summary.leased_count || 0),
      failed24hCount: Number(summary.failed_24h_count || 0),
      completed24hCount: Number(summary.completed_24h_count || 0),
      publishBacklogCount: Number(summary.publish_backlog_count || 0),
      evaluationBacklogCount: Number(summary.evaluation_backlog_count || 0),
      lanes,
      jobs: jobsResult.rows.map((row) => mapWorkflowJob(row as Record<string, unknown>)),
    };
    return {
      ...snapshotWithoutAlerts,
      alerts: buildWorkflowQueueAlerts(snapshotWithoutAlerts),
    };
  } catch (error) {
    if (!isMissingRelationError(error)) {
      throw error;
    }
    return {
      queuedCount: 0,
      leasedCount: 0,
      failed24hCount: 0,
      completed24hCount: 0,
      publishBacklogCount: 0,
      evaluationBacklogCount: 0,
      lanes: mergeWorkflowLaneRows([]),
      alerts: [],
      jobs: [],
    };
  }
}

export async function getWorkflowJob(
  jobId: string,
): Promise<WorkflowJobDetail | null> {
  try {
    const [jobResult, attemptsResult, artifactsResult] = await Promise.all([
      getPool().query(
        `
          SELECT
            jobs.id,
            jobs.lane,
            jobs.job_type,
            jobs.workflow_id,
            jobs.status,
            jobs.created_at,
            jobs.available_at,
            jobs.started_at,
            jobs.finished_at,
            jobs.deadline_at,
            jobs.source,
            jobs.priority,
            jobs.attempt_count,
            jobs.max_attempts,
            jobs.error_message,
            jobs.notes,
            jobs.payload,
            jobs.state,
            jobs.result,
            jobs.lease_owner,
            jobs.lease_expires_at,
            jobs.heartbeat_at,
            controls.paused,
            controls.reason AS control_reason,
            controls.updated_by AS control_updated_by,
            controls.paused_at AS control_paused_at,
            controls.updated_at AS control_updated_at
          FROM workflow_jobs AS jobs
          LEFT JOIN workflow_lane_controls AS controls
            ON controls.lane = jobs.lane
          WHERE jobs.id = $1
          LIMIT 1
        `,
        [jobId],
      ),
      getPool().query(
        `
          SELECT
            id,
            attempt_number,
            worker_id,
            status,
            started_at,
            finished_at,
            error_message,
            state_before,
            state_after,
            result
          FROM workflow_job_attempts
          WHERE job_id = $1
          ORDER BY attempt_number ASC
        `,
        [jobId],
      ),
      getPool().query(
        `
          SELECT
            id,
            attempt_id,
            artifact_key,
            artifact_type,
            payload,
            created_at,
            updated_at
          FROM workflow_job_artifacts
          WHERE job_id = $1
          ORDER BY created_at ASC, id ASC
        `,
        [jobId],
      ),
    ]);

    const row = jobResult.rows[0] as Record<string, unknown> | undefined;
    if (!row) {
      return null;
    }
    const summary = mapWorkflowJob(row);
    return {
      ...summary,
      availableAt: asDateString(row.available_at),
      startedAt: asDateString(row.started_at),
      finishedAt: asDateString(row.finished_at),
      deadlineAt: asDateString(row.deadline_at),
      leaseOwner: row.lease_owner ? String(row.lease_owner) : null,
      leaseExpiresAt: asDateString(row.lease_expires_at),
      heartbeatAt: asDateString(row.heartbeat_at),
      notes: row.notes ? String(row.notes) : null,
      payload: asObject(row.payload),
      state: asObject(row.state),
      result: asObject(row.result),
      control:
        row.paused === null || row.paused === undefined
          ? null
          : {
              lane: String(row.lane),
              paused: Boolean(row.paused),
              reason: row.control_reason ? String(row.control_reason) : null,
              updatedBy: row.control_updated_by ? String(row.control_updated_by) : null,
              pausedAt: asDateString(row.control_paused_at),
              updatedAt: asDateString(row.control_updated_at),
            },
      attempts: attemptsResult.rows.map((attempt) =>
        mapWorkflowAttempt(attempt as Record<string, unknown>),
      ),
      artifacts: artifactsResult.rows.map((artifact) =>
        mapWorkflowArtifact(artifact as Record<string, unknown>),
      ),
    };
  } catch (error) {
    if (!isMissingRelationError(error)) {
      throw error;
    }
    return null;
  }
}
