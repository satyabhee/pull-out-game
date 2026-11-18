# collect/: runs on the server (`~/hl/`)

| file | what |
|---|---|
| `record_hl.py` | `hl-recorder` service: BTC/ETH trades, L2 books and block headers, 24/7 |
| `fetch_blocks.py` | one-off block fetcher + the `Fetcher` class (error/cap checks, never requests blocks older than 120 min) |
| `capture.py` | `hl-capture` service: back-to-back replay windows + order-id anchors |
| `alo_probe.py` | `hl-alo-probe` service: live orderStatus samples of fresh BTC/ETH ALOs → `hl_data/*/*/alo_status.jsonl` |

Deploy: `scp -i <key> collect/*.py ubuntu@<SERVER_IP>:~/hl/ && ssh ... 'sudo systemctl restart hl-capture'`.
Setup on a fresh server: `server/ec2_setup.sh`, then install `server/hl-capture.service`. See `report/SERVER_SETUP_REPORT.md`.
Mac helpers: `server/status.sh`, `server/sync_down.sh [--prune]`. G0 data checks: `checks/`, results in `report/G0_REPORT.md`.
