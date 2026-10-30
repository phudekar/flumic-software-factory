import { useState } from "react";
import { useSetupForm } from "../hooks";
import { Failure } from "../components/ui";
import { useScopedWrite } from "../project-context";
export function RepositoryHandover({
  repository,
}: {
  repository: { id: string; alias: string; owner: string; name: string };
}) {
  const [open, setOpen] = useState(false),
    [confirmed, setConfirmed] = useState(false),
    [pending, setPending] = useState(false),
    [error, setError] = useState(""),
    [complete, setComplete] = useState(false);
  const { write, alive } = useScopedWrite();
  const formRef = useSetupForm(open);
  return (
    <div className="setup-advanced">
      <h4>Repository execution</h4>
      <p>
        Enable project workflows for this repository connection. No workflow
        starts until you explicitly launch it.
      </p>
      {complete ? (
        <p role="status">Project execution enabled for {repository.alias}.</p>
      ) : !open ? (
        <button
          onClick={() => {
            setOpen(true);
            setError("");
            setConfirmed(false);
          }}
        >
          Review project execution for {repository.alias}
        </button>
      ) : (
        <form
          ref={formRef}
          onSubmit={async (e) => {
            e.preventDefault();
            if (!confirmed || pending) return;
            setPending(true);
            setError("");
            try {
              await write(
                "/repositories/" + repository.id + "/dynamic-mode",
                {},
              );
              if (alive.current) {
                setComplete(true);
                setOpen(false);
              }
            } catch (e) {
              if (alive.current) setError((e as Error).message);
            } finally {
              if (alive.current) setPending(false);
            }
          }}
        >
          <p>
            Target:{" "}
            <strong>
              {repository.owner}/{repository.name}
            </strong>{" "}
            (alias {repository.alias}). Confirm this is the correct target and
            review its execution ownership before enabling project workflows.
          </p>
          <label className="checkbox-label">
            <input
              type="checkbox"
              required
              disabled={pending}
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            I have reviewed repository ownership and drained and stopped legacy
            workers for this repository, or verified that none are running.
          </label>
          <Failure error={error} />
          <p>
            <a
              href="https://phudekar.github.io/flumic-software-factory/handbook/project-runtime-operations/#roll-out-from-a-current-checkout"
              target="_blank"
              rel="noreferrer"
            >
              Moving an existing installation? Read the migration guide.
            </a>
          </p>
          <p>
            The server checks execution ownership before accepting this request.
          </p>
          <button className="primary" disabled={!confirmed || pending}>
            Enable project execution
          </button>
          <button
            type="button"
            disabled={pending}
            onClick={() => setOpen(false)}
          >
            Cancel
          </button>
        </form>
      )}
    </div>
  );
}
