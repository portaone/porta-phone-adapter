#!/bin/sh
# Runs the adapter unit tests for the Gerrit check (WT-2002), see .jenkins/README.md.
#
# Only the unit tests: they import the adapter from app/ and stub its API objects. The
# other files in tests/ are integration tests against a running adapter, and they all take
# the server URL through the api_url fixture - so a test file that does not mention api_url
# is a unit test and is picked up here automatically.
#
# A file without test functions (test_30_serialize.py is a plain script that checks
# itself at import) makes pytest exit with 5, "no tests collected"; that counts as a
# pass - an exception at import is still a collection error, exit code 2.
#
# Each file runs in its own pytest process: several of them register stub packages in
# sys.modules (bss.adapters.portaswitch and friends) at import time, so in one shared
# session the first file's stubs break the imports of the files collected after it.
set -u
cd /src
rc=0
failed=""
for f in $(grep -L api_url tests/test_*.py | sort); do
    echo "=== $f"
    python -m pytest -v -p no:cacheprovider -p no:warnings "$f"
    case $? in
        0|5) ;;
        *) rc=1; failed="$failed $f" ;;
    esac
done
if [ "$rc" -ne 0 ]; then
    echo "FAILED test files:$failed"
fi
exit "$rc"
