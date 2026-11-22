FROM python:3.12-slim-bookworm AS fixture
ENV PYTHONPATH=/app/server PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
WORKDIR /app
COPY server/requirements.txt server/requirements-history.txt server/
RUN pip install --no-cache-dir -r server/requirements-history.txt
COPY contracts/reference contracts/reference
COPY contracts/schema contracts/schema
COPY contracts/samples/07_microphone_audio.json contracts/samples/
COPY server/encore_server server/encore_server
COPY deploy/start-server.sh deploy/smoke.py deploy/
EXPOSE 8765
STOPSIGNAL SIGINT
USER 10001:10001
ENTRYPOINT ["/bin/sh", "/app/deploy/start-server.sh"]

FROM fixture AS inference
USER root
COPY server/requirements-yamnet.txt server/
COPY deploy/models/requirements-face.txt deploy/models/
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 libportaudio2 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r server/requirements-yamnet.txt "tensorflow[and-cuda]==2.18.1" \
    && python -m venv /opt/face \
    && /opt/face/bin/pip install --no-cache-dir -r deploy/models/requirements-face.txt \
    && pip check && /opt/face/bin/pip check
USER 10001:10001
