FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client rsync \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . /src

CMD ["python3", "-m", "tests.e2e.mac_safe_case"]
