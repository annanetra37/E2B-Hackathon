"""
HEALTH RULES  (ELI5: the doctor that looks at the numbers and says "fine" or "sick")
===================================================================================
Input : the latest values the gateway collected for one substation.
Output: a "snapshot" - every element of the diagram with
        status  = ok | warning | fault | offline | unmonitored
        message = a plain sentence explaining why
        values  = the numbers to show next to it

All limits come from substations.yaml -> limits.
"""

RANK = {"unmonitored": 0, "ok": 0, "warning": 1, "fault": 2, "offline": 2}
PHASES = ["L1", "L2", "L3"]
DEAD_V = 50          # below this a phase counts as "no voltage"


def worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: RANK[s]) if statuses else "ok"


def val(label, value, unit="", digits=1):
    if isinstance(value, (int, float)):
        value = round(value, digits)
    return {"label": label, "value": value, "unit": unit}


def el(id_, kind, label, status="ok", message="", values=None, **extra):
    return {"id": id_, "kind": kind, "label": label, "status": status, "message": message,
            "values": values or [], **extra}


def pct_check(value, nominal, warn_pct, fault_pct, what, unit):
    dev = (value - nominal) / nominal * 100
    sign = "+" if dev >= 0 else "−"
    text = f"{what} {value:.1f} {unit} ({sign}{abs(dev):.1f}% from nominal)"
    if abs(dev) > fault_pct:
        return "fault", text
    if abs(dev) > warn_pct:
        return "warning", text
    return "ok", ""


def gateway_element(online: dict) -> dict:
    n_ok, n = sum(online.values()), len(online)
    if n_ok == n:
        return el("GW", "gateway", "Data box (gateway)", "ok", "",
                  [val("Devices answering", f"{n_ok} of {n}")])
    if n_ok == 0:
        return el("GW", "gateway", "Data box (gateway)", "offline", "No device is answering",
                  [val("Devices answering", f"0 of {n}")])
    silent = ", ".join(k.replace("_", " ") for k, v in online.items() if not v)
    return el("GW", "gateway", "Data box (gateway)", "warning", f"Not answering: {silent}",
              [val("Devices answering", f"{n_ok} of {n}")])


