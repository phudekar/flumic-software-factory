# Run `just` to list shortcuts. Recipes run from the repository root.
set positional-arguments

python := env("FLUMIC_PYTHON", ".venv/bin/python")

# List available commands without starting services.
default:
    @just --list

# Start control services (arguments select services or Compose options).
control-up *args:
    ./scripts/start-control-plane.sh "$@"

# Build both worker images without starting containers.
images-build *args:
    ./scripts/build-worker-images.sh "$@"

# Stop control services, preserving database volumes. Keep the same DB mode as startup.
control-down *args:
    ./scripts/control-compose.sh down "$@"

# Follow control-service logs; optional arguments select services or log options.
control-logs *args:
    ./scripts/control-compose.sh logs -f "$@"

# Start the optional local gateway; does not change Flumic model routing.
gateway-up *args:
    ./scripts/start-gateway.sh "$@"

gateway-down *args:
    ./scripts/gateway-compose.sh down "$@"

gateway-logs *args:
    ./scripts/gateway-compose.sh logs -f "$@"

# Start SigNoz dashboards and resource metrics.
observability-up *args:
    ./scripts/start-observability.sh "$@"

observability-down *args:
    ./scripts/observability-compose.sh down "$@"

observability-logs *args:
    ./scripts/observability-compose.sh logs -f "$@"

# Reconcile the factory dashboard explicitly; monitoring remains independent.
dashboards-provision *args:
    ./scripts/provision-dashboards.sh "$@"

metrics-up *args:
    ./scripts/start-resource-metrics.sh "$@"

metrics-down *args:
    ./scripts/compose-command.sh -f compose.resource-metrics.yaml down "$@"

metrics-logs *args:
    ./scripts/compose-command.sh -f compose.resource-metrics.yaml logs -f "$@"

# Check connectivity to the Hatchet development profile through Docker.
hatchet-check *args:
    ./scripts/check-hatchet-connectivity.sh "$@"

# Install frontend and website dependencies using locked versions.
install-ui:
    npm ci --prefix control-plane/frontend

install-docs:
    npm ci --prefix website

# Run the documentation development server; forward Astro CLI options after --.
docs-dev *args:
    npm run dev --prefix website -- "$@"

# Build and validate the documentation site, including links and generated content.
docs-check:
    npm run build --prefix website
    npm run check --prefix website

# Preview the built docs; run docs-check first.
docs-preview *args:
    npm run preview --prefix website -- "$@"

# Run all default unit test suites (no opt-in integration contracts).
test: test-worker test-backend test-ui

test-worker:
    {{ quote(python) }} -m unittest discover -s tests -q

test-backend:
    PYTHONPATH=worker:control-plane/backend {{ quote(python) }} -m unittest discover -s control-plane/backend/tests -q

test-ui:
    npm test --prefix control-plane/frontend

# Compile/type-check and bundle the Control Pane frontend.
ui-build:
    npm run build --prefix control-plane/frontend

# Run the Control Pane frontend development server.
ui-dev *args:
    npm run dev --prefix control-plane/frontend -- "$@"
