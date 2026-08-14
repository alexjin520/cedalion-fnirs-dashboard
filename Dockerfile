FROM python:3.11-slim

ARG CEDALION_VERSION=v26.5.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-cedalion.txt ./
RUN pip install --no-cache-dir -r requirements-cedalion.txt
RUN git clone --depth 1 --branch ${CEDALION_VERSION} \
      https://github.com/ibs-lab/cedalion.git /opt/cedalion \
    && pip install --no-cache-dir --no-deps /opt/cedalion

COPY server.py ./
COPY static ./static
COPY data/samples/fingertapping.snirf /data/fingertapping.snirf

ENV FNIRS_DATA_DIR=/data
ENV FNIRS_DEFAULT_FILE=fingertapping.snirf
EXPOSE 8080

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8080"]

