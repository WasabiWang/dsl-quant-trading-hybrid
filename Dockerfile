FROM python:3.11-slim

WORKDIR /app

# OS deps for ta-lib and other native packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8888/api/version || exit 1

CMD ["python", "web_dashboard/server.py", "--host", "0.0.0.0", "--port", "8888"]
