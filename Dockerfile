FROM python:3.12-slim AS builder

WORKDIR /app

COPY pyproject.toml .
COPY bcn/ bcn/
COPY postgres/ postgres/
RUN pip install --no-cache-dir --pre .

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/bcn /usr/local/bin/bcn
COPY --from=builder /app/postgres/ postgres/

# Install Playwright Chromium + all system dependencies
RUN playwright install --with-deps chromium

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/')" || exit 1

ENTRYPOINT ["bcn"]
CMD ["run"]
