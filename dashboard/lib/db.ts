import { Pool } from "pg";

import type { AIReviewConfig } from "@/lib/ai-review";
import { getAIReviewConfig } from "@/lib/ai-review";
import type { ReviewDecision } from "@/lib/review-taxonomy";

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

export type WorkflowControlAction = "publish" | "analysis" | "collection";

export type WorkflowControlEnqueueResult = {
  action: WorkflowControlAction;
  lane: string;
  jobIds: string[];
  workflowIds: string[];
};

export type HumanReviewDecision = ReviewDecision;

export type AIReviewSummary = {
  id: string;
  briefingId: string;
  runId: string | null;
  source: string;
  reviewerProvider: string;
  reviewerModel: string;
  reasoningEffort: string | null;
  decision: HumanReviewDecision;
  issueTags: string[];
  editedMarkdown: string | null;
  notes: string | null;
  rawResponse: JsonObject;
  createdAt: string;
};

export type HumanReviewSummary = {
  id: string;
  briefingId: string;
  runId: string | null;
  reviewer: string;
  decision: HumanReviewDecision;
  issueTags: string[];
  editedMarkdown: string | null;
  notes: string | null;
  createdAt: string;
};

export type BriefingReviewQueueEntry = {
  id: string;
  createdAt: string;
  distributedAt: string | null;
  status: string;
  preview: string;
  reviewCount: number;
  lastDecision: HumanReviewDecision | null;
  lastReviewAt: string | null;
};

export type BriefingReviewStatusFilter = "distributed" | "draft" | "all";

export type BriefingReviewDetail = {
  briefing: {
    id: string;
    createdAt: string;
    distributedAt: string | null;
    status: string;
    contentMarkdown: string;
    coverImageUrl: string | null;
    coverImagePrompt: string | null;
  };
  latestRun: {
    id: string;
    createdAt: string;
    decision: string;
    rewriteCount: number;
    llmModel: string | null;
  } | null;
  reviews: HumanReviewSummary[];
  aiReviews: AIReviewSummary[];
  aiReviewConfig: AIReviewConfig;
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

type WorkflowStepPayload = {
  step_id: string;
  component: string;
  operation: string;
  args?: Record<string, unknown>;
};

type ManualWorkflowDefinition = {
  workflowId: string;
  description: string;
  lane: "publish" | "collection" | "analysis";
  priority: number;
  maxAttempts: number;
  steps: WorkflowStepPayload[];
};

const MANUAL_WORKFLOW_ACTIONS: Record<
  WorkflowControlAction,
  readonly ManualWorkflowDefinition[]
> = {
  publish: [
    {
      workflowId: "regular_daily_briefing",
      description: "Run the regular daily briefing publish pipeline.",
      lane: "publish",
      priority: 100,
      maxAttempts: 3,
      steps: [
        {
          step_id: "generate_briefing",
          component: "writer",
          operation: "generate_release_candidate",
          args: { mode: "regular_daily_briefing" },
        },
        {
          step_id: "distribute_briefing",
          component: "distributor",
          operation: "deliver",
          args: { mode: "regular_daily_briefing" },
        },
      ],
    },
  ],
  analysis: [
    {
      workflowId: "analyst",
      description: "Analyze newly collected items.",
      lane: "analysis",
      priority: 40,
      maxAttempts: 3,
      steps: [
        {
          step_id: "analyze_pending",
          component: "analyst",
          operation: "analyze_pending",
        },
      ],
    },
  ],
  collection: [
    {
      workflowId: "ghsa_collector",
      description: "Collect GitHub Security Advisory items.",
      lane: "collection",
      priority: 50,
      maxAttempts: 3,
      steps: [
        {
          step_id: "collect_ghsa",
          component: "collector",
          operation: "collect",
          args: { source: "ghsa" },
        },
      ],
    },
    {
      workflowId: "rss_collector",
      description: "Collect RSS items from configured feeds.",
      lane: "collection",
      priority: 50,
      maxAttempts: 3,
      steps: [
        {
          step_id: "collect_rss",
          component: "collector",
          operation: "collect",
          args: { source: "rss" },
        },
      ],
    },
    {
      workflowId: "reddit_collector",
      description: "Collect Reddit items from configured subreddits.",
      lane: "collection",
      priority: 50,
      maxAttempts: 3,
      steps: [
        {
          step_id: "collect_reddit",
          component: "collector",
          operation: "collect",
          args: { source: "reddit" },
        },
      ],
    },
    {
      workflowId: "twitter_collector",
      description: "Collect Twitter/X items from configured handles.",
      lane: "collection",
      priority: 50,
      maxAttempts: 3,
      steps: [
        {
          step_id: "collect_twitter",
          component: "collector",
          operation: "collect",
          args: { source: "twitter" },
        },
      ],
    },
  ],
} as const;

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

function envInt(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) {
    return fallback;
  }
  const value = Number.parseInt(raw, 10);
  return Number.isFinite(value) ? value : fallback;
}

