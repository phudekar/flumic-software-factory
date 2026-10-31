# Legacy retirement maintenance

Implementation is in progress. The available command performs **inspection only**;
there is no deletion switch. Do not manually delete records based on its output.

Run from the repository root with the backend dependencies installed and
`CONTROL_DATABASE_URL` supplied through your private deployment environment:

```sh
PYTHONPATH=worker:control-plane/backend .venv/bin/python \
  scripts/retire-legacy-runtime.py --output /private/path/retirement-manifest.json
```

Choose a private existing parent directory and a new output filename. The command
refuses existing files and symlinks. It creates the manifest with mode `0600`,
prints only counts, and uses a read-only consistent PostgreSQL transaction.
Keep the manifest outside the repository: it contains internal execution IDs.

Inspection selects workers explicitly marked `legacy` and requires stopped or
removed state, no pending lifecycle work and no retained assignment/runtime binding
references. Events must belong to those exact worker IDs and have no project
assignment or execution attribution. Conversation candidates must join those event
IDs. Sharing the Default project ID is never a deletion criterion.

The fingerprint changes when candidate contents change, without exposing those
contents. A successful inspection is not proof that a backup can be restored or
that writers are paused. The final cleanup requires both checks, a preservation
comparison and explicit transactional apply support, which are not implemented yet.

Current project settings, credentials, definitions, project runs and shared worker
history remain intact. Hatchet/SigNoz historical data follows their own retention.
