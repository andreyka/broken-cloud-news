import {
  REVIEW_DECISIONS,
  REVIEW_ISSUE_TAGS,
  type ReviewDecision,
  type ReviewIssueTag,
} from "@/lib/review-taxonomy";

export type AIReviewConfig = {
  enabled: boolean;
  provider: "openai";
  baseUrl: string;
  model: string;
  reasoningEffort: "low" | "medium" | "high" | "xhigh" | null;
  apiKeyConfigured: boolean;
};

export type AIReviewInput = {
  briefingId: string;
  contentMarkdown: string;
  latestRunModel?: string | null;
  latestRunDecision?: string | null;
  rewriteCount?: number | null;
};

export type AIReviewResult = {
  reviewerProvider: "openai";
  reviewerModel: string;
  reasoningEffort: string | null;
  decision: ReviewDecision;
  issueTags: ReviewIssueTag[];
  notes: string | null;
  editedMarkdown: string | null;
  rawResponse: Record<string, unknown>;
};

type OpenAIToolCall = {
  function?: {
    name?: string;
    arguments?: string;
  };
};

type OpenAIChatChoice = {
  message?: {
    content?: string | null;
    tool_calls?: OpenAIToolCall[];
  };
};

type OpenAIChatResponse = {
  id?: string;
  model?: string;
  choices?: OpenAIChatChoice[];
  usage?: Record<string, unknown>;
};

const OPENAI_API_BASE_URL = "https://api.openai.com/v1";
const OPENAI_TIMEOUT_MS = 180_000;
const OPENAI_TOOL_NAME = "submit_editorial_review";

function envValue(name: string): string | null {
  const raw = process.env[name];
  if (!raw) {
    return null;
  }
  const value = raw.trim();
  return value.length > 0 ? value : null;
}

function normalizeReasoningEffort(
  value: string | null,
): AIReviewConfig["reasoningEffort"] {
  if (value === "low" || value === "medium" || value === "high" || value === "xhigh") {
    return value;
  }
  return null;
}

export function getAIReviewConfig(): AIReviewConfig {
  const apiKey = envValue("BCN_AI_REVIEW_API_KEY") || envValue("OPENAI_API_KEY");
  return {
    enabled: Boolean(apiKey),
    provider: "openai",
    baseUrl: envValue("BCN_AI_REVIEW_BASE_URL") || OPENAI_API_BASE_URL,
    model: envValue("BCN_AI_REVIEW_MODEL") || "gpt-5.4",
    reasoningEffort: normalizeReasoningEffort(
      envValue("BCN_AI_REVIEW_REASONING_EFFORT") || "high",
    ),
    apiKeyConfigured: Boolean(apiKey),
  };
}

function buildSystemPrompt(): string {
  return [
    "You are a strict security editorial reviewer for a cloud security news briefing.",
    "You are not the original writer. You are the reviewer and rewrite editor.",
    "Review the supplied final BCN markdown as if you were deciding whether it should stand as-is, be lightly rewritten, need substantial rework, or be rejected.",
    "Return exactly one tool call.",
    "Allowed decisions:",
    "- accept: strong, publishable, no rewrite needed.",
    "- edit: mostly good; a rewrite improves accuracy, clarity, or formatting.",
    "- needs_work: useful, but not ready without substantive fixes.",
    "- reject: do not use this draft as a publish candidate.",
    "Rules:",
    "- Judge only from the provided markdown and metadata. No source material is provided.",
    "- Do not invent facts, URLs, incidents, or organizations.",
    "- Treat unsupported certainty conservatively. If a claim appears stronger than the draft justifies, say so.",
    "- Distinguish between confirmed exploitation, proof-of-concept exploitation, theoretical impact, configuration-dependent risk, default-only risk, exposed-only risk, and authenticated versus unauthenticated attack paths when the draft suggests them.",
    "- Flag phrases that sound absolute or overly strong unless the text clearly justifies them.",
    "- Evaluate whether the draft truly reads as cloud security news, not just generic infrastructure security.",
    "- Strong cloud framing usually connects to control plane risk, identity or auth plane, secrets or metadata exposure, multi-tenancy, internet-facing edge risk, CI/CD supply chain, orchestration state, IAM boundaries, managed service exposure, or hybrid trust boundaries.",
    "- Keep the style punchy, technical, compact, and opinionated without fearmongering or generic filler.",
    "- Preserve existing links in any rewrite unless the surrounding sentence must be removed.",
    "- Prefer the smallest set of issue tags that explains the main problems.",
    "- The verdict summary and notes should be concise and concrete.",
    "- If you provide edited_markdown, it must be the full corrected markdown, not partial fragments.",
    "- If you provide alternate_markdown, it should be a second full draft with slightly different style: either more restrained and analytical or more punchy and digest-like.",
  ].join("\n");
}