function leaseSecondsForLane(
  lane: ManualWorkflowDefinition["lane"],
): number {
  if (lane === "publish") {
    return envInt("BCN_WORKFLOW_JOB_PUBLISH_LEASE_SECONDS", 1200);
  }
  if (lane === "analysis") {
    return envInt("BCN_WORKFLOW_JOB_ANALYSIS_LEASE_SECONDS", 1200);
  }
  return envInt("BCN_WORKFLOW_JOB_COLLECTION_LEASE_SECONDS", 900);
}

function deadlineSecondsForLane(
  lane: ManualWorkflowDefinition["lane"],
): number {
  if (lane === "publish") {
    return envInt("BCN_WORKFLOW_JOB_PUBLISH_DEADLINE_SECONDS", 7200);
  }
  if (lane === "analysis") {
    return envInt("BCN_WORKFLOW_JOB_ANALYSIS_DEADLINE_SECONDS", 3600);
  }
  return envInt("BCN_WORKFLOW_JOB_COLLECTION_DEADLINE_SECONDS", 5400);
}

function workflowJobPayload(definition: ManualWorkflowDefinition): string {
  return JSON.stringify({
    workflow_id: definition.workflowId,
    description: definition.description,
    steps: definition.steps.map((step) => ({
      step_id: step.step_id,
      component: step.component,
      operation: step.operation,
      args: step.args || {},
    })),
  });
}

export function isWorkflowControlAction(
  value: string,
): value is WorkflowControlAction {
  return value === "publish" || value === "analysis" || value === "collection";
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

function asStringArray(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value
      .filter((item): item is string => typeof item === "string")
      .map((item) => item.trim())
      .filter(Boolean);
  }
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) {
        return parsed
          .filter((item): item is string => typeof item === "string")
          .map((item) => item.trim())
          .filter(Boolean);
      }
    } catch {
      return [];
    }
  }
  return [];
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

function mapHumanReview(row: Record<string, unknown>): HumanReviewSummary {
  return {
    id: String(row.id),
    briefingId: String(row.briefing_id),
    runId: row.run_id ? String(row.run_id) : null,
    reviewer: String(row.reviewer || "portal"),
    decision: String(row.decision || "needs_work") as HumanReviewDecision,
    issueTags: asStringArray(row.issue_tags),
    editedMarkdown:
      row.edited_markdown && String(row.edited_markdown).trim().length > 0
        ? String(row.edited_markdown)
        : null,
    notes: row.notes && String(row.notes).trim().length > 0 ? String(row.notes) : null,
    createdAt: asDateString(row.created_at) || "",
  };
}

