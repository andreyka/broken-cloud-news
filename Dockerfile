FROM python:3.12-slim

WORKDIR /app

# Copy project files and install
COPY pyproject.toml .
COPY bcn/ bcn/
COPY postgres/ postgres/
RUN pip install --no-cache-dir --pre .

# Install Playwright Chromium + all system dependencies
RUN playwright install --with-deps chromium

ENTRYPOINT ["bcn"]
CMD ["run"]
