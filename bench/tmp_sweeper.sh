#!/bin/sh
# During build phases: every 5 minutes remove /tmp/toydb* dirs that hold only the leaked `bitcask` file of upstream's
# bitcask tests and are older than 10 minutes (so a running test's dir is never touched).
while true; do
  find /tmp -maxdepth 1 -type d -name 'toydb*' -mmin +10 -exec sh -c '[ "$(ls -A "$1")" = bitcask ] && rm -rf "$1"' _ {} \; 2>/dev/null
  sleep 300
done
