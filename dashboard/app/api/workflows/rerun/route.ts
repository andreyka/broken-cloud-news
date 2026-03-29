import { NextResponse } from "next/server";

import {
  enqueueWorkflowControlAction,
  isWorkflowControlAction,
} from "@/lib/db";

export const runtime = "nodejs";

function safeRedirectTarget(value: string | null): string {
  if (!value) {
    return "/";
  }
  if (!value.startsWith("/")) {
    return "/";
  }
  if (value.startsWith("//")) {
    return "/";
  }
  return value;
}

function successMessage(action: string, count: number): string {
  if (action === "collection") {
    return `Queued ${count} collection workflow jobs.`;
  }
  if (action === "analysis") {
    return "Queued analysis workflow rerun.";
  }
  return "Queued publish workflow rerun.";
}

function redirectWithMessage(
  redirectTo: string,
  status: "success" | "error",
  message: string,
) {
  const params = new URLSearchParams();
  params.set("queueActionStatus", status);
  params.set("queueActionMessage", message);
  return new NextResponse(null, {
    status: 303,
    headers: {
      Location: `${redirectTo}?${params.toString()}`,
    },
  });
}

export async function POST(request: Request) {
  const formData = await request.formData();
  const target = String(formData.get("target") || "").trim();
  const redirectTo = safeRedirectTarget(
    String(formData.get("redirectTo") || "/"),
  );

  if (!isWorkflowControlAction(target)) {
    return redirectWithMessage(
      redirectTo,
      "error",
      "Unknown workflow action requested.",
    );
  }

  try {
    const result = await enqueueWorkflowControlAction(target);
    return redirectWithMessage(
      redirectTo,
      "success",
      successMessage(target, result.jobIds.length),
    );
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Failed to queue workflow action.";
    return redirectWithMessage(redirectTo, "error", message);
  }
}
