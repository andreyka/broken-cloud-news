export const REVIEW_DECISIONS = [
  "accept",
  "reject",
  "edit",
  "needs_work",
] as const;

export const REVIEW_ISSUE_TAGS = [
  "factual_error",
  "unsupported_claim",
  "weak_cloud_focus",
  "weak_actionability",
  "weak_opener",
  "poor_structure",
  "formatting",
  "duplicate_url",
  "repeated_topic",
  "tone",
] as const;

export type ReviewDecision = (typeof REVIEW_DECISIONS)[number];
export type ReviewIssueTag = (typeof REVIEW_ISSUE_TAGS)[number];
