from __future__ import annotations

import argparse
import os

from hatchet_sdk import Hatchet


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("issue_number", type=int)
    args = parser.parse_args()
    repository = os.getenv("GITHUB_REPOSITORY", "phudekar/software-factory")
    Hatchet().event.push(
        event_key="issue-agent:issue",
        payload={"repository": repository, "issue_number": args.issue_number},
    )
    print(f"Queued issue #{args.issue_number} from {repository}")


if __name__ == "__main__":
    main()
