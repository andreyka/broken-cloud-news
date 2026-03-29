import { NextRequest, NextResponse } from "next/server";

import { runOpenAIEditorialReview } from "@/lib/ai-review";
import {
  getHumanReviewBriefing,
  insertAIReviewFromPortal,
} from "@/lib/db";

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
  const redirectTo = String(formData.get("redirectTo") || "/review").trim() || "/review";

  if (!briefingId) {
    const failureLocation = buildRedirectLocation(redirectTo, {
      reviewStatus: "error",
      reviewMessage: "Missing briefing id for AI review.",
    });
    return new NextResponse(null, {
      status: 303,
      headers: { Location: failureLocation },
    });
  }

  try {
    const detail = await getHumanReviewBriefing(briefingId);
    if (!detail) {
      throw new Error(`Briefing not found: ${briefingId}`);
    }

    const aiReview = await runOpenAIEditorialReview({
      briefingId,
      contentMarkdown: detail.briefing.contentMarkdown,
      latestRunModel: detail.latestRun?.llmModel || null,
      latestRunDecision: detail.latestRun?.decision || null,
      rewriteCount: detail.latestRun?.rewriteCount || 0,
    });

    const result = await insertAIReviewFromPortal({
      briefingId,
      reviewerProvider: aiReview.reviewerProvider,
      reviewerModel: aiReview.reviewerModel,
      reasoningEffort: aiReview.reasoningEffort,
      decision: aiReview.decision,
      issueTags: aiReview.issueTags,
      editedMarkdown: aiReview.editedMarkdown,
      notes: aiReview.notes,
      rawResponse: aiReview.rawResponse,
      source: "dashboard",
    });

    const successLocation = buildRedirectLocation(redirectTo, {
      briefingId,
      reviewStatus: "success",
      reviewMessage: `Stored AI review ${result.reviewId}.`,
    });
    return new NextResponse(null, {
      status: 303,
      headers: { Location: successLocation },
    });
  } catch (error) {
    const message =
      error instanceof Error && error.message.trim().length > 0
        ? error.message
        : "Failed to run AI review.";
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
