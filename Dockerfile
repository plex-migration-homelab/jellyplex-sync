FROM python:3.13-slim AS builder

WORKDIR /app
COPY . /app/

RUN pip install --no-cache-dir poetry
RUN poetry build --format wheel --output dist

FROM python:3.13-slim AS runtime

# Install attr package for getfattr command (MergerFS debugging)
RUN apt-get update && apt-get install -y --no-install-recommends attr && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/dist/*.whl /app/
RUN pip install --no-cache-dir /app/*.whl

ENTRYPOINT ["jellyplex-sync"]
