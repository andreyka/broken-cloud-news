import Image from "next/image";
import Link from "next/link";

import { PortalTabs } from "@/app/_components/portal-tabs";
import {
  getHumanReviewBriefing,
  getHumanReviewQueue,
  type BriefingReviewQueueEntry,
} from "@/lib/db";

export const dynamic = "force-dynamic";

const ISSUE_TAGS = [
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

type SearchParams = Record<string, string | string[] | undefined>;

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

function issueTagLabel(value: string): string {
  return value.replaceAll("_", " ");
}

function reviewTone(
  decision: string | null,
): "completed" | "failed" | "queued" {
  if (decision === "accept" || decision === "edit") {
    return "completed";
  }
  if (decision === "reject") {
    return "failed";
  }
  return "queued";
}

function queueLink(
  entry: BriefingReviewQueueEntry,
  onlyUnreviewed: boolean,
): string {
  const query = new URLSearchParams({ briefingId: entry.id });
  if (onlyUnreviewed) {
    query.set("scope", "unreviewed");
  }
  return `/review?${query.toString()}`;
}

type ReviewPageProps = {
  searchParams?: Promise<SearchParams>;
};

export default async function ReviewPage({
  searchParams,
}: ReviewPageProps) {
  const resolvedSearchParams = (await searchParams) || {};
  const onlyUnreviewed = searchParamValue(resolvedSearchParams, "scope") === "unreviewed";
  const requestedBriefingId = searchParamValue(resolvedSearchParams, "briefingId");
  const noticeMessage = searchParamValue(resolvedSearchParams, "reviewMessage");
  const noticeStatus = searchParamValue(resolvedSearchParams, "reviewStatus");

  const queue = await getHumanReviewQueue(18, onlyUnreviewed);
  const selectedBriefingId = requestedBriefingId || queue[0]?.id || null;
  const detail = selectedBriefingId
    ? await getHumanReviewBriefing(selectedBriefingId)
    : null;
  const selectedId = detail?.briefing.id || selectedBriefingId;
  const reviewedCount = queue.filter((entry) => entry.reviewCount > 0).length;

  return (
    <main className="shell">
      <PortalTabs current="review" />

      <section className="hero compact-hero">
        <div className="brand-lockup brand-lockup-compact">
          <div className="brand-logo-frame brand-logo-frame-compact">
            <Image
              src="/logo.png"
              alt="Broken Cloud News"
              width={120}
              height={120}
              className="brand-logo brand-logo-compact"
              priority
            />
          </div>
          <div>
            <p className="eyebrow">Human Labels</p>
            <h1>Editorial review lab</h1>
            <p className="lede">
              This page is for training signal, not telemetry. Label finished
              briefings with human decisions, issue tags, notes, and optional
              edited markdown for future SFT and preference tuning.
            </p>
          </div>
        </div>
        <div className="hero-note">
          <span>Queue focus</span>
          <strong>{queue.length}</strong>
          <small>
            {reviewedCount} reviewed in this view · {onlyUnreviewed ? "unreviewed only" : "all recent briefings"}
          </small>
        </div>
      </section>

      {noticeMessage ? (
        <div className={`queue-notice queue-notice-${noticeStatus === "success" ? "completed" : "failed"}`}>
          <span className={`run-state run-state-${noticeStatus === "success" ? "completed" : "failed"}`}>
            {noticeStatus === "success" ? "saved" : "error"}
          </span>
          <strong>{noticeMessage}</strong>
        </div>
      ) : null}

      <div className="review-toolbar">
        <div>
          <p className="eyebrow">Label types</p>
          <p className="lane-copy">
            Highest ROI labels are editorial output labels: accept/reject/edit/needs-work,
            issue tags, and edited markdown. Analyst/source labeling can come later.
          </p>
        </div>
        <div className="review-toolbar-actions">
          <Link
            className={`portal-filter ${onlyUnreviewed ? "portal-filter-active" : ""}`}
            href="/review?scope=unreviewed"
          >
            Unreviewed only
          </Link>
          <Link
            className={`portal-filter ${onlyUnreviewed ? "" : "portal-filter-active"}`}
            href="/review"
          >
            All recent
          </Link>
        </div>
      </div>

      <section className="review-grid">
        <aside className="panel review-queue-panel">
          <div className="section-head">
            <div>
              <p className="eyebrow">Review queue</p>
              <h2>Recent briefings</h2>
            </div>
            <p className="muted">Pick one briefing at a time and add grounded human labels.</p>
          </div>
          {queue.length === 0 ? (
            <p className="lane-copy">No briefings in scope for review.</p>
          ) : (
            <div className="review-queue-list">
              {queue.map((entry) => (
                <Link
                  className={`review-queue-card ${selectedId === entry.id ? "review-queue-card-active" : ""}`}
                  href={queueLink(entry, onlyUnreviewed)}
                  key={entry.id}
                >
                  <div className="review-queue-head">
                    <span className={`run-state run-state-${reviewTone(entry.lastDecision)}`}>
                      {entry.lastDecision || "pending"}
                    </span>
                    <span className="lane-meta">{formatDate(entry.createdAt)}</span>
                  </div>
                  <strong>{entry.status}</strong>
                  <p className="lane-copy">{entry.preview || "No preview available."}</p>
                  <div className="review-queue-meta">
                    <span>{entry.reviewCount} review(s)</span>
                    <span>Distributed {formatDate(entry.distributedAt)}</span>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </aside>

        <section className="panel review-detail-panel">
          {!detail ? (
            <>
              <div className="section-head">
                <div>
                  <p className="eyebrow">Briefing detail</p>
                  <h2>Select a briefing</h2>
                </div>
              </div>
              <p className="lane-copy">
                Choose a briefing from the queue to store a human review.
              </p>
            </>
          ) : (
            <>
              <div className="section-head">
                <div>
                  <p className="eyebrow">Briefing detail</p>
                  <h2>{detail.briefing.status}</h2>
                </div>
                <div className="lane-head-meta">
                  <span className="run-state run-state-completed">
                    {detail.latestRun?.decision || "n/a"}
                  </span>
                  <span className="lane-timestamp">
                    {formatDate(detail.briefing.createdAt)} UTC
                  </span>
                </div>
              </div>

              <div className="metric-grid detail-grid">
                <div className="metric-cell">
                  <span className="metric-label">Briefing</span>
                  <strong className="metric-value">{detail.briefing.id.slice(0, 8)}</strong>
                </div>
                <div className="metric-cell">
                  <span className="metric-label">Latest model</span>
                  <strong className="metric-value">
                    {detail.latestRun?.llmModel || "n/a"}
                  </strong>
                </div>
                <div className="metric-cell">
                  <span className="metric-label">Rewrites</span>
                  <strong className="metric-value">
                    {String(detail.latestRun?.rewriteCount || 0)}
                  </strong>
                </div>
                <div className="metric-cell">
                  <span className="metric-label">Stored reviews</span>
                  <strong className="metric-value">{String(detail.reviews.length)}</strong>
                </div>
              </div>

              <div className="review-detail-grid">
                <section className="review-block">
                  <div className="review-block-head">
                    <h3>Current markdown</h3>
                    <span className="lane-meta">
                      Use this as the reference draft when you add labels.
                    </span>
                  </div>
                  <pre className="briefing-markdown">{detail.briefing.contentMarkdown}</pre>
                </section>

                <section className="review-block">
                  <div className="review-block-head">
                    <h3>Store human review</h3>
                    <span className="lane-meta">
                      This is the highest-value tuning signal BCN can collect right now.
                    </span>
                  </div>

                  <form action="/api/reviews/submit" className="review-form" method="post">
                    <input name="briefingId" type="hidden" value={detail.briefing.id} />
                    <input name="reviewer" type="hidden" value="portal" />
                    <input
                      name="redirectTo"
                      type="hidden"
                      value={
                        onlyUnreviewed
                          ? `/review?scope=unreviewed&briefingId=${detail.briefing.id}`
                          : `/review?briefingId=${detail.briefing.id}`
                      }
                    />

                    <label className="review-field">
                      <span className="review-label">Decision</span>
                      <div className="review-decision-grid">
                        {[
                          { value: "accept", copy: "Good enough to keep." },
                          { value: "edit", copy: "Keep it, but a human rewrite is better." },
                          { value: "needs_work", copy: "Useful draft, not publish-ready." },
                          { value: "reject", copy: "Do not use this version." },
                        ].map((option, index) => (
                          <label className="review-decision-option" key={option.value}>
                            <input
                              defaultChecked={index === 0}
                              name="decision"
                              type="radio"
                              value={option.value}
                            />
                            <span>
                              <strong>{option.value}</strong>
                              <small>{option.copy}</small>
                            </span>
                          </label>
                        ))}
                      </div>
                    </label>

                    <fieldset className="review-field review-fieldset">
                      <legend className="review-label">Issue tags</legend>
                      <div className="review-chip-grid">
                        {ISSUE_TAGS.map((tag) => (
                          <label className="review-chip" key={tag}>
                            <input name="issueTag" type="checkbox" value={tag} />
                            <span>{issueTagLabel(tag)}</span>
                          </label>
                        ))}
                      </div>
                    </fieldset>

                    <label className="review-field">
                      <span className="review-label">Notes</span>
                      <textarea
                        className="review-input review-textarea review-notes"
                        name="notes"
                        placeholder="Optional. Short note about the main problem or why this draft is acceptable."
                      />
                    </label>

                    <details className="review-optional">
                      <summary>Optional gold edit</summary>
                      <p className="lane-copy">
                        Only open this if you want to provide the corrected full markdown.
                        That is the best SFT signal, but it should stay optional so review stays fast.
                      </p>
                      <label className="review-field">
                        <span className="review-label">Edited markdown</span>
                        <textarea
                          className="review-input review-textarea review-editor"
                          name="editedMarkdown"
                          placeholder="Paste the corrected full markdown here."
                        />
                      </label>
                    </details>

                    <div className="review-submit-row">
                      <button className="control-button" type="submit">
                        Save review
                      </button>
                      <span className="lane-meta">
                        Fast path: choose a decision, add 0-2 issue tags, save.
                      </span>
                    </div>
                  </form>
                </section>
              </div>

              <section className="review-block">
                <div className="review-block-head">
                  <h3>Stored reviews</h3>
                  <span className="lane-meta">Newest first.</span>
                </div>
                {detail.reviews.length === 0 ? (
                  <p className="lane-copy">No human reviews stored for this briefing yet.</p>
                ) : (
                  <div className="selection-list">
                    {detail.reviews.map((review) => (
                      <article className="selection-item review-history-item" key={review.id}>
                        <div className="review-history-head">
                          <span className={`run-state run-state-${reviewTone(review.decision)}`}>
                            {review.decision}
                          </span>
                          <span className="lane-meta">
                            {review.reviewer} · {formatDate(review.createdAt)}
                          </span>
                        </div>
                        <strong>{review.issueTags.length > 0 ? review.issueTags.join(", ") : "No issue tags"}</strong>
                        {review.notes ? <p className="lane-copy">{review.notes}</p> : null}
                        {review.editedMarkdown ? (
                          <details>
                            <summary>Show edited markdown</summary>
                            <pre className="briefing-markdown briefing-markdown-compact">
                              {review.editedMarkdown}
                            </pre>
                          </details>
                        ) : null}
                      </article>
                    ))}
                  </div>
                )}
              </section>
            </>
          )}
        </section>
      </section>
    </main>
  );
}
