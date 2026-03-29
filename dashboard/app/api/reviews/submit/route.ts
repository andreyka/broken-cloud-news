import { NextRequest, NextResponse } from "next/server";

import {
  type HumanReviewDecision,
  insertHumanReviewFromPortal,
} from "@/lib/db";

const VALID_DECISIONS = new Set<HumanReviewDecision>([
  "accept",
  "reject",
  "edit",
  "needs_work",
]);

function buildRedirectLocation(
  redirectTo: string,
  params: Record<string, string>,
): string {
  const [basePath, existingQuery = ""] = redirectTo.split("?", 2);
  const query = new URLSearchParams(existingQuery);
  for (const [key, value] of Object.entries(params)) {
    query.set(key, value);
  }
  const suffix = query.toString();
  return suffix ? `${basePath}?${suffix}` : basePath;
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  const formData = await request.formData();
  const briefingId = String(formData.get("briefingId") || "").trim();
  const decision = String(formData.get("decision") || "").trim() as HumanReviewDecision;
  const reviewer = String(formData.get("reviewer") || "portal").trim();
  const notes = String(formData.get("notes") || "").trim();
  const editedMarkdown = String(formData.get("editedMarkdown") || "").trim();
  const redirectTo = String(formData.get("redirectTo") || "/review").trim() || "/review";
  const issueTags = formData
    .getAll("issueTag")
    .map((value) => String(value || "").trim())
    .filter(Boolean);

  if (!briefingId || !VALID_DECISIONS.has(decision)) {
    const failureLocation = buildRedirectLocation(redirectTo, {
      reviewStatus: "error",
      reviewMessage: "Missing briefing id or review decision.",
    });
    return new NextResponse(null, {
      status: 303,
      headers: { Location: failureLocation },
    });
  }

  try {
    const result = await insertHumanReviewFromPortal({
      briefingId,
      decision,
      reviewer,
      issueTags,
      editedMarkdown,
      notes,
    });
    const successLocation = buildRedirectLocation(redirectTo, {
      briefingId,
      reviewStatus: "success",
      reviewMessage: `Stored ${decision} review ${result.reviewId}.`,
    });
    return new NextResponse(null, {
      status: 303,
      headers: { Location: successLocation },
    });
  } catch (error) {
    const message =
      error instanceof Error && error.message.trim().length > 0
        ? error.message
        : "Failed to store human review.";
    const failureLocation = buildRedirectLocation(redirectTo, {
      briefingId,
      reviewStatus: "error",
      reviewMessage: message,
    });
    return new NextResponse(null, {
      status: 303,
      headers: { Location: failureLocation },
    });
  }
}
