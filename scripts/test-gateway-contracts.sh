#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
image=ghcr.io/berriai/litellm@sha256:37e2dafb088a7723beb998590682957386dfc83ffea1848486f8e0e5cfac0b06
fixture="$(mktemp -d "$PWD/.gateway-contract.XXXXXX")"
name="flumic-gateway-contract-${fixture##*.}"
cleanup() {
    docker rm -f "$name-proxy" "$name-upstream" >/dev/null 2>&1 || true
    docker network rm "$name" >/dev/null 2>&1 || true
    rm -rf "$fixture"
}
trap cleanup EXIT
# Download only the checksum-verified tokenizer asset before creating the offline fixture.
docker run --rm -e TIKTOKEN_CACHE_DIR=/cache -v "$fixture:/cache" \
    --entrypoint python "$image" -c 'import tiktoken; tiktoken.encoding_for_model("gpt-4.1-mini")'
docker network create --internal "$name" >/dev/null
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=flumic-fixture' \
    -addext 'subjectAltName=IP:127.0.0.1' -keyout "$fixture/server.key" \
    -out "$fixture/server.crt" 2>/dev/null
cat > "$fixture/config.yaml" <<'CONFIG'
model_list:
  - model_name: planning
    litellm_params:
      model: openai/gpt-4.1-mini
      api_base: http://fake-upstream:8080/v1
      api_key: sk-fake-upstream
router_settings:
  num_retries: 0
litellm_settings:
  drop_params: false
general_settings:
  master_key: sk-flumic-fixture
CONFIG

fixture_source="$PWD/tests/integration/gateway_fixture.py"
docker run -d --name "$name-upstream" --network "$name" --network-alias fake-upstream \
    -v "$fixture_source:/gateway_fixture.py:ro" --entrypoint python "$image" \
    /gateway_fixture.py upstream >/dev/null

docker run -d --name "$name-proxy" --network "$name" \
    -e LITELLM_LOCAL_MODEL_COST_MAP=True -e LITELLM_TELEMETRY=False \
    -v "$fixture:/fixture:ro" -v "$fixture_source:/gateway_fixture.py:ro" \
    -v "$PWD/worker:/worker:ro" --entrypoint litellm "$image" \
    --config /fixture/config.yaml --host 127.0.0.1 --port 4000 \
    --ssl_keyfile_path /fixture/server.key --ssl_certfile_path /fixture/server.crt >/dev/null

docker image inspect "$image" --format 'Image {{index .RepoDigests 0}} ({{.Os}}/{{.Architecture}})'
if ! docker exec -e TIKTOKEN_CACHE_DIR=/fixture -e PYTHONPATH=/worker "$name-proxy" python /gateway_fixture.py probe; then
    docker logs --tail 40 "$name-proxy" >&2
    exit 1
fi

expected_requests=1
if [[ -n "${FLUMIC_GATEWAY_WORKER_TEST_IMAGE:-}" ]]; then
    docker run --rm --network "container:$name-proxy" \
        -e FLUMIC_TEST_LITELLM_ENDPOINT=https://127.0.0.1:4000/v1 \
        -e FLUMIC_TEST_LITELLM_CA_FILE=/fixture/server.crt \
        -e TIKTOKEN_CACHE_DIR=/fixture -e PYTHONDONTWRITEBYTECODE=1 \
        -e PYTHONPATH=/repo/worker:/repo/control-plane/backend:/repo/control-plane/backend/tests \
        -v "$fixture:/fixture:ro" -v "$PWD:/repo:ro" -w /repo \
        --entrypoint python "$FLUMIC_GATEWAY_WORKER_TEST_IMAGE" \
        -m unittest test_model_broker.DirectGatewayWorkerTests.test_real_local_litellm_worker_dispatch -v
    expected_requests=2
fi

upstream_requests="$(docker logs "$name-upstream" 2>&1 | grep -c 'POST /v1/chat/completions' || true)"
if [[ "$upstream_requests" != "$expected_requests" ]]; then
    echo "Expected $expected_requests fake upstream completions, observed $upstream_requests" >&2
    exit 1
fi
