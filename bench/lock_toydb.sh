#!/usr/bin/env bash
# lock|unlock everything outside the run workspaces that could leak task answers
SP=/tmp/claude-1001/-home-user/0e981355-0d3b-4c00-86f4-706c60e10216/scratchpad
case "$1" in
  lock)   chmod 000 /home/user/agentbench/toydb /home/user/refcode "$SP" ;;
  unlock) chmod 755 /home/user/agentbench/toydb /home/user/refcode "$SP" ;;
esac
ls -ld /home/user/agentbench/toydb /home/user/refcode "$SP"
