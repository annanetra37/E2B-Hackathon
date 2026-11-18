# IWR405 router RPC capture (task T1, step 1) — TO BE FILLED IN

The bridge (`tools/di_bridge.py`) reads the digital input through the router's own web API, the
same call its "IWR405 · DI" page makes. The exact format is not documented, so capture it once:

1. On a computer in the router's LAN open `https://10.18.249.233` in Chrome, log in.
2. DevTools (F12) → **Network** → tick *Preserve log* → filter `rpc`.
3. Log out and in again, then open the DI page.
4. For the **login** request and for the **do_di_status_get** request, copy: the full URL, the
   request headers (cookie / token / authorization), the request body and the response body.
5. Paste them below with the **password replaced by `<password>`** and tokens shortened.

Then set the bridge's `ROUTER_LOGIN_BODY`, `ROUTER_DI_BODY`, `ROUTER_HEADERS` and, if needed,
`ROUTER_DI_KEY` to match (see `tools/di-bridge.env.example`), and check with:

    python tools/di_bridge.py --probe

which prints the raw reply and the `di1` it understood. Flip the switch and probe again.

## Login request
```
(paste here)
```

## do_di_status_get request + reply (switch ON)
```
(paste here)
```

## do_di_status_get reply (switch OFF)
```
(paste here)
```

Answers to the open questions (fill in): login session needed? how long? LOW/HIGH or 0/1?
