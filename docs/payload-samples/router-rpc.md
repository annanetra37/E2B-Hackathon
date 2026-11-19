# IWR405 router RPC capture (task T1, step 1) — captured 2026-11-19

The bridge (`tools/di_bridge.py`) reads the digital input through the router's own web API: the
same call the router's **Maintenance → IO set** page makes to show "DI status". Captured with
Chrome DevTools → Network on the IO set page (firmware on the stand's IWR405, web server
`Libuhttpd/3.14.1`). Password and session ID are not reproduced here.

Every call is `POST https://<router>/v1/rpc` with `Content-Type: application/json`.

## Login request
```
POST /v1/rpc
{"jsonrpc":"2.0","id":1,"method":"login","params":{"username":"admin","password":"<password>"}}

200 OK
{"jsonrpc": "2.0", "id": 1, "result": {"sid": "<32 hex chars>", "username": "admin", "role": "admin", "need_guide": false}}
```

The `sid` is the session. The web page then sends it on every call as **both**:
```
Authorization: <sid>                 (the bare value, no "Bearer")
Cookie: Admin-Token=<sid>
```

## do_di_status_get request + reply (switch ON, page shows "DI status: low level")
```
POST /v1/rpc
Authorization: <sid>
{"jsonrpc":"2.0","id":1,"method":"do_di_status_get"}

200 OK
{"jsonrpc": "2.0", "id": 1, "result": {"di_status": 1}}
```

## do_di_status_get reply (switch OFF, page shows "DI status: high level")
```
{"jsonrpc": "2.0", "id": 1, "result": {"di_status": 0}}
```

## What this means for the bridge

- **0/1, not LOW/HIGH.** `di_status: 1` is shown by the page as "low level", so the bridge reads
  1 as LOW and 0 as HIGH (`ROUTER_DI_ONE_IS=LOW`, the default).
- **Wiring on the stand** (Arsen, 2026-11-19): the light switch closes DI+ to GND. Closed → 1 (low
  level) = light on; open → 0 (high level) = light off. With `DI_LOW_MEANS_ON=true` the bridge
  publishes `di1 = 1` for light on and `di1 = 0` for light off, which is what the app expects.
- **Login needed:** yes. The bridge logs in once, sends the `sid` as above, and logs in again when
  the router answers with an error or 401 (e.g. after a router reboot).
- **Router settings that must stay on:** Maintenance → IO set → *IO set* ON and *DI function
  control* ON. Delete or leave disabled any IO action with *Trigger Mode: high level → reboot*
  (light off would reboot the router).
- The router's "HTTP" IO switch (port 6006) is **not** used; that port was not reachable over
  ZeroTier and the web API above is enough.
- **Why a bridge at all:** on this firmware the built-in DI cannot be added to Data Acquisition →
  Cloud → Variable List (that list only imports variables of Child Devices, and Child Device offers
  no built-in-IO type, only FFBOX). A Modbus RS485 DI module as a Child Device would let the router
  publish natively, without the bridge.

Check with:

    python tools/di_bridge.py --probe

which prints the raw reply and the `di1` it understood. Flip the switch and probe again.
