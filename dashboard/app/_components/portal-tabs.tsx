import Link from "next/link";

export function PortalTabs({
  current,
}: {
  current: "overview" | "review";
}) {
  return (
    <nav className="portal-tabs" aria-label="Control portal sections">
      <Link
        aria-current={current === "overview" ? "page" : undefined}
        className={`portal-tab ${current === "overview" ? "portal-tab-active" : ""}`}
        href="/"
      >
        Control Room
      </Link>
      <Link
        aria-current={current === "review" ? "page" : undefined}
        className={`portal-tab ${current === "review" ? "portal-tab-active" : ""}`}
        href="/review"
      >
        Human Labels
      </Link>
    </nav>
  );
}
