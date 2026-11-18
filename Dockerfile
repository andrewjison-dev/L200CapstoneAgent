FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONCIERGE_ENV_MODE=isolated \
    PORT=8080

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY concierge_agent/ ./concierge_agent/
COPY evals/ ./evals/
COPY pyproject.toml README.md ./

EXPOSE 8080

CMD ["python3", "-m", "concierge_agent.ui.server", "--host", "0.0.0.0", "--port", "8080"]
