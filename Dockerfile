# ---- Build stage: frontend ----
FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Runtime stage: Python backend ----
FROM python:3.12-slim
WORKDIR /app

# Install system dependencies for optional packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml ./
COPY onebookwiki/ ./onebookwiki/
COPY server/ ./server/
COPY references/ ./references/
RUN pip install --no-cache-dir -e ".[server,imports,rag]"

# Copy pre-built frontend
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Create data directories
RUN mkdir -p /app/books

EXPOSE 8000

# Use environment variables with defaults
ENV ONEBOOKWIKI_HOST=0.0.0.0
ENV ONEBOOKWIKI_PORT=8000
ENV ONEBOOKWIKI_BOOKS_ROOT=/app/books

CMD ["sh", "-c", "uvicorn server.main:app --host ${ONEBOOKWIKI_HOST} --port ${ONEBOOKWIKI_PORT}"]
