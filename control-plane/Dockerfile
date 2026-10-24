FROM node:24-bookworm-slim AS frontend
WORKDIR /ui
COPY control-plane/frontend/package*.json ./
RUN npm ci
COPY control-plane/frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY control-plane/backend/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY control-plane/backend /app/backend
COPY worker/factory /app/worker/factory
COPY worker/github_client.py /app/worker/github_client.py
COPY --from=frontend /ui/dist /app/frontend
ENV PYTHONPATH=/app/backend:/app/worker CONTROL_FRONTEND=/app/frontend
WORKDIR /app/backend
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
