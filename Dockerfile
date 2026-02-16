FROM python:3.12-slim

WORKDIR /app

# Install Playwright system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxcomposite1 \
        libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
        libasound2 libxshmfence1 && \
    rm -rf /var/lib/apt/lists/*

# Copy project files and install
COPY pyproject.toml .
COPY bcn/ bcn/
RUN pip install --no-cache-dir --pre .

# Install Playwright Chromium browser
RUN playwright install chromium

ENTRYPOINT ["bcn"]
CMD ["run"]
