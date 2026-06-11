# Dockerfile — Production image for LangGraph Server
#
# Build:  docker build -t zuixiaoagent .
# Run:    docker run -p 2024:2024 --env-file .env zuixiaoagent

FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency files
COPY requirements.txt .
COPY setup.py .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY agent/ ./agent/
COPY memory/ ./memory/
COPY tools/ ./tools/
COPY plugins/ ./plugins/
COPY server/ ./server/
COPY langgraph.json .

# Install as package
RUN pip install -e .

# Create data directory
RUN mkdir -p /app/data

# Expose LangGraph Server port
EXPOSE 2024

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:2024/health || exit 1

# Start LangGraph server
CMD ["langgraph", "up", "--host", "0.0.0.0", "--port", "2024"]
