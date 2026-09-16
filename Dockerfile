FROM python:3.11-slim

# System libraries for OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}