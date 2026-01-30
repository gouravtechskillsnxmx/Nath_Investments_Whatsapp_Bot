# ---- Base image ----
FROM python:3.11-slim

# ---- Environment ----
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ---- System deps (minimal) ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ---- Workdir ----
WORKDIR /app

# ---- Install Python deps first (better caching) ----
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ---- Copy app code ----
COPY app.py /app/app.py

#-------copy image--------

COPY "nath investment.jpeg" /app/"nath investment.jpeg"

# ---- Expose port ----
EXPOSE 8000

# ---- Start server ----
# Note: For Render, they provide $PORT. Locally it'll use 8000 unless you override.
CMD ["bash", "-lc", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
