import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function MarkdownPreview({
  markdown,
  compact = false,
}: {
  markdown: string;
  compact?: boolean;
}) {
  return (
    <div className={`markdown-preview ${compact ? "markdown-preview-compact" : ""}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ ...props }) => (
            <a
              {...props}
              rel="noreferrer noopener"
              target="_blank"
            />
          ),
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
