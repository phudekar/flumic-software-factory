import { lazy, Suspense, useState } from "react";

const Markdown = lazy(() => import("react-markdown"));

export function MarkdownPromptEditor({
  value,
  readOnly,
  onChange,
}: {
  value: string;
  readOnly: boolean;
  onChange: (value: string) => void;
}) {
  const [preview, setPreview] = useState(false);
  return (
    <div className="markdown-prompt">
      <div className="prompt-toolbar">
        <div className="actions" role="group" aria-label="Prompt view">
          <button
            className="secondary"
            aria-label="Write Markdown"
            aria-pressed={!preview}
            onClick={() => setPreview(false)}
          >
            Write
          </button>
          <button
            className="secondary"
            aria-label="Preview Markdown"
            aria-pressed={preview}
            onClick={() => setPreview(true)}
          >
            Preview
          </button>
        </div>
        <small>{value.length.toLocaleString()} / 16,000 characters</small>
      </div>
      {preview ? (
        <section
          className="markdown-preview"
          aria-label="Markdown prompt preview"
        >
          <Suspense fallback={<p>Loading preview…</p>}>
            <Markdown
              skipHtml
              components={{
                img: ({ alt }) => <span>[Image: {alt || "image"}]</span>,
              }}
            >
              {value || "*No additional prompt yet.*"}
            </Markdown>
          </Suspense>
        </section>
      ) : (
        <label>
          Additional system prompt
          <textarea
            className="prompt-editor"
            readOnly={readOnly}
            maxLength={16000}
            value={value}
            spellCheck={false}
            placeholder={
              "# Task instructions\n\n## Requirements\n- Describe the expected result.\n- List constraints and acceptance criteria."
            }
            onChange={(event) => onChange(event.target.value)}
          />
        </label>
      )}
      <small className="markdown-help">
        Markdown: # headings · - lists · **bold** · `inline code` · fenced code
        blocks with ```
      </small>
    </div>
  );
}
