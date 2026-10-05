FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./client

# Keep a client endpoint available for docker exec demos and report evidence.
CMD ["python", "-c", "import time; time.sleep(10**9)"]
