# --- Stage 1: Builder (Dependencies) ---
# Pin to specific digest for reproducible builds and supply chain security
# python:3.11 as of 2025-11-06
FROM python:3.11@sha256:e8ab764baee5109566456913b42d7d4ad97c13385e4002973c896e1dd5f01146 as builder

WORKDIR /usr/src/app

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements first for optimal caching
COPY requirements.txt .

# Install dependencies (this layer will be cached)
RUN pip install --no-cache-dir -r requirements.txt

# --- Stage 2: Final Runtime Image ---
# Pin to same digest as builder for consistency
FROM python:3.11@sha256:e8ab764baee5109566456913b42d7d4ad97c13385e4002973c896e1dd5f01146

# Install sox (audio), curl (downloads), unzip (model extraction)
RUN apt-get update \
    && apt-get install -y --no-install-recommends sox curl unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create non-root user for security and grant access to asterisk group (GID 995)
RUN groupadd -g 995 asterisk || true \
    && useradd --create-home appuser \
    && usermod -aG 995 appuser

# Copy the virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Copy application source code
COPY --chown=appuser:appuser src/ ./src
COPY --chown=appuser:appuser config/ ./config
COPY --chown=appuser:appuser main.py ./

# Prepare log directory for file logging
RUN mkdir -p /app/logs && chown appuser:appuser /app/logs

# Set PATH for virtual environment
ENV PATH="/opt/venv/bin:$PATH"

# Run the application
USER appuser
CMD ["python", "main.py"]