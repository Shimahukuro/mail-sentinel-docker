# Third-Party Notices

Mail Sentinel Docker uses and distributes third-party software. This document
summarizes the directly referenced components identified from the Dockerfiles
and Docker Compose configuration.

The Mail Sentinel Docker project itself is licensed under the Apache License,
Version 2.0. That license does not replace or alter the licenses of the
third-party components listed below.

## Runtime components

### Debian GNU/Linux (bookworm-slim)

- Source: https://www.debian.org/
- Container image: https://hub.docker.com/_/debian
- License: Multiple free and open-source licenses, on a per-package basis

The Debian base image and installed Debian packages contain software under
multiple licenses. The authoritative copyright and license information for an
installed package is available inside the built image under
`/usr/share/doc/<package>/copyright`.

### IMAPFilter

- Version supplied by Debian Bookworm at the time of review: 2.8.1
- Source: https://github.com/lefcha/imapfilter
- License: MIT/X11

Copyright and license notices are provided by the upstream project and by the
Debian package in `/usr/share/doc/imapfilter/copyright`.

### Apache SpamAssassin, spamd, and spamc

- Version supplied by Debian Bookworm at the time of review: 4.0.1 series
- Source: https://spamassassin.apache.org/
- License: Apache License 2.0

SpamAssassin, spamd, and spamc are produced from the SpamAssassin source
package. Their authoritative license and attribution information is included
with the Debian packages under `/usr/share/doc/`.

### Tini

- Version supplied by Debian Bookworm at the time of review: 0.19.0
- Source: https://github.com/krallin/tini
- License: MIT

Copyright and license notices are provided by the upstream project and by the
Debian package in `/usr/share/doc/tini/copyright`.

### CA certificates

- Package: ca-certificates
- Source: https://packages.debian.org/bookworm/ca-certificates
- License: Multiple licenses covering the package and included certificates

The authoritative notices are included in the Debian package at
`/usr/share/doc/ca-certificates/copyright`.

## Test-only component

### GreenMail

- Version: 2.1.11
- Source: https://greenmail-mail-test.github.io/greenmail/
- Container image: https://hub.docker.com/r/greenmail/standalone
- License: Apache License 2.0

GreenMail is referenced only by the integration-test Docker Compose
configuration. Its container image also contains transitive dependencies and a
base operating-system image governed by their respective licenses.

## Scope and maintenance

This file is a convenience summary, not a substitute for the complete license
texts and notices shipped with each component. Container images include
transitive operating-system and language dependencies that are not individually
enumerated here.

The Debian base tag and APT package versions are not currently pinned to
immutable digests or exact versions. Therefore, the actual component versions
and license inventory can change when images are rebuilt. Before distributing a
built image, generate or inspect its software bill of materials and preserve
the license and attribution files contained in that exact image.
