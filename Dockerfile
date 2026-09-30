FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
RUN mkdir -p /data
VOLUME /data
ENV DB_PATH=/data/bridge.db
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8686"]
