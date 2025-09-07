# Dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Gunicorn serves the Flask app; bot starts in background thread
CMD gunicorn -b 0.0.0.0:$PORT web:create_app
