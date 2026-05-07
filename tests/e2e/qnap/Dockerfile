FROM debian:12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server rsync ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && if id backup >/dev/null 2>&1; then usermod -d /home/backup -s /bin/sh backup; mkdir -p /home/backup; chown backup:backup /home/backup; else useradd -m -d /home/backup -s /bin/sh backup; fi \
    && mkdir -p /run/sshd /backup /home/backup/.ssh \
    && chown -R backup:backup /backup /home/backup/.ssh \
    && chmod 700 /home/backup/.ssh

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 22
CMD ["/entrypoint.sh"]
