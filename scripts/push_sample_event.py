from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Works both from the repository and when copied into /worker in the image.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from factory.settings import Settings

from factory.adapters.orchestration.hatchet import create_client


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("issue_number", type=int)
    args = parser.parse_args()
    settings = Settings.load()
    repository = settings.repository
    create_client(settings).event.push(
        event_key="issue-agent:issue",
        payload={"repository": repository, "issue_number": args.issue_number},
    )
    print(f"Queued issue #{args.issue_number} from {repository}")


if __name__ == "__main__":
    main()
