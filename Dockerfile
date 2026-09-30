# The multi-architecture manifest is verified when updating this pin.
FROM python:3.13.15-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS dependencies
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt

FROM python:3.13.15-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HUB_DATA_DIR=/app/data HUB_PORT=8765 TZ=Asia/Taipei
WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN mkdir -p /app/data
COPY hub ./hub
COPY shared ./shared
COPY web ./web
COPY agent ./agent
COPY scripts ./scripts
COPY deploy ./deploy
COPY requirements-agent.txt ./
COPY LICENSE ./
USER 0:0
VOLUME /app/data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=4s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=3)"
CMD ["python", "-m", "hub", "run", "--host", "0.0.0.0", "--port", "8765"]
