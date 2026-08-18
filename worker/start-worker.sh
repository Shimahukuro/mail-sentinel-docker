#!/bin/sh
set -eu

imapfilter -c /etc/mail-sentinel/diagnose.lua
exec imapfilter -c /etc/mail-sentinel/imapfilter.lua
