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
echo 'SpamAssassin rule validation passed.'
