#!/usr/bin/env bash
# usage: mk_batch.sh <prefix> <model> <effort> <tag>
cd /home/user/agentbench/bench
for t in T01 T02 T03 T04 T05 T06 T07 T08 T09 T10 T11 T12; do for l in python rust; do
  echo "$t $l ${1}_${t,,}_${l:0:2}_$4 $2 $3"
done; done | xargs -P 4 -L 1 python3 make_run.py > /dev/null