# ====================== primary 110/10 kV substation ======================
def evaluate_primary(cfg, sub, data, online):
    L = cfg["limits"]
    rtu_ok = online.get("rtu", False)
    rtu = data.get("rtu", {}) if rtu_ok else {}
    out = []

    def breaker(key, label):
        if not rtu_ok:
            return el(key, "breaker", label, "offline", "No communication with the RTU", closed=None)
        closed = bool(rtu.get(key))
        return el(key, "breaker", label, "ok" if closed else "fault", "" if closed else "Breaker open (tripped)",
                  [val("Position", "Closed" if closed else "Open")], closed=closed)

    q0, q1 = breaker("Q0", "Q0 line breaker"), breaker("Q1", "Q1 transformer breaker")
    supply_ok = q0["closed"] is not False and q1["closed"] is not False

    if online.get("line_meter"):
        m = data["line_meter"]
        st, msg = pct_check(m["voltage"], L["line_kv"]["nominal"], L["line_kv"]["warn_pct"],
                            L["line_kv"]["fault_pct"], "Incoming voltage", "kV")
        out.append(el("LINE", "line", "110 kV incoming line", st, msg,
                      [val("Voltage", m["voltage"], "kV"), val("Current", m["current"], "A", 0),
                       val("Power", m["power"], "MW", 2)], kv=110))
    else:
        out.append(el("LINE", "line", "110 kV incoming line", "offline", "Line meter not responding", kv=110))
    out.append(q0)

    if online.get("transformer"):
        t, st, msgs = data["transformer"], "ok", []
        if t["oil_temp"] > L["oil_temp_c"]["fault"]:
            st, msgs = "fault", [f"Oil temperature {t['oil_temp']:.0f} °C (limit {L['oil_temp_c']['fault']} °C)"]
        elif t["oil_temp"] > L["oil_temp_c"]["warn"]:
            st, msgs = "warning", [f"Oil temperature {t['oil_temp']:.0f} °C, getting hot"]
        if t["load"] > L["load_pct"]["fault"]:
            st = "fault"; msgs.append(f"Overloaded at {t['load']:.0f}%")
        elif t["load"] > L["load_pct"]["warn"]:
            st = worst(st, "warning"); msgs.append(f"High load {t['load']:.0f}%")
        out.append(el("T1", "transformer", f"T1 110/10 kV, {sub['transformer_mva']} MVA", st, "; ".join(msgs),
                      [val("Load", t["load"], "%", 0), val("Temperature", t["oil_temp"], "°C", 0),
                       val("Tap", t["tap"], "", 0)]))
    else:
        out.append(el("T1", "transformer", f"T1 110/10 kV, {sub['transformer_mva']} MVA", "offline",
                      "Transformer monitor not responding"))
    out.append(q1)

    if online.get("bus_meter"):
        b = data["bus_meter"]
        if b["voltage"] < 1:
            st, msg = "fault", "No voltage on the busbar" + ("" if supply_ok else " (supply breaker open)")
        else:
            st, msg = pct_check(b["voltage"], L["bus_kv"]["nominal"], L["bus_kv"]["warn_pct"],
                                L["bus_kv"]["fault_pct"], "Busbar voltage", "kV")
        vals = [val("Voltage", b["voltage"], "kV", 2), val("Power", b["power"], "MW", 2)]
        if rtu_ok and rtu.get("frequency") is not None:
            fq, FL = rtu["frequency"], L["frequency_hz"]
            vals.append(val("Frequency", fq, "Hz", 2))
            if b["voltage"] >= 1 and not FL["fault_low"] <= fq <= FL["fault_high"]:
                st, msg = "fault", f"Frequency {fq:.2f} Hz out of range"
            elif b["voltage"] >= 1 and not FL["warn_low"] <= fq <= FL["warn_high"]:
                st, msg = worst(st, "warning"), msg or f"Frequency {fq:.2f} Hz drifting"
        out.append(el("BUS", "busbar", "10 kV busbar", st, msg, vals, kv=10))
    else:
        out.append(el("BUS", "busbar", "10 kV busbar", "offline", "Busbar meter not responding", kv=10))

    FA = L["feeder_a"]
    for i, fname in enumerate(sub["feeders"], start=1):
        closed = rtu.get(f"QF{i}") if rtu_ok else None
        e = el(f"F{i}", "feeder", f"F{i} {fname}", closed=None if closed is None else bool(closed), measured=True)
        if not online.get(f"feeder_{i}"):
            e.update(status="offline", message="Feeder meter not responding")
        else:
            m = data[f"feeder_{i}"]
            e["values"] = [val("Current", m["current"], "A", 0), val("Power", m["power"], "MW", 2),
                           val("Voltage", m["voltage"], "kV", 2)]
            e["energised"] = m["voltage"] >= 1 and closed is not False
            if closed is False:
                e.update(status="fault", message="Feeder breaker tripped, customers without power")
            elif m["voltage"] < 1:
                e.update(status="fault", message="No voltage, feeder de-energised")
            elif m["current"] > FA["fault"]:
                e.update(status="fault", message=f"Overload {m['current']:.0f} A (limit {FA['fault']} A)")
            elif m["current"] > FA["warn"]:
                e.update(status="warning", message=f"High current {m['current']:.0f} A")
            if closed is None:
                e.update(status=worst(e["status"], "offline"),
                         message=e["message"] or "Breaker position unknown (RTU offline)")
        out.append(e)
    return out


