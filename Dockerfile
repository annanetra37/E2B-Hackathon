# Container recipe for Railway (or any Docker host).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN chmod +x start.sh

# Railway sets $PORT itself; 8000 is only the local default.
EXPOSE 8000
CMD ["./start.sh"]
