FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py gdrive_client.py ./

RUN mkdir -p /app/output

EXPOSE 8888

# Default: SSE transport (HTTP). Use "python3 server.py --stdio" for stdio.
CMD ["python3", "server.py"]
