import { useMemo, useRef } from "react";
import type { ReactNode } from "react";
import Prism from "prismjs";
import "prismjs/components/prism-yaml";

function tokens(
  value: string | Prism.Token | (string | Prism.Token)[],
): ReactNode {
  if (typeof value === "string") return value;
  if (Array.isArray(value))
    return value.map((item, index) => <span key={index}>{tokens(item)}</span>);
  const alias = Array.isArray(value.alias)
    ? value.alias.join(" ")
    : (value.alias ?? "");
  return (
    <span className={`token ${value.type} ${alias}`}>
      {tokens(value.content)}
    </span>
  );
}

/** A native textarea keeps selection, accessibility and form behavior unchanged.
 * The inert highlighted layer tracks its scroll position and identical metrics. */
export function YamlInput({
  value,
  label,
  readOnly,
  invalid,
  onChange,
}: {
  value: string;
  label: string;
  readOnly: boolean;
  invalid: boolean;
  onChange: (value: string) => void;
}) {
  const backdrop = useRef<HTMLElement>(null);
  const highlight = useMemo(
    () => tokens(Prism.tokenize(value + "\n", Prism.languages.yaml)),
    [value],
  );
  return (
    <div className="yaml-input">
      <pre aria-hidden="true" className="yaml-highlight">
        <code ref={backdrop}>{highlight}</code>
      </pre>
      <textarea
        aria-label={label}
        readOnly={readOnly}
        value={value}
        spellCheck={false}
        aria-invalid={invalid}
        wrap="off"
        onChange={(event) => onChange(event.target.value)}
        onScroll={(event) => {
          if (backdrop.current) {
            backdrop.current.style.transform = `translate(${-event.currentTarget.scrollLeft}px, ${-event.currentTarget.scrollTop}px)`;
          }
        }}
      />
    </div>
  );
}
