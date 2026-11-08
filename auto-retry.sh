#!/bin/bash
INST=$(curl -s -m 8 -X POST http://127.0.0.1:8333/api -H 'Content-Type: application/json' -d '{"action":"status"}' | grep -c '\"instance\": {')
[ "$INST" -gt 0 ] && exit 0
echo "$(date '+%F %T') 无实例, 尝试create" >> /home/jun/vultr-clash/.state/auto_retry.log
curl -s -m 10 -X POST http://127.0.0.1:8333/api -H 'Content-Type: application/json' -d '{"action":"create","region":"ewr","plan":"vc2-1c-0.5gb"}' >> /home/jun/vultr-clash/.state/auto_retry.log 2>&1
