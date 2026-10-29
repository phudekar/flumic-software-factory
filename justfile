# Run `just` to list shortcuts. Recipes run from the repository root.
set positional-arguments

python := env("FLUMIC_PYTHON", ".venv/bin/python")

# List available commands without starting services.
default:
    @just --list

# Start control services (arguments select services or Compose options).
control-up *args:
    ./scripts/start-control-plane.sh "$@"

# Start the legacy Compose worker fleet; may call models and write repositories.
workers-up *args:
    ./scripts/start-workers.sh -d "$@"

# Start SigNoz dashboards and resource metrics.
observability-up *args:
    ./scripts/start-observability.sh "$@"

# Check connectivity to the Hatchet development profile through Docker.
hatchet-check *args:
    ./scripts/check-hatchet-connectivity.sh "$@"

# Explicitly send an issue event to the legacy worker (may incur costs/writes).
issue-send issue:
    ./scripts/push-sample-event.sh {{ quote(issue) }}

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
