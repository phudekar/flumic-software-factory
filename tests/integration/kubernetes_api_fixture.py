"""Private isolated API process for Pod bootstrap/assignment qualification only."""

import json
import os
from pathlib import Path
import sys
import time


def main():
    if os.environ.get("FLUMIC_LIVE_KUBERNETES") != "1":
        raise SystemExit("Explicit local fixture opt-in required")
    path = Path(os.environ["FLUMIC_KUBERNETES_API_FIXTURE_CONFIG"])
    deadline = time.monotonic() + 90
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    document = json.loads(path.read_text())
    root = Path(document["root"])
    sys.path[:0] = [str(root / "control-plane/backend"), str(root / "worker")]
    from app.settings import AppSettings
    from app.main import create_app
    import uvicorn

    for field in (
        "root",
        "host_root",
        "snapshot_root",
        "frontend",
        "runtime_targets_file",
    ):
        if document.get(field) is not None:
            document[field] = Path(document[field])
    app = create_app(AppSettings(**document))
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
