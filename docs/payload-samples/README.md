# Payload samples (task HW-5)

The IWR405 manual does not document the MQTT topic or JSON layout, so the parser is
built from real captures. Commit them here; `tests/test_parser.py` parses **every**
`*.json` file in this folder and fails if a message yields no power value.

`illustrative.json` is the made-up shape from §5.1 of the developer-tasks doc.
Delete it once real samples exist.

## How to capture

1. In MQTTX (or `mosquitto_sub`), subscribe with the app credential to `#` and note the
   exact topic the gateway publishes on (this becomes `MQTT_TOPIC`).
2. Record at least 10 messages for each case: lamp only, hair dryer low, hair dryer high,
   and the burst that arrives after a Wi-Fi drop (Data Cache back-fill).
3. Save each case as a file, e.g. `lamp.json`, in this format:

```json
{
  "topic": "<exact topic>",
  "messages": [
    {"payload": { ...one message exactly as received... }},
    {"payload": { ... }}
  ]
}
```

4. Run `pytest tests/test_parser.py`. If a sample fails, set `FIELD_*` to the names the
   gateway uses (`PAYLOAD_FORMAT=fourfaith` matches them case-insensitively, also with a
   device prefix such as `demo_meter_power`), or switch to `PAYLOAD_FORMAT=generic_json`
   with dotted paths (`FIELD_POWER=values.power`).
5. Check that the gateway timestamp is parsed (`ts` in the reading is not just the receive
   time) and that back-filled messages come out with `late: true`.
