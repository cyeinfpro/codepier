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
LABEL org.codepier.collaboration-contract="1"
LABEL org.codepier.hub-state-epoch="1"
COPY --from=dependencies /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN groupadd --gid 10001 codepier \
    && useradd --uid 10001 --gid codepier --create-home --shell /usr/sbin/nologin codepier \
    && mkdir -p /app/data /var/lib/codepier-probe \
    && chown codepier:codepier /app/data /var/lib/codepier-probe \
    && chmod 0700 /var/lib/codepier-probe
COPY --chown=root:root hub ./hub
COPY --chown=root:root shared ./shared
COPY --chown=root:root web ./web
COPY --chown=root:root agent ./agent
COPY --chown=root:root scripts/install_agent.py scripts/agent_lifecycle.py scripts/mcp_stdio_bridge.py ./scripts/
COPY --chown=root:root deploy/install-from-hub.sh deploy/install-from-hub.ps1 deploy/install-agent.sh deploy/install-agent.ps1 deploy/start-agent.sh deploy/start-agent.cmd deploy/agent.service deploy/mcp-stdio.sh ./deploy/
COPY --chown=root:root requirements-agent.txt ./
COPY --chown=root:root LICENSE ./
# Host updater extraction uses umask 077. COPY preserves those private modes.
# Normalize only immutable source; never change runtime data or secret permissions.
RUN chmod -R a=rX,u+w hub shared web agent scripts deploy requirements-agent.txt LICENSE
USER 10001:10001
# Check the full application import graph as the final runtime user at build time.
RUN python -c "from hub.app import create_app"
VOLUME /app/data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=4s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=3)"
ENTRYPOINT ["python", "-m", "shared.hub_data_contract", "--exec"]
CMD ["python", "-m", "hub", "run", "--host", "0.0.0.0", "--port", "8765"]
