FROM debian:trixie-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        ca-certificates \
        python3 \
        spamc \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 mailsentinel \
    && useradd --system --uid 10001 --gid mailsentinel --home-dir /nonexistent --shell /usr/sbin/nologin mailsentinel \
    && install -d -o mailsentinel -g mailsentinel -m 0700 /var/lib/mail-sentinel-state

COPY --chown=root:root python/mail_sentinel_imap.py /usr/lib/python3/dist-packages/mail_sentinel_imap.py
COPY --chown=root:root python/mail_sentinel_accounts.py /usr/lib/python3/dist-packages/mail_sentinel_accounts.py
COPY --chown=root:root worker/worker.py /usr/local/bin/mail-sentinel-worker
COPY --chown=root:root worker/supervisor.py /usr/local/bin/mail-sentinel-supervisor

RUN chmod 0755 /usr/local/bin/mail-sentinel-worker /usr/local/bin/mail-sentinel-supervisor

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

USER 10001:10001

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/mail-sentinel-supervisor"]
