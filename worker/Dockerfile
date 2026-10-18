FROM alpine:3.22

RUN apk add --no-cache python3 py3-pip ca-certificates \
    && python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /worker

COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker/bootstrap.sh worker/worker.py worker/guardrails.py worker/model_client.py worker/github_client.py ./
COPY worker/check_hatchet_connectivity.py scripts/push_sample_event.py ./
RUN chmod +x /worker/bootstrap.sh

ENTRYPOINT ["/worker/bootstrap.sh"]
