"""Rendering helpers for writer-produced briefing artifacts."""

from __future__ import annotations

import html
import re


def format_markdown(
    briefing_body: str,
    cover_url: str,
    *,
    mode: str = "standard",
) -> str:
    """Wrap the briefing body with an optional cover image in markdown."""
    markdown = ""
    if cover_url and cover_url.startswith(("http://", "https://")):
        alt = "Monthly Newsletter Cover" if mode == "monthly_newsletter" else "Daily Cover"
        markdown += f"![{alt}]({cover_url})\n\n"
    markdown += briefing_body
    return markdown


def format_html(
    briefing_body: str,
    cover_url: str,
    *,
    mode: str = "standard",
) -> str:
    """Convert briefing markdown-ish text to styled HTML email markup."""
    if mode == "monthly_newsletter":
        title = "Broken Cloud News Monthly Newsletter"
        subtitle = "Most interesting cloud security developments from the last month."
    else:
        title = "Broken Cloud News Briefing"
        subtitle = "Cloud security highlights, analysis, and operator guidance."

    body_html = render_html_body(briefing_body)
    cover_block = ""
    if cover_url and cover_url.startswith(("http://", "https://", "data:image/")):
        safe_cover = html.escape(cover_url, quote=True)
        cover_block = (
            "<div style=\"margin:0 0 20px 0;\">"
            f"<img src=\"{safe_cover}\" alt=\"Briefing cover\" "
            "style=\"display:block;width:100%;max-width:760px;border-radius:14px;border:1px solid #d7e3ef;\"/>"
            "</div>"
        )

    return (
        "<html><body style=\"margin:0;padding:24px;background:#f4f7fb;"
        "font-family:'Segoe UI',Arial,sans-serif;color:#142033;\">"
        "<div style=\"max-width:820px;margin:0 auto;background:#ffffff;border:1px solid #d8e3ee;"
        "border-radius:16px;overflow:hidden;box-shadow:0 10px 26px rgba(16,40,69,0.08);\">"
        "<div style=\"padding:20px 24px;background:linear-gradient(120deg,#0f243f,#1a4f7a);color:#eaf3fb;\">"
        f"<h1 style=\"margin:0 0 8px 0;font-size:28px;line-height:1.2;\">{html.escape(title)}</h1>"
        f"<p style=\"margin:0;font-size:14px;opacity:0.93;\">{html.escape(subtitle)}</p>"
        "</div>"
        "<div style=\"padding:22px 24px 26px 24px;\">"
        f"{cover_block}"
        f"{body_html}"
        "</div>"
        "</div></body></html>"
    )


def render_html_body(markdown: str) -> str:
    """Render markdown-ish digest text into readable HTML blocks."""
    parts: list[str] = []
    in_list = False
    for raw in str(markdown or "").splitlines():
        line = raw.strip()
        if not line:
            if in_list:
                parts.append("</ul>")
                in_list = False
            continue

        heading_text = ""
        heading_tag = ""
        if line.startswith("## "):
            heading_text = line[3:].strip()
            heading_tag = "h2"
        elif line.startswith("### "):
            heading_text = line[4:].strip()
            heading_tag = "h3"
        else:
            bold_heading = re.fullmatch(r"\*\*(.+?)\*\*", line)
            if bold_heading:
                heading_text = bold_heading.group(1).strip()
                heading_tag = "h3"

        if heading_tag:
            if in_list:
                parts.append("</ul>")
                in_list = False
            parts.append(
                f"<{heading_tag} style=\"margin:18px 0 10px 0;color:#143154;\">"
                f"{inline_markdown_to_html(heading_text)}"
                f"</{heading_tag}>"
            )
            continue

        if line.startswith("- ") or line.startswith("* "):
            if not in_list:
                parts.append("<ul style=\"margin:8px 0 14px 18px;padding:0;\">")
                in_list = True
            parts.append(
                "<li style=\"margin:0 0 8px 0;line-height:1.5;\">"
                f"{inline_markdown_to_html(line[2:].strip())}"
                "</li>"
            )
            continue

        if in_list:
            parts.append("</ul>")
            in_list = False
        parts.append(
            "<p style=\"margin:0 0 12px 0;line-height:1.6;color:#1a2940;\">"
            f"{inline_markdown_to_html(line)}"
            "</p>"
        )

    if in_list:
        parts.append("</ul>")
    return "\n".join(parts)


def inline_markdown_to_html(value: str) -> str:
    """Convert basic inline markdown syntax to safe HTML."""
    text = html.escape(value or "", quote=True)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" style="color:#1c5f96;text-decoration:underline;">\1</a>',
        text,
    )
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    return text


__all__ = [
    "format_html",
    "format_markdown",
    "inline_markdown_to_html",
    "render_html_body",
]
