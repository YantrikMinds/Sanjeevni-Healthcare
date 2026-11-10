# Use Python 3.12-slim for compatibility and size
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies (required for some ML libraries)
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements from backend directory
COPY backend/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the backend code into /app/backend to maintain package structure
COPY backend ./backend

# CRITICAL: Copy data directory (Required for Cohort Engine)
COPY data ./data

# Create a writable directory for the model (Hugging Face Spaces requirement)
RUN mkdir -p /app/backend/models/weights && chmod -R 777 /app/backend/models/weights

# Expose the port
EXPOSE 8001

# Run the application
# We run from /app so 'backend.api' is resolved correctly
# Run with Gunicorn for better concurrency
# Workers: 2 (Balance between concurrency and RAM usage for 4GB model)
# Timeout: 120s (Generous for slow LLM inference)
CMD ["gunicorn", "backend.api:app", "--workers", "2", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8001", "--timeout", "120"]
