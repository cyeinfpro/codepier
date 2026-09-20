FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HUB_DATA_DIR=/app/data HUB_PORT=8765 TZ=Asia/Taipei
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 codepier \
    && useradd --uid 10001 --gid codepier --create-home --shell /usr/sbin/nologin codepier \
    && mkdir -p /app/data && chown codepier:codepier /app/data
COPY --chown=codepier:codepier hub ./hub
COPY --chown=codepier:codepier shared ./shared
COPY --chown=codepier:codepier web ./web
COPY --chown=codepier:codepier agent ./agent
COPY --chown=codepier:codepier scripts ./scripts
COPY --chown=codepier:codepier deploy ./deploy
COPY --chown=codepier:codepier requirements-agent.txt ./
COPY --chown=codepier:codepier LICENSE ./
USER 10001:10001
VOLUME /app/data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=4s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=3)"
CMD ["python", "-m", "hub", "run", "--host", "0.0.0.0", "--port", "8765"]
