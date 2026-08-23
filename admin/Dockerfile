FROM debian:trixie-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        ca-certificates \
        python3 \
        spamc \
        tzdata \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 mailsentinel \
    && useradd --system --uid 10001 --gid mailsentinel --home-dir /nonexistent --shell /usr/sbin/nologin mailsentinel \
    && install -d -o mailsentinel -g mailsentinel -m 0700 /var/lib/mail-sentinel-state \
    && install -d -o mailsentinel -g mailsentinel -m 0700 \
        /data/spamassassin-data /data/spamassassin-rules /data/mail-sentinel-state /backups

COPY --chown=root:root admin/admin.py /usr/local/bin/mail-sentinel-admin
COPY --chown=root:root admin/maintenance.py /usr/local/bin/mail-sentinel-maintenance
COPY --chown=root:root python/mail_sentinel_imap.py /usr/lib/python3/dist-packages/mail_sentinel_imap.py
COPY --chown=root:root python/mail_sentinel_accounts.py /usr/lib/python3/dist-packages/mail_sentinel_accounts.py
COPY --chown=root:root python/mail_sentinel_operations.py /usr/lib/python3/dist-packages/mail_sentinel_operations.py
COPY --chown=root:root spamassassin/local.cf /etc/mail-sentinel/local.cf
RUN chmod 0755 /usr/local/bin/mail-sentinel-admin /usr/local/bin/mail-sentinel-maintenance

ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 STATE_DIR=/var/lib/mail-sentinel-state
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/mail-sentinel-admin"]
