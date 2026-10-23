#!/bin/sh
# Offline inspection. Usage: sh scripts/check-emudhra-ca.sh [image ...]
# PROJECT_NAME selects running Compose containers (default: mail-sentinel).
set -u

probe=$(cat <<'PY'
import importlib.util
import ssl
import subprocess
import sys

def report_error(kind, value, traceback):
    print("UNKNOWN: inspection failed:", value, file=sys.stderr)
    sys.exit(2)

sys.excepthook = report_error
bad = {"emSign ECC Root CA - G3", "AAA Certificate Services"}
print("Python:", sys.version.split()[0])
try:
    print("CA package:", subprocess.check_output(
        ["dpkg-query", "-W", "ca-certificates"], text=True).strip())
except (OSError, subprocess.CalledProcessError):
    print("CA package: unknown")

def check(label, context):
    names = {value for cert in context.get_ca_certs()
             for rdn in cert.get("subject", ())
             for key, value in rdn if key == "commonName"}
    if not names:
        raise RuntimeError(label + ": no CA certificates loaded")
    found = sorted(names & bad)
    print(label + ": " + ("FAIL: " + ", ".join(found) if found else "PASS: affected roots absent"))
    return bool(found)

failed = check("Default SSL trust", ssl.create_default_context())
# The application's IMAP_CA_FILE supplements, rather than replaces, system CAs.
import os
extra = os.environ.get("IMAP_CA_FILE")
if extra:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=extra)
    failed |= check("IMAP_CA_FILE combined trust", ctx)
for name in ("certifi", "pip._vendor.certifi"):
    try:
        import importlib
        module = importlib.import_module(name)
    except ModuleNotFoundError:
        print(name + ": not installed")
        continue
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=module.where())
    failed |= check(name, ctx)
sys.exit(1 if failed else 0)
PY
)

status=0
check_result() {
    case "$1" in
        0) ;;
        1) [ "$status" -eq 0 ] && status=1 ;;
        *) status=2 ;;
    esac
    return 0
}

docker info >/dev/null 2>&1 || { echo 'ERROR: Docker is unavailable or permission denied.' >&2; exit 2; }
project=${PROJECT_NAME:-mail-sentinel}
echo "Running Compose containers: project=$project"
for service in worker admin maintenance; do
    ids=$(docker ps -q --filter "label=com.docker.compose.project=$project" \
        --filter "label=com.docker.compose.service=$service") || exit 2
    if [ -z "$ids" ]; then
        echo "$service: no running container"
    fi
    for id in $ids; do
        echo "--- running $service ($id) ---"
        docker inspect --format 'Image={{.Image}} Started={{.State.StartedAt}}' "$id" || exit 2
        printf '%s\n' "$probe" | docker exec -i "$id" python3 -B -
        check_result "$?"
    done
done

if [ "$#" -eq 0 ]; then
    set -- "$project-worker:latest" "$project-admin:latest" "$project-maintenance:latest"
fi
for image do
    echo "--- saved image: $image ---"
    if ! docker image inspect --format 'ID={{.Id}} Created={{.Created}}' "$image"; then
        echo 'UNKNOWN: image missing or inaccessible'
        status=2
        continue
    fi
    printf '%s\n' "$probe" | docker run --rm -i --pull never --network none \
        --read-only --cap-drop ALL --security-opt no-new-privileges \
        --entrypoint python3 "$image" -B -
    check_result "$?"
done
echo 'Scope: default trust and installed certifi/pip bundles; no external TLS requests.'
echo 'Account-specific CA files and other deployment hosts require separate inspection.'
echo "Exit: $status (0=PASS, 1=affected roots trusted, 2=incomplete/error)"
exit "$status"
