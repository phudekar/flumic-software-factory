import { YamlEditor } from "./yaml-editor";

const defaults: Record<string, unknown> = {
  base_image: "",
  system_packages: [],
  codex_version: "0.163.0",
  claude_version: "2.1.306",
  image_setup_script: "",
};
export function buildSettings(settings: Record<string, unknown>) {
  return Object.fromEntries(
    Object.entries(defaults).map(([key, value]) => [
      key,
      settings[key] ?? value,
    ]),
  );
}
function normalizeBuild(fields: Record<string, unknown>) {
  for (const [key, value] of Object.entries(fields)) {
    if (!Object.hasOwn(defaults, key))
      throw new Error("Unknown image build field: " + key);
    if (key === "system_packages") {
      if (
        !Array.isArray(value) ||
        value.some((item) => typeof item !== "string")
      )
        throw new Error(
          "system_packages must be a YAML list of package names.",
        );
    } else if (typeof value !== "string")
      throw new Error(key + " must be a string.");
  }
  return buildSettings(fields);
}
export function ImageBuildEditor({
  settings,
  readOnly,
  onChange,
  onError,
  onValidate,
}: {
  settings: Record<string, unknown>;
  readOnly: boolean;
  onChange: (settings: Record<string, unknown>) => void;
  onError: (error: string | null) => void;
  onValidate?: (settings: Record<string, unknown>) => Promise<void>;
}) {
  return (
    <>
      <YamlEditor
        value={buildSettings(settings)}
        label="Image build YAML"
        readOnly={readOnly}
        onChange={onChange}
        onError={onError}
        normalize={normalizeBuild}
        onValidate={onValidate}
        maxBytes={65536}
      />
      <small>
        Paste base_image, system_packages, codex_version, claude_version and
        image_setup_script. Omitted fields use defaults. Saved revisions retain
        parsed values; comments and formatting are not stored.
      </small>
    </>
  );
}
