FROM python:3.12-slim

WORKDIR /app

# Install dependencies dulu (untuk Docker layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Pastikan folder logs ada
RUN mkdir -p logs credentials

CMD ["python", "main.py"]