function mapAIReview(row: Record<string, unknown>): AIReviewSummary {
  return {
    id: String(row.id),
    briefingId: String(row.briefing_id),
    runId: row.run_id ? String(row.run_id) : null,
    source: String(row.source || "dashboard"),
    reviewerProvider: String(row.reviewer_provider || "openai"),
    reviewerModel: String(row.reviewer_model || ""),
    reasoningEffort: row.reasoning_effort ? String(row.reasoning_effort) : null,
    decision: String(row.decision || "needs_work") as HumanReviewDecision,
    issueTags: asStringArray(row.issue_tags),
    editedMarkdown:
      row.edited_markdown && String(row.edited_markdown).trim().length > 0
        ? String(row.edited_markdown)
        : null,
    notes: row.notes && String(row.notes).trim().length > 0 ? String(row.notes) : null,
    rawResponse: asObject(row.raw_response),
    createdAt: asDateString(row.created_at) || "",
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

export async function enqueueWorkflowControlAction(
  action: WorkflowControlAction,
): Promise<WorkflowControlEnqueueResult> {
  const workflows = MANUAL_WORKFLOW_ACTIONS[action];
  if (!workflows || workflows.length === 0) {
    throw new Error(`Unsupported workflow control action: ${action}`);
  }

  const client = await getPool().connect();
  try {
    await client.query("BEGIN");
    const jobIds: string[] = [];
    for (const workflow of workflows) {
      const result = await client.query(
        `
          INSERT INTO workflow_jobs (
            lane,
            priority,
            job_type,
            source,
            workflow_id,
            max_attempts,
            lease_duration_seconds,
            payload,
            notes,
            deadline_at
          )
          VALUES (
            $1,
            $2,
            'scheduled_workflow',
            'dashboard',
            $3,
            $4,
            $5,
            $6::jsonb,
            $7,
            NOW() + make_interval(secs => $8)
          )
          RETURNING id
        `,
        [
          workflow.lane,
          workflow.priority,
          workflow.workflowId,
          workflow.maxAttempts,
          leaseSecondsForLane(workflow.lane),
          workflowJobPayload(workflow),
          `Queued manually from control portal (${action})`,
          deadlineSecondsForLane(workflow.lane),
        ],
      );
      jobIds.push(String(result.rows[0]?.id || ""));
    }
    await client.query("COMMIT");
    return {
      action,
      lane: workflows[0].lane,
      jobIds,
      workflowIds: workflows.map((workflow) => workflow.workflowId),
    };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

export async function getHumanReviewQueue(
  limit = 20,
  onlyUnreviewed = false,
  statusFilter: BriefingReviewStatusFilter = "distributed",
): Promise<BriefingReviewQueueEntry[]> {
  const filters: string[] = [];
  if (onlyUnreviewed) {
    filters.push("COALESCE(rv.review_count, 0) = 0");
  }
  if (statusFilter === "distributed") {
    filters.push("b.status = 'DISTRIBUTED'");
  } else if (statusFilter === "draft") {
    filters.push("b.status = 'DRAFT'");
  }
  const where = filters.length > 0 ? `WHERE ${filters.join(" AND ")}` : "";
  const result = await getPool().query(
    `
      SELECT
        b.id,
        b.created_at,
        b.distributed_at,
        b.status,
        LEFT(COALESCE(b.content_markdown, ''), 280) AS preview,
        COALESCE(rv.review_count, 0)::int AS review_count,
        rv.last_decision,
        rv.last_review_at
      FROM briefings b
      LEFT JOIN LATERAL (
        SELECT
          COUNT(*)::int AS review_count,
          MAX(created_at) AS last_review_at,
          (ARRAY_AGG(decision ORDER BY created_at DESC))[1] AS last_decision
        FROM briefing_human_reviews hr
        WHERE hr.briefing_id = b.id
      ) rv ON TRUE
      ${where}
      ORDER BY b.created_at DESC
      LIMIT $1
    `,
    [Math.max(1, limit)],
  );
  return result.rows.map((row) => ({
    id: String(row.id),
    createdAt: asDateString(row.created_at) || "",
    distributedAt: asDateString(row.distributed_at),
    status: String(row.status || "DRAFT"),
    preview: String(row.preview || ""),
    reviewCount: Number(row.review_count || 0),
    lastDecision: row.last_decision
      ? (String(row.last_decision) as HumanReviewDecision)
      : null,
    lastReviewAt: asDateString(row.last_review_at),
  }));
}

export async function getHumanReviewBriefing(
  briefingId: string,
): Promise<BriefingReviewDetail | null> {
  const aiReviewsPromise = getPool()
    .query(
      `
        SELECT
          id,
          briefing_id,
          run_id,
          source,
          reviewer_provider,
          reviewer_model,
          reasoning_effort,
          decision,
          issue_tags,
          edited_markdown,
          notes,
          raw_response,
          created_at
        FROM briefing_ai_reviews
        WHERE briefing_id = $1
        ORDER BY created_at DESC
      `,
      [briefingId],
    )
    .catch((error) => {
      if (isMissingRelationError(error)) {
        return { rows: [] as Record<string, unknown>[] };
      }
      throw error;
    });

  const [briefingResult, reviewsResult, aiReviewsResult] = await Promise.all([
    getPool().query(
      `
        SELECT
          b.id,
          b.created_at,
          b.distributed_at,
          b.status,
          b.content_markdown,
          b.cover_image_url,
          b.cover_image_prompt,
          gr.id AS run_id,
          gr.created_at AS run_created_at,
          gr.decision AS run_decision,
          gr.rewrite_count AS run_rewrite_count,
          gr.llm_model AS run_llm_model
        FROM briefings b
        LEFT JOIN LATERAL (
          SELECT id, created_at, decision, rewrite_count, llm_model
          FROM generation_runs
          WHERE briefing_id = b.id
          ORDER BY created_at DESC
          LIMIT 1
        ) gr ON TRUE
        WHERE b.id = $1
        LIMIT 1
      `,
      [briefingId],
    ),
    getPool().query(
      `
        SELECT
          id,
          briefing_id,
          run_id,
          reviewer,
          decision,
          issue_tags,
          edited_markdown,
          notes,
          created_at
        FROM briefing_human_reviews
        WHERE briefing_id = $1
        ORDER BY created_at DESC
      `,
      [briefingId],
    ),
    aiReviewsPromise,
  ]);

  const row = briefingResult.rows[0] as Record<string, unknown> | undefined;
  if (!row) {
    return null;
  }

  return {
    briefing: {
      id: String(row.id),
      createdAt: asDateString(row.created_at) || "",
      distributedAt: asDateString(row.distributed_at),
      status: String(row.status || "DRAFT"),
      contentMarkdown: String(row.content_markdown || ""),
      coverImageUrl: row.cover_image_url ? String(row.cover_image_url) : null,
      coverImagePrompt: row.cover_image_prompt
        ? String(row.cover_image_prompt)
        : null,
    },
    latestRun: row.run_id
      ? {
          id: String(row.run_id),
          createdAt: asDateString(row.run_created_at) || "",
          decision: String(row.run_decision || "PENDING"),
          rewriteCount: Number(row.run_rewrite_count || 0),
          llmModel: row.run_llm_model ? String(row.run_llm_model) : null,
        }
      : null,
    reviews: reviewsResult.rows.map((review) =>
      mapHumanReview(review as Record<string, unknown>),
    ),
    aiReviews: aiReviewsResult.rows.map((review) =>
      mapAIReview(review as Record<string, unknown>),
    ),
    aiReviewConfig: getAIReviewConfig(),
  };
}

export async function insertHumanReviewFromPortal(params: {
  briefingId: string;
  decision: HumanReviewDecision;
  reviewer?: string;
  issueTags?: string[];
  editedMarkdown?: string | null;
  notes?: string | null;
}): Promise<{ reviewId: string; runId: string | null }> {
  const decision = params.decision;
  if (!["accept", "reject", "edit", "needs_work"].includes(decision)) {
    throw new Error(`Unsupported review decision: ${decision}`);
  }

  const reviewer = (params.reviewer || "portal").trim() || "portal";
  const issueTags = [...new Set((params.issueTags || []).map((tag) => tag.trim()).filter(Boolean))];
  const editedMarkdown =
    params.editedMarkdown && params.editedMarkdown.trim().length > 0
      ? params.editedMarkdown.trim()
      : null;
  const notes =
    params.notes && params.notes.trim().length > 0 ? params.notes.trim() : null;

  const client = await getPool().connect();
  try {
    await client.query("BEGIN");
    const briefingResult = await client.query(
      `SELECT id FROM briefings WHERE id = $1 LIMIT 1`,
      [params.briefingId],
    );
    if (briefingResult.rowCount === 0) {
      throw new Error(`Briefing not found: ${params.briefingId}`);
    }

    const runResult = await client.query(
      `
        SELECT id
        FROM generation_runs
        WHERE briefing_id = $1
        ORDER BY created_at DESC
        LIMIT 1
      `,
      [params.briefingId],
    );
    const runId = runResult.rows[0]?.id ? String(runResult.rows[0].id) : null;

    const reviewResult = await client.query(
      `
        INSERT INTO briefing_human_reviews (
          briefing_id,
          run_id,
          reviewer,
          decision,
          issue_tags,
          edited_markdown,
          notes
        )
        VALUES ($1, $2, $3, $4, $5::text[], $6, $7)
        RETURNING id
      `,
      [
        params.briefingId,
        runId,
        reviewer,
        decision,
        issueTags,
        editedMarkdown,
        notes,
      ],
    );

    await client.query("COMMIT");
    return {
      reviewId: String(reviewResult.rows[0]?.id || ""),
      runId,
    };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

export async function insertAIReviewFromPortal(params: {
  briefingId: string;
  reviewerProvider: string;
  reviewerModel: string;
  reasoningEffort?: string | null;
  decision: HumanReviewDecision;
  issueTags?: string[];
  editedMarkdown?: string | null;
  notes?: string | null;
  rawResponse?: JsonObject;
  source?: string;
}): Promise<{ reviewId: string; runId: string | null }> {
  const decision = params.decision;
  if (!["accept", "reject", "edit", "needs_work"].includes(decision)) {
    throw new Error(`Unsupported AI review decision: ${decision}`);
  }

  const reviewerProvider = params.reviewerProvider.trim() || "openai";
  const reviewerModel = params.reviewerModel.trim();
  if (!reviewerModel) {
    throw new Error("AI review must include reviewer model.");
  }
  const issueTags = [...new Set((params.issueTags || []).map((tag) => tag.trim()).filter(Boolean))];
  const editedMarkdown =
    params.editedMarkdown && params.editedMarkdown.trim().length > 0
      ? params.editedMarkdown.trim()
      : null;
  const notes =
    params.notes && params.notes.trim().length > 0 ? params.notes.trim() : null;
  const reasoningEffort =
    params.reasoningEffort && params.reasoningEffort.trim().length > 0
      ? params.reasoningEffort.trim()
      : null;
  const source = params.source?.trim() || "dashboard";

  const client = await getPool().connect();
  try {
    await client.query("BEGIN");
    const briefingResult = await client.query(
      `SELECT id FROM briefings WHERE id = $1 LIMIT 1`,
      [params.briefingId],
    );
    if (briefingResult.rowCount === 0) {
      throw new Error(`Briefing not found: ${params.briefingId}`);
    }

    const runResult = await client.query(
      `
        SELECT id
        FROM generation_runs
        WHERE briefing_id = $1
        ORDER BY created_at DESC
        LIMIT 1
      `,
      [params.briefingId],
    );
    const runId = runResult.rows[0]?.id ? String(runResult.rows[0].id) : null;

    const reviewResult = await client.query(
      `
        INSERT INTO briefing_ai_reviews (
          briefing_id,
          run_id,
          source,
          reviewer_provider,
          reviewer_model,
          reasoning_effort,
          decision,
          issue_tags,
          edited_markdown,
          notes,
          raw_response
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::text[], $9, $10, $11::jsonb)
        RETURNING id
      `,
      [
        params.briefingId,
        runId,
        source,
        reviewerProvider,
        reviewerModel,
        reasoningEffort,
        decision,
        issueTags,
        editedMarkdown,
        notes,
        JSON.stringify(params.rawResponse || {}),
      ],
    );

    await client.query("COMMIT");
    return {
      reviewId: String(reviewResult.rows[0]?.id || ""),
      runId,
    };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
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
