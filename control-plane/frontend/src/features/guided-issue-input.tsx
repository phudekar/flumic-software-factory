import { Link } from "react-router-dom";
import { projectPath, useProjectId } from "../project-context";
import { Failure } from "../components/ui";
import { useProjectData } from "../project-context";
import type { Definition, DefinitionRevision } from "../workflow-api";

export function useIssueFollowups(
  repository: string,
  feedback: string,
  verify: string,
  enabled: boolean,
) {
  const definitions = useProjectData<Definition[]>(
    "/workflow-definitions",
    enabled,
  );
  const feedbackRevision = useProjectData<DefinitionRevision>(
    "/workflow-definition-revisions/" + feedback,
    enabled && !!feedback,
  );
  const verifyRevision = useProjectData<DefinitionRevision>(
    "/workflow-definition-revisions/" + verify,
    enabled && !!verify,
  );
  const matches = (
    revision: DefinitionRevision | undefined,
    kind: "feedback" | "verify",
  ) =>
    !!revision &&
    revision.id === (kind === "feedback" ? feedback : verify) &&
    revision.document.default_repository === repository &&
    revision.document.repositories.length === 1 &&
    revision.document.stages.length === 1 &&
    revision.document.stages[0].executor === "builtin" &&
    revision.document.stages[0].action ===
      (kind === "feedback" ? "pull.feedback@1" : "pull.verify@1");
  return {
    definitions,
    feedbackRevision,
    verifyRevision,
    valid:
      matches(feedbackRevision.data, "feedback") &&
      matches(verifyRevision.data, "verify") &&
      !!definitions.data?.some(
        (d) =>
          d.revision_id === feedback && d.name.startsWith("factory-feedback-"),
      ) &&
      !!definitions.data?.some(
        (d) => d.revision_id === verify && d.name.startsWith("factory-verify-"),
      ),
  };
}
export function GuidedIssueInput({
  issue,
  setIssue,
  feedback,
  setFeedback,
  verify,
  setVerify,
  followups,
  pending,
}: {
  issue: string;
  setIssue: (value: string) => void;
  feedback: string;
  setFeedback: (value: string) => void;
  verify: string;
  setVerify: (value: string) => void;
  followups: ReturnType<typeof useIssueFollowups>;
  pending: boolean;
}) {
  const project = useProjectId();
  return (
    <>
      <p>
        Run the saved issue recipe. Provider calls and repository writes can
        occur after submission. Create the issue in your source host first; no
        issue is created by this form.
      </p>
      <label>
        Issue number
        <input
          required
          type="number"
          min={1}
          step={1}
          disabled={pending}
          value={issue}
          onChange={(e) => setIssue(e.target.value)}
        />
      </label>
      <p>
        The issue follow-up uses this saved issue revision. Choose feedback and
        verification revisions for the same repository; selections are pinned at
        acceptance.
      </p>
      {(["feedback", "verify"] as const).map((kind) => (
        <label key={kind}>
          {kind === "feedback"
            ? "Feedback workflow revision"
            : "Verification workflow revision"}
          <select
            required
            disabled={pending}
            value={kind === "feedback" ? feedback : verify}
            onChange={(e) =>
              (kind === "feedback" ? setFeedback : setVerify)(e.target.value)
            }
          >
            <option value="">Choose saved revision</option>
            {followups.definitions.data
              ?.filter((d) => d.name.startsWith("factory-" + kind + "-"))
              .map((d) => (
                <option key={d.id} value={d.revision_id}>
                  {kind === "feedback" ? "Feedback" : "Verification"}:{" "}
                  {d.name
                    .replace(/^factory-(feedback|verify)-/, "")
                    .replace(/-[0-9a-f]{8}$/, "")}{" "}
                  · {d.revision_id.slice(0, 8)}
                </option>
              ))}
          </select>
        </label>
      ))}
      {followups.feedbackRevision.data && (
        <p>
          <Link
            target="_blank"
            rel="noreferrer"
            to={projectPath(project, "/workflow-revisions/" + feedback)}
          >
            Inspect feedback revision
          </Link>
        </p>
      )}
      {followups.verifyRevision.data && (
        <p>
          <Link
            target="_blank"
            rel="noreferrer"
            to={projectPath(project, "/workflow-revisions/" + verify)}
          >
            Inspect verification revision
          </Link>
        </p>
      )}
      <Failure
        error={
          followups.definitions.error ||
          followups.feedbackRevision.error ||
          followups.verifyRevision.error
        }
      />
      {feedback &&
        verify &&
        !followups.valid &&
        !followups.feedbackRevision.isPending &&
        !followups.verifyRevision.isPending && (
          <Failure error="Select compatible feedback and verification revisions for this repository." />
        )}
      <p>
        Inspect the workflow cards before selecting. Use Advanced JSON for
        custom workflows or earlier revisions not listed here.
      </p>
    </>
  );
}
