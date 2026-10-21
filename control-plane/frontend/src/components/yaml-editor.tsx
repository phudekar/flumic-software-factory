import { useEffect, useRef, useState } from "react";
import { parseDocument, stringify } from "yaml";
import { Failure } from "./ui";
import { YamlInput } from "./yaml-input";

export function YamlEditor({
  value,
  label,
  readOnly,
  onChange,
  onError,
  normalize,
  onValidate,
  maxBytes = 262144,
}: {
  value: Record<string, unknown>;
  label: string;
  readOnly: boolean;
  onChange: (value: Record<string, unknown>) => void;
  onError: (error: string | null) => void;
  normalize: (value: Record<string, unknown>) => Record<string, unknown>;
  onValidate?: (value: Record<string, unknown>) => Promise<void>;
  maxBytes?: number;
}) {
  const serialized = JSON.stringify(value);
  const lastValue = useRef(serialized);
  const [text, setText] = useState(() => stringify(value));
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [validating, setValidating] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    if (serialized !== lastValue.current) {
      generation.current++;
      lastValue.current = serialized;
      setText(stringify(JSON.parse(serialized)));
      setError(null);
      setMessage("");
      onError(null);
    }
  }, [serialized, onError]);
  useEffect(() => {
    onError(null);
    return () => {
      generation.current++;
      onError(null);
    };
  }, [onError]);
  function parse(input: string) {
    if (new TextEncoder().encode(input).length > maxBytes)
      throw new Error(`YAML must be at most ${maxBytes / 1024} KiB.`);
    const document = parseDocument(input, { uniqueKeys: true });
    if (document.errors.length || document.warnings.length)
      throw new Error((document.errors[0] ?? document.warnings[0]).message);
    const parsed: unknown = document.toJS({ maxAliasCount: 0 });
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
      throw new Error("YAML must be a mapping.");
    return { document, parsed: normalize(parsed as Record<string, unknown>) };
  }
  function failure(error: unknown) {
    const message = (error as Error).message;
    setError(message);
    onError(message);
    setMessage("");
  }
  return (
    <div className="editor-body yaml-editor">
      <label>
        {label}
        <YamlInput
          label={label}
          readOnly={readOnly}
          value={text}
          invalid={!!error}
          onChange={(next) => {
            generation.current++;
            setText(next);
            setMessage("");
            try {
              const { parsed } = parse(next);
              lastValue.current = JSON.stringify(parsed);
              setError(null);
              onError(null);
              onChange(parsed);
            } catch (error) {
              failure(error);
            }
          }}
        />
      </label>
      <div className="actions">
        <button
          type="button"
          className="secondary"
          disabled={validating}
          aria-label={`Validate ${label.charAt(0).toLowerCase() + label.slice(1)}`}
          onClick={async () => {
            const current = generation.current;
            setValidating(true);
            setMessage("");
            try {
              const { parsed } = parse(text);
              await onValidate?.(parsed);
              if (current === generation.current) {
                setError(null);
                onError(null);
                setMessage(
                  onValidate ? "Configuration is valid." : "YAML is valid.",
                );
              }
            } catch (error) {
              if (current === generation.current) failure(error);
            } finally {
              setValidating(false);
            }
          }}
        >
          {validating ? "Validating…" : "Validate"}
        </button>
        <button
          type="button"
          className="secondary"
          disabled={readOnly}
          aria-label={`Format ${label.charAt(0).toLowerCase() + label.slice(1)}`}
          onClick={() => {
            try {
              const { document } = parse(text);
              generation.current++;
              setText(document.toString({ indent: 2 }));
              setMessage("YAML formatted.");
            } catch (error) {
              failure(error);
            }
          }}
        >
          Format YAML
        </button>
      </div>
      <Failure error={error ?? ""} />
      {message && <small role="status">{message}</small>}
    </div>
  );
}