function buildUserPrompt(input: AIReviewInput): string {
  return [
    `Briefing ID: ${input.briefingId}`,
    `Latest model: ${input.latestRunModel || "unknown"}`,
    `Latest generation decision: ${input.latestRunDecision || "unknown"}`,
    `Rewrite count: ${String(input.rewriteCount ?? 0)}`,
    "",
    "Evaluate the markdown below against BCN editorial standards:",
    "- factual grounding and scope accuracy",
    "- whether attack prerequisites are stated clearly",
    "- whether impact is overstated or understated",
    "- whether remediation advice is appropriately scoped",
    "- cloud relevance and cloud-security framing strength",
    "- opener strength, structure, readability, and tone precision",
    "",
    "Markdown:",
    input.contentMarkdown,
  ].join("\n");
}

function parseJsonLike(value: string): Record<string, unknown> | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  const candidates = [trimmed];
  const fencedMatch = trimmed.match(/```(?:json)?\s*([\s\S]+?)```/i);
  if (fencedMatch?.[1]) {
    candidates.unshift(fencedMatch[1].trim());
  }
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      continue;
    }
  }
  return null;
}

function parseToolArguments(response: OpenAIChatResponse): Record<string, unknown> | null {
  const firstChoice = response.choices?.[0];
  const toolCalls = firstChoice?.message?.tool_calls || [];
  for (const call of toolCalls) {
    if (call.function?.name !== OPENAI_TOOL_NAME) {
      continue;
    }
    const argumentsText = call.function.arguments || "";
    const parsed = parseJsonLike(argumentsText);
    if (parsed) {
      return parsed;
    }
  }
  const content = firstChoice?.message?.content || "";
  return typeof content === "string" ? parseJsonLike(content) : null;
}

function asString(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

function sanitizeIssueTags(value: unknown): ReviewIssueTag[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const allowed = new Set<string>(REVIEW_ISSUE_TAGS);
  const unique = new Set<ReviewIssueTag>();
  for (const item of value) {
    if (typeof item !== "string") {
      continue;
    }
    const trimmed = item.trim();
    if (allowed.has(trimmed)) {
      unique.add(trimmed as ReviewIssueTag);
    }
  }
  return [...unique];
}

function sanitizeDecision(value: unknown): ReviewDecision | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  if (REVIEW_DECISIONS.includes(trimmed as ReviewDecision)) {
    return trimmed as ReviewDecision;
  }
  return null;
}

function normalizeAIReviewResult(
  config: AIReviewConfig,
  response: OpenAIChatResponse,
  payload: Record<string, unknown>,
): AIReviewResult {
  const decision = sanitizeDecision(payload.decision);
  if (!decision) {
    throw new Error("AI review did not return a supported decision.");
  }
  const editedMarkdown = asString(payload.edited_markdown);
  return {
    reviewerProvider: "openai",
    reviewerModel: asString(response.model) || config.model,
    reasoningEffort: config.reasoningEffort,
    decision,
    issueTags: sanitizeIssueTags(payload.issue_tags),
    notes: asString(payload.notes),
    editedMarkdown,
    rawResponse: {
      response_id: asString(response.id),
      model: asString(response.model) || config.model,
      usage:
        response.usage && typeof response.usage === "object" && !Array.isArray(response.usage)
          ? response.usage
          : {},
      extracted_review: payload,
    },
  };
}

