FROM python:3.12-slim

WORKDIR /app

# Install build essentials only if needed by cryptg, then clean up
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Create standard mount points for homelab volumes
RUN mkdir -p /downloads /media /mnt

ENV PYTHONUNBUFFERED=1
ENV WEB_HOST=0.0.0.0
ENV WEB_PORT=8000
ENV DOWNLOAD_DIR=/downloads

EXPOSE 8000

CMD ["python", "src/tbmd.py", "--web"]
