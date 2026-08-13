FROM debian:bookworm-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        ca-certificates \
        imapfilter \
        spamc \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 mailsentinel \
    && useradd --system --uid 10001 --gid mailsentinel --home-dir /nonexistent --shell /usr/sbin/nologin mailsentinel

COPY --chown=root:root imapfilter.lua /etc/mail-sentinel/imapfilter.lua

ENV IMAPFILTER_HOME=/tmp/imapfilter

USER 10001:10001

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["imapfilter", "-c", "/etc/mail-sentinel/imapfilter.lua"]