# ============ distribution transformer station (the hand-drawn sketch) ============
def evaluate_distribution(cfg, sub, data, online):
    L = cfg["limits"]
    kv, kva = sub["incoming_kv"], sub["transformer_kva"]
    lv_ok = online.get("lv_meter", False)
    lv = data.get("lv_meter", {})
    volts = [lv.get(f"v_l{p}") for p in (1, 2, 3)] if lv_ok else [None] * 3
    alive = [v is not None and v >= DEAD_V for v in volts]
    qs = None
    if online.get("di_module"):
        qs = bool(round(data["di_module"]["disconnector_closed"]))
    out = []

    # --- incoming MV line: not measured directly, judged from the 0.4 kV side ---
    line_label = f"{kv} kV incoming line"
    if not lv_ok:
        out.append(el("LINE", "line", line_label, "offline", "Can't tell: 0.4 kV meter not responding", kv=kv))
    elif qs is False:
        out.append(el("LINE", "line", line_label, "unmonitored",
                      "Can't check while the disconnector is open", [val("Supply", "Unknown")], kv=kv))
    elif not any(alive):
        out.append(el("LINE", "line", line_label, "fault", f"No supply from the {kv} kV line",
                      [val("Supply", "Lost")], kv=kv, energised=False))
    else:
        out.append(el("LINE", "line", line_label, "ok", "", [val("Supply", "Present")], kv=kv, energised=True))

    # --- QS disconnector ---
    if qs is None:
        out.append(el("QS", "disconnector", "QS disconnector", "offline", "Position input not responding", closed=None))
    else:
        out.append(el("QS", "disconnector", "QS disconnector", "ok" if qs else "fault",
                      "" if qs else "Disconnector open, station switched off",
                      [val("Position", "Closed" if qs else "Open")], closed=qs))

    # --- FU fuses: a blown fuse = one phase dead while the others are alive ---
    if not lv_ok:
        out.append(el("FU", "fuses", "FU fuses", "offline", "Can't check: 0.4 kV meter not responding"))
    elif not any(alive):
        out.append(el("FU", "fuses", "FU fuses", "unmonitored", "Can't check without supply",
                      [val(p, "Unknown") for p in PHASES], blown=[False] * 3))
    else:
        blown = [not a for a in alive]
        names = [p for p, b in zip(PHASES, blown) if b]
        out.append(el("FU", "fuses", "FU fuses", "fault" if names else "ok",
                      f"Fuse blown on {', '.join(names)}: phase lost" if names else "",
                      [val(p, "Blown" if b else "OK") for p, b in zip(PHASES, blown)], blown=blown))

    # --- transformer: temperature sensor + load from the 0.4 kV meter ---
    t_label = f"T {kv}/0.4 kV, {kva} kVA"
    vals, st, msgs = [], "ok", []
    if online.get("temp_sensor"):
        temp = data["temp_sensor"]["temperature"]
        vals.append(val("Temperature", temp, "°C", 0))
        if temp > L["oil_temp_c"]["fault"]:
            st = "fault"; msgs.append(f"Temperature {temp:.0f} °C (limit {L['oil_temp_c']['fault']} °C)")
        elif temp > L["oil_temp_c"]["warn"]:
            st = "warning"; msgs.append(f"Temperature {temp:.0f} °C, getting hot")
    else:
        st = "offline"; msgs.append("Temperature sensor not responding")
    if lv_ok:
        load = sum((lv.get(f"v_l{p}") or 0) * (lv.get(f"i_l{p}") or 0) for p in (1, 2, 3)) / 1000 / kva * 100
        vals.append(val("Load", load, "%", 0))
        if load > L["load_pct"]["fault"]:
            st = worst(st, "fault"); msgs.append(f"Overloaded at {load:.0f}%")
        elif load > L["load_pct"]["warn"]:
            st = worst(st, "warning"); msgs.append(f"High load {load:.0f}%")
    vals.append(val("Rating", kva, "kVA", 0))
    out.append(el("T", "transformer", t_label, st, "; ".join(msgs), vals))

    # --- 0.4 kV busbar with L1 / L2 / L3 (the 230 / 241 box in the sketch) ---
    LV = L["lv_v"]
    if not lv_ok:
        out.append(el("BUS", "busbar", "0.4 kV busbar", "offline", "0.4 kV meter not responding", kv=0.4))
    else:
        st, msgs = "ok", []
        for p, v in zip(PHASES, volts):
            if v < DEAD_V:
                st = "fault"; msgs.append(f"{p} no voltage")
            else:
                s, m = pct_check(v, LV["nominal"], LV["warn_pct"], LV["fault_pct"], p, "V")
                st = worst(st, s)
                if m:
                    msgs.append(m)
        if not any(alive):
            msgs = ["No voltage on the busbar"]
        vals = [val(p, v, "V", 0) for p, v in zip(PHASES, volts)] + [val("Power", lv.get("power"), "kW", 0)]
        out.append(el("BUS", "busbar", "0.4 kV busbar", st, "; ".join(msgs), vals, kv=0.4, phases_alive=alive))

    # --- outgoing 0.4 kV lines (circles in the sketch = current transformers) ---
    FP = L["lv_feeder_pct"]
    for i, ln in enumerate(sub["lines"], start=1):
        e = el(f"W{i}", "feeder", f"W{i} {ln['name']}", measured=bool(ln.get("measured")),
               energised=any(alive) if lv_ok else None)
        dead_phases = [p for p, a in zip(PHASES, alive) if not a] if lv_ok else []
        if ln.get("measured"):
            if not online.get(f"ct_{i}"):
                e.update(status="offline", message="Current meter not responding")
            else:
                amps = [data[f"ct_{i}"][f"i_l{p}"] for p in (1, 2, 3)]
                rating = ln.get("rating_a", 250)
                peak = max(amps)
                e["values"] = [val(f"{p} current", a, "A", 0) for p, a in zip(PHASES, amps)] + \
                              [val("Rating", rating, "A", 0)]
                if lv_ok and not any(alive):
                    e.update(status="fault", message="No voltage, customers without power")
                elif dead_phases:
                    e.update(status="fault", message=f"Phase {', '.join(dead_phases)} missing")
                elif peak > rating * FP["fault"] / 100:
                    e.update(status="fault", message=f"Overload {peak:.0f} A (rating {rating} A)")
                elif peak > rating * FP["warn"] / 100:
                    e.update(status="warning", message=f"High current {peak:.0f} A")
        else:
            if lv_ok and not any(alive):
                e.update(status="fault", message="No voltage, customers without power")
            elif dead_phases:
                e.update(status="fault", message=f"Phase {', '.join(dead_phases)} missing")
            else:
                e.update(status="unmonitored", message="No meter on this line")
        out.append(e)
    return out


