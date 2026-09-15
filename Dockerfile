FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser
COPY . .
RUN mkdir -p /app/data/rag /app/data/logs \
    && chown -R appuser:appuser /app
ENV PYTHONUNBUFFERED=1
USER appuser
CMD ["python", "main.py"]
