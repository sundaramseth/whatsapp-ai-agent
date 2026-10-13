FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -m agent && mkdir /data && chown agent /data
COPY app ./app
USER agent
ENV DATABASE_PATH=/data/agent.sqlite3
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
