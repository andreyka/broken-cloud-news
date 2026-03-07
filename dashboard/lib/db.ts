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

const globalForPg = globalThis as typeof globalThis & {
  __bcnDashboardPool?: Pool;
};

function getPool(): Pool {
  if (globalForPg.__bcnDashboardPool) {
    return globalForPg.__bcnDashboardPool;
  }
  const databaseUrl =
    process.env.DASHBOARD_DATABASE_URL || process.env.DATABASE_URL || "";
  if (!databaseUrl) {
    throw new Error(
      "DATABASE_URL or DASHBOARD_DATABASE_URL must be set for the dashboard.",
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
