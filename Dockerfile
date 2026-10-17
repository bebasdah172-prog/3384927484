FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/home/app

WORKDIR /app

# Install the pinned Playwright version, its Linux browser dependencies, and
# regular Chromium during the image build. --no-shell avoids the headless-shell
# binary that crashes on the reported host.
COPY duckapi/requirements.txt /tmp/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates passwd \
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && python -m playwright install --with-deps --no-shell chromium \
    && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}" \
    && rm -f /tmp/requirements.txt \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app
COPY --chown=app:app duckapi/ /app/duckapi/

WORKDIR /app/duckapi
USER app

EXPOSE 8080
CMD ["python", "main.py"]
