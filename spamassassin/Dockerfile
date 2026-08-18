FROM debian:trixie-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        spamassassin \
        spamc \
        spamd \
        gnupg \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 mailsentinel \
    && useradd --system --uid 10001 --gid mailsentinel --home-dir /var/lib/mail-sentinel --shell /usr/sbin/nologin mailsentinel \
    && install -d -o mailsentinel -g mailsentinel -m 0700 /var/lib/mail-sentinel \
    && install -d -o mailsentinel -g mailsentinel -m 0750 /var/lib/spamassassin \
    && chown -R mailsentinel:mailsentinel /var/lib/spamassassin

COPY --chown=root:root local.cf /etc/mail/spamassassin/local.cf
COPY --chown=root:root update-rules.sh /usr/local/bin/update-rules

RUN chmod 0755 /usr/local/bin/update-rules

USER 10001:10001

EXPOSE 783

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["spamd", "--listen=0.0.0.0", "--port=783", "--allowed-ips=0.0.0.0/0", "--username=mailsentinel", "--max-children=2", "--nouser-config", "--pidfile=/tmp/spamd.pid"]
