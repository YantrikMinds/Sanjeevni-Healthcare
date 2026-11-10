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

# CRITICAL: Copy shared directory (Required for triage_thresholds.json & safety_layer.py)
COPY shared ./shared

# Create a writable directory for the model (Hugging Face Spaces requirement)
RUN mkdir -p /app/backend/models/weights && chmod -R 777 /app/backend/models/weights

# Expose the port
EXPOSE 8001

# Run the application — 1 worker on free tier (512MB RAM)
CMD ["gunicorn", "backend.api:app", "--workers", "1", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8001", "--timeout", "120"]
