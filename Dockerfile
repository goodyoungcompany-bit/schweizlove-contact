FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# Ein Worker mit mehreren Threads: das Rate-Limit liegt im Arbeitsspeicher und gilt so fuer alle Anfragen
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 60"]
