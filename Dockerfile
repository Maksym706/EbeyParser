FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY ebeyparser ./ebeyparser
COPY config.example.yaml .env.example ./
RUN pip install --no-cache-dir ".[claude]"

EXPOSE 8000
CMD ["python", "-m", "ebeyparser", "-c", "/app/config.yaml", "run", "--host", "0.0.0.0"]
