FROM python:3.12-slim
ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb \
 && mkdir -p /opt/verification \
 && sha256sum /tmp/chrome.deb > /opt/verification/chrome-deb.sha256 \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm -rf /var/lib/apt/lists/* /tmp/chrome.deb
RUN pip install --no-cache-dir 'browser-harness[mcp]==0.1.13' mcp==2.1.1
COPY fixtures/webapp/server.py /opt/fixture/server.py
COPY verification/chrome/container.py /opt/verification/container.py
ENV HOME=/tmp/home BH_RECORD=0 BH_TAB_MARKER=0 BH_UPDATE_CHECK=0 BH_OPEN_LIVE_URL=0 BH_RUNTIME_DIR=/tmp/harness
CMD ["python", "/opt/verification/container.py"]
