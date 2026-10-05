# Container for the web app. Works locally and on Google Cloud Run.
#   docker build -t gajan-triage .
#   docker run -p 8080:8080 gajan-triage        then open http://localhost:8080
# Bring your own key in the browser. The image contains no credentials.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra api --no-install-project --no-dev
COPY triage ./triage
COPY data ./data
# On a shared deployment, only accept callers' own API keys, never the host's cloud project.
ENV TRIAGE_ALLOW_GCP_PROJECT=0 PORT=8080 PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
CMD ["sh", "-c", "uvicorn triage.api:app --host 0.0.0.0 --port ${PORT}"]