export async function runOpenAIEditorialReview(
  input: AIReviewInput,
): Promise<AIReviewResult> {
  const config = getAIReviewConfig();
  const apiKey = envValue("BCN_AI_REVIEW_API_KEY") || envValue("OPENAI_API_KEY");
  if (!config.enabled || !apiKey) {
    throw new Error(
      "AI review is not configured. Set BCN_AI_REVIEW_API_KEY or OPENAI_API_KEY.",
    );
  }

  const response = await fetch(`${config.baseUrl.replace(/\/+$/, "")}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model: config.model,
      reasoning_effort: config.reasoningEffort || undefined,
      temperature: 0.2,
      max_completion_tokens: 8000,
      tool_choice: {
        type: "function",
        function: { name: OPENAI_TOOL_NAME },
      },
      tools: [
        {
          type: "function",
          function: {
            name: OPENAI_TOOL_NAME,
            description:
              "Submit a structured editorial review for the supplied BCN briefing.",
            parameters: {
              type: "object",
              additionalProperties: false,
              properties: {
                decision: {
                  type: "string",
                  enum: [...REVIEW_DECISIONS],
                },
                issue_tags: {
                  type: "array",
                  items: {
                    type: "string",
                    enum: [...REVIEW_ISSUE_TAGS],
                  },
                },
                verdict_summary: {
                  type: "string",
                  description:
                    "2-5 sentence overall assessment. State whether the draft works as cloud security news and whether any claims are too strong.",
                },
                notes: {
                  type: "string",
                  description:
                    "Concise editorial notes and recommended fixes.",
                },
                recommended_fixes: {
                  type: "array",
                  items: { type: "string" },
                  description:
                    "Concrete edits that would improve the draft without inventing facts.",
                },
                cloud_angle_strength: {
                  type: "string",
                  enum: ["strong", "moderate", "weak", "mostly_absent"],
                },
                cloud_angle_rationale: {
                  type: "string",
                  description:
                    "Why the cloud angle is strong, moderate, weak, or mostly absent, and how to strengthen it without inventing facts.",
                },
                strong_claims_to_soften: {
                  type: "array",
                  items: {
                    type: "object",
                    additionalProperties: false,
                    properties: {
                      original: { type: "string" },
                      why: { type: "string" },
                      safer_replacement: { type: "string" },
                    },
                    required: ["original", "why", "safer_replacement"],
                  },
                  description:
                    "Specific phrases from the draft that should be softened.",
                },
                assumptions: {
                  type: "array",
                  items: { type: "string" },
                  description:
                    "Any assumptions made, verification gaps, or claims that should not be stated without checking an advisory.",
                },
                edited_markdown: {
                  type: "string",
                  description:
                    "Optional full corrected markdown rewrite. Omit this field if no rewrite is needed.",
                },
                alternate_markdown: {
                  type: "string",
                  description:
                    "Optional second full rewrite with a slightly different voice.",
                },
              },
              required: [
                "decision",
                "issue_tags",
                "verdict_summary",
                "notes",
                "cloud_angle_strength",
                "cloud_angle_rationale",
                "assumptions",
              ],
            },
          },
        },
      ],
      messages: [
        {
          role: "system",
          content: buildSystemPrompt(),
        },
        {
          role: "user",
          content: buildUserPrompt(input),
        },
      ],
    }),
    signal: AbortSignal.timeout(OPENAI_TIMEOUT_MS),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(
      `OpenAI review request failed (${response.status}): ${body.slice(0, 400)}`,
    );
  }

  const payload = (await response.json()) as OpenAIChatResponse;
  const extracted = parseToolArguments(payload);
  if (!extracted) {
    throw new Error("OpenAI review did not return a parsable tool result.");
  }
  return normalizeAIReviewResult(config, payload, extracted);
}
