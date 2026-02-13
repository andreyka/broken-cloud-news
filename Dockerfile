FROM python:3.12-slim

WORKDIR /app

# Copy project files and install
COPY pyproject.toml .
COPY bcn/ bcn/
RUN pip install --no-cache-dir --pre .

ENTRYPOINT ["bcn"]
CMD ["run"]
