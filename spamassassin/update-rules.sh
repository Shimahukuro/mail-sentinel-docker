#!/bin/sh
set -eu

install -d -m 0700 /var/lib/spamassassin/sa-update-keys

update_status=0
sa-update \
    --updatedir /var/lib/spamassassin \
    --gpghomedir /var/lib/spamassassin/sa-update-keys \
    || update_status=$?

case "$update_status" in
    0)
        echo 'SpamAssassin rules updated.'
        ;;
    1)
        echo 'SpamAssassin rules are already current.'
        ;;
    *)
        echo "SpamAssassin rule update failed with status $update_status." >&2
        exit "$update_status"
        ;;
esac

spamassassin --lint
date -u +%Y-%m-%dT%H:%M:%SZ > /var/lib/spamassassin/mail-sentinel-rules-updated-at
echo 'SpamAssassin rule validation passed.'
printf '{"event":"rule_update","result":"success","timestamp":"%s"}\n' \
    "$(cat /var/lib/spamassassin/mail-sentinel-rules-updated-at)"