# ================================ entry point ================================
def evaluate(cfg: dict, sub: dict, data: dict, online: dict) -> dict:
    """data[device][point] -> value ; online[device] -> bool"""
    fn = evaluate_primary if sub["type"] == "primary" else evaluate_distribution
    elements = fn(cfg, sub, data, online)
    elements.append(gateway_element(online))

    overall = worst(*(e["status"] for e in elements))
    problems = [e for e in elements if RANK[e["status"]] > 0]
    if not any(online.values()):
        overall, headline = "offline", "No communication with the substation"
    elif problems:
        top = max(problems, key=lambda e: RANK[e["status"]])   # first = most upstream = root cause
        headline = f"{top['label'].split(',')[0]}: {top['message']}" if top["message"] else top["label"]
    else:
        headline = "All equipment normal"

    if sub["type"] == "primary":
        rating = f"110/10 kV, {sub['transformer_mva']} MVA"
    else:
        rating = f"{sub['incoming_kv']}/0.4 kV, {sub['transformer_kva']} kVA"
    return {"id": sub["id"], "name": sub["name"], "type": sub["type"], "region": sub.get("region", ""),
            "rating": rating, "lat": sub["lat"], "lon": sub["lon"], "status": overall,
            "headline": headline, "problem_count": len(problems), "elements": elements}
