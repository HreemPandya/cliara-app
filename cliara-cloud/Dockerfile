FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects PORT at runtime; default to 8000 for local dev
ENV PORT=8000

EXPOSE 8000

# Use shell form so $PORT is expanded at container start from Railway's env.
# Single worker avoids startup race; healthcheck has 30s window.
CMD exec uvicorn main:app --host 0.0.0.0 --port $PORT
