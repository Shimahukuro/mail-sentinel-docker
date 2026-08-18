FROM debian:trixie-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        ca-certificates \
        imapfilter \
        spamc \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 mailsentinel \
    && useradd --system --uid 10001 --gid mailsentinel --home-dir /nonexistent --shell /usr/sbin/nologin mailsentinel

COPY --chown=root:root common.lua diagnose.lua imapfilter.lua /etc/mail-sentinel/
COPY --chown=root:root start-worker.sh /usr/local/bin/start-worker

RUN chmod 0755 /usr/local/bin/start-worker

ENV IMAPFILTER_HOME=/tmp/imapfilter \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

USER 10001:10001

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["start-worker"]
