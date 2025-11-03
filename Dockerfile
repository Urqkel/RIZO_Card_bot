# ---- Base ----
FROM python:3.12-slim

# ---- System deps ----
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    libtesseract-dev \
    && rm -rf /var/lib/apt/lists/*

# ---- Working dir ----
WORKDIR /app

# ---- Install Python deps ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Copy app ----
COPY . .

# ---- Expose port for FastAPI ----
EXPOSE 10000

# ---- Start app ----
CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "10000"]
