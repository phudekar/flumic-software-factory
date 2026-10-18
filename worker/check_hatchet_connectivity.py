from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

import grpc
from openai import APIError, OpenAI
from hatchet_sdk import Hatchet
from hatchet_sdk.connection import new_conn


def check_hatchet(timeout: float) -> bool:
    try:
        client = Hatchet()
    except (ValueError, OSError) as error:
        print(
            f"Hatchet client configuration failed ({type(error).__name__}). "
            "Check the loaded development profile.",
            file=sys.stderr,
        )
        return False

    config = client.config
    try:
        parsed_endpoint = urlsplit(f"//{config.host_port}")
        hostname = parsed_endpoint.hostname
        port = parsed_endpoint.port
    except ValueError:
        hostname = None
        port = None

    if hostname is None or port is None:
        print("Hatchet endpoint is missing a valid host or port.", file=sys.stderr)
        return False

    print(
        f"Checking Hatchet endpoint {hostname}:{port} "
        f"(TLS strategy: {config.tls_config.strategy})"
    )

    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as error:
        print(f"DNS lookup failed: {error}", file=sys.stderr)
        return False

    resolved = sorted({address[4][0] for address in addresses})
    print(f"DNS resolved: {', '.join(resolved)}")

    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            print("TCP connection succeeded")
    except OSError as error:
        print(f"TCP connection failed: {error}", file=sys.stderr)
        return False

    channel = new_conn(config, aio=False)
    try:
        grpc.channel_ready_future(channel).result(timeout=timeout)
    except grpc.FutureTimeoutError:
        print(
            f"gRPC channel did not become ready within {timeout:g} seconds.",
            file=sys.stderr,
        )
        return False
    finally:
        channel.close()

    print(
        "gRPC channel is ready. "
        "This check does not validate Hatchet API permissions."
    )
    return True


def check_github(timeout: float) -> bool:
    token = os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY", "phudekar/software-factory")
    if not token:
        print("GITHUB_TOKEN is not set in the container.", file=sys.stderr)
        return False

    if not repository or "/" not in repository:
        print("GITHUB_REPOSITORY must be in owner/name form.", file=sys.stderr)
        return False

    owner, name = repository.split("/", maxsplit=1)
    endpoint = "https://api.github.com/repos/{}/{}".format(
        urllib.parse.quote(owner, safe=""),
        urllib.parse.quote(name, safe=""),
    )
    print(f"Checking authenticated GitHub API access to {repository}")
    request = urllib.request.Request(
        endpoint,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hatchet-issue-agent-connectivity-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            repository_data = json.load(response)
    except urllib.error.HTTPError as error:
        print(
            f"GitHub API rejected the request with HTTP {error.code}. "
            "Check token validity and repository access.",
            file=sys.stderr,
        )
        return False
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        print("GitHub API request failed due to a network or response error.", file=sys.stderr)
        return False

    full_name = repository_data.get("full_name")
    if not isinstance(full_name, str) or full_name.lower() != repository.lower():
        print("GitHub API returned an unexpected repository.", file=sys.stderr)
        return False

    print("GitHub HTTPS request succeeded; token can read the repository.")
    return True


def check_openai_model(timeout: float) -> bool:
    api_key = os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
    if not api_key:
        print("OPENAI_API_KEY is not set in the container.", file=sys.stderr)
        return False

    print(f"Checking OpenAI chat-completions access for model {model}")
    client = OpenAI(api_key=api_key, timeout=timeout, max_retries=0)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Return a JSON object."},
                {"role": "user", "content": "Connectivity check."},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=16,
        )
    except APIError as error:
        status = getattr(error, "status_code", None)
        code = getattr(error, "code", None)
        error_type = getattr(error, "type", None)
        details = []
        if status is not None:
            details.append(f"HTTP {status}")
        if code:
            details.append(f"code={code}")
        if error_type:
            details.append(f"type={error_type}")
        detail_text = ", ".join(details) or type(error).__name__
        print(f"OpenAI model request failed ({detail_text}).", file=sys.stderr)
        return False

    if not response.choices:
        print("OpenAI returned no completion choices.", file=sys.stderr)
        return False

    print("OpenAI chat-completions request succeeded.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check Hatchet, GitHub, and OpenAI connectivity and access from "
            "the worker container."
        )
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Timeout in seconds for each network request (default: 10)",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    hatchet_ok = check_hatchet(args.timeout)
    github_ok = check_github(args.timeout)
    openai_ok = check_openai_model(args.timeout)
    return 0 if hatchet_ok and github_ok and openai_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
