#!/usr/bin/env python3
"""
Build (or merge into) a Genesys Cloud Scripter .script file that shows an
"idle chat" alert to the agent.

The script itself contains NO business logic about *when* a chat is idle.
That decision is made server-side by the Architect workflow (see
workflow/Chat-Idle-Monitor-workflow.md), which writes three participant
attributes on the customer participant of the conversation:

    IdleAlert    "1" while the alert is active, "0" when cleared
    IdleMinutes  minutes since the agent's last activity (string)
    IdleSeq      a counter that increases on every change

The script polls those attributes with the built-in
"Scripter.Refresh Input Variables" action every --tick seconds. Whenever
IdleSeq changes it shows a Scripter.Alert pop-up and a red banner (IdleAlert
== "1") or hides the banner again (IdleAlert == "0").

Usage
-----
    python3 tools/build_script.py                       # writes script/Chat-Idle-Alert.script
    python3 tools/build_script.py --tick 20 --hours 6   # faster polling, longer coverage
    python3 tools/build_script.py --merge "My Existing Script.script" \
        --out "My Existing Script + idle alert.script"   # inject into an exported script

Only the Python standard library is used. No Genesys access is needed.
"""
import argparse
import copy
import json
import sys
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Names of the participant attributes / script variables. Keep in sync with the
# workflow and the data actions.
# ---------------------------------------------------------------------------
ATTR_ALERT = "IdleAlert"
ATTR_MINUTES = "IdleMinutes"
ATTR_SEQ = "IdleSeq"
VAR_SEEN = "IdleSeqSeen"
VAR_BANNER = "IdleBannerVisible"

CA_TICK = "Idle Monitor - Tick"
CA_BLOCK = "Idle Monitor - 10 min block"
CA_START = "Idle Monitor - Start"
CA_WRAPPER = "Idle Monitor - On Load Wrapper"

FONT = 'Arial, "Helvetica Neue", Helvetica, sans-serif'


def new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Low level JSON builders (shapes taken from real Scripter exports and the
# Scripter editor's own serializer).
# ---------------------------------------------------------------------------
def variable(name, vtype, value, description="", is_input=False, is_output=False, vid=None):
    return {
        "id": vid or new_id(),
        "name": name,
        "description": description,
        "type": {"isDynamic": False, "isList": False, "name": vtype},
        "value": value,
        "input": is_input,
        "output": is_output,
    }


def step_action(action_name, args=None):
    return {"type": "action", "actionName": action_name, "actionArgumentValues": args or []}


def step_delay(ms: int):
    return {"type": "delay", "delay": {"typeName": "integer", "value": int(ms)}}


def step_alert(text: str):
    return step_action("scripter.alert", [{"typeName": "interpolatedText", "value": text}])


def step_refresh_inputs():
    return step_action("scripter.refreshInputVariables")


def step_set_bool(var_id: str, value: bool):
    return step_action(
        "scripter.setVariable",
        [{
            "typeName": "variableWithValue",
            "value": {
                "variable": {"typeName": "variable", "value": var_id},
                "variableTypeName": "boolean",
                "valueOfVariableProperty": {"typeName": "boolean", "value": bool(value)},
            },
        }],
    )


def step_copy_string(dst_var_id: str, src_var_id: str):
    return step_action(
        "scripter.setVariable",
        [{
            "typeName": "variableWithValue",
            "value": {
                "variable": {"typeName": "variable", "value": dst_var_id},
                "variableTypeName": "string",
                "valueOfVariableProperty": {"typeName": "variable", "value": src_var_id},
            },
        }],
    )


def step_if_var_vs_text(var_id, operator, text, if_block, else_block=None):
    return {
        "type": "if",
        "condition": {
            "type": "standard",
            "leftHandSide": {"typeName": "variable", "value": var_id},
            "operator": operator,
            "rightHandSide": {"typeName": "interpolatedText", "value": text},
        },
        "ifBlock": if_block,
        "elseBlock": else_block or [],
    }


def step_if_var_vs_var(var_id, operator, other_var_id, if_block, else_block=None):
    return {
        "type": "if",
        "condition": {
            "type": "standard",
            "leftHandSide": {"typeName": "variable", "value": var_id},
            "operator": operator,
            "rightHandSide": {"typeName": "variable", "value": other_var_id},
        },
        "ifBlock": if_block,
        "elseBlock": else_block or [],
    }


def step_try(try_block, catch_block=None):
    return {"type": "tryCatch", "tryBlock": try_block, "catchBlock": catch_block or []}


def custom_action(name, block, cid=None):
    return {"id": cid or new_id(), "name": name, "action": {"block": block}}


def sizing(size_type="stretch", size=100):
    v = {"sizeType": size_type}
    if size_type != "auto":
        v["size"] = size
    return {"typeName": "sizing", "value": v}


def text_component(text, *, bold=False, size=14, color="#0f172a", margin=0, padding=0, visible_var=None):
    return {
        "type": "text",
        "properties": {
            "margin": {"typeName": "spacing", "value": margin},
            "alignment": {"typeName": "alignment", "value": "start"},
            "width": sizing("stretch", 100),
            "height": sizing("auto"),
            "visible": {"typeName": "variable", **({"value": visible_var} if visible_var else {})},
            "preserveLayoutLocation": {"typeName": "boolean", "value": False},
            "text": {"typeName": "interpolatedText", "value": text},
            "bold": {"typeName": "boolean", "value": bold},
            "italic": {"typeName": "boolean", "value": False},
            "underline": {"typeName": "boolean", "value": False},
            "font": {"typeName": "font", "value": FONT},
            "fontSize": {"typeName": "integer", "value": size},
            "justification": {"typeName": "text", "value": "left"},
            "backgroundColor": {"typeName": "null"},
            "textColor": {"typeName": "color", "value": color},
            "padding": {"typeName": "spacing", "value": padding},
        },
    }


def stack_container(children, *, horizontal=False, background="#ffffff", padding=0, margin=0,
                    visible_var=None, border_color=None, height="auto"):
    return {
        "type": "horizontalStackContainer" if horizontal else "verticalStackContainer",
        "properties": {
            "margin": {"typeName": "spacing", "value": margin},
            "alignment": {"typeName": "alignment", "value": "start"},
            "width": sizing("stretch", 100),
            "height": sizing("stretch", 100) if height == "stretch" else sizing("auto"),
            "visible": {"typeName": "variable", **({"value": visible_var} if visible_var else {})},
            "preserveLayoutLocation": {"typeName": "boolean", "value": False},
            "backgroundColor": {"typeName": "color", "value": background},
            "padding": {"typeName": "spacing", "value": padding},
            "childArrangement": {"typeName": "childArrangement", "value": "start"},
            "border": {"typeName": "border",
                       "value": {"width": 2 if border_color else 0, "style": "solid",
                                 "color": border_color or background}},
        },
        "children": children,
    }


def features(*, chat=True, message=True, screen_pop=False):
    def flag(v):
        return {"properties": {"enabled": {"typeName": "boolean", "value": v}}}
    return {
        "dialer": {"properties": {
            "enabled": {"typeName": "boolean", "value": False},
            "contactListId": {"typeName": "text", "value": ""},
            "onContactDataLoadAction": {"typeName": "action", "value": {}},
        }},
        "scripter": flag(True),
        "screenPop": flag(screen_pop),
        "callback": flag(False),
        "chat": flag(chat),
        "email": flag(False),
        "message": flag(message),
        "bridge": flag(False),
        "uuiData": flag(False),
        "customerSecuredData": flag(False),
        "list": flag(False),
    }


# ---------------------------------------------------------------------------
# The idle-alert building blocks
# ---------------------------------------------------------------------------
def build_idle_blocks(var_ids, tick_seconds: int, hours: float):
    """Return (custom_actions, start_action_id, banner_component)."""
    v_alert, v_min, v_seq, v_seen, v_banner = (
        var_ids[ATTR_ALERT], var_ids[ATTR_MINUTES], var_ids[ATTR_SEQ], var_ids[VAR_SEEN], var_ids[VAR_BANNER])

    alert_text = (
        "Chat waiting: no reply from you for {{%s}} min. "
        "The customer is still waiting - please answer this chat now or wrap it up." % v_min
    )

    # --- Tick: wait, refresh participant data, react if IdleSeq changed -----
    on_change = [
        step_copy_string(v_seen, v_seq),
        step_if_var_vs_text(
            v_alert, "equal", "1",
            if_block=[step_set_bool(v_banner, True), step_alert(alert_text)],
            else_block=[step_set_bool(v_banner, False)],
        ),
    ]
    # Waits one tick, re-reads participant data, and raises the alert when the
    # workflow changed IdleSeq. Edit the Delay to change the poll rate.
    tick = custom_action(CA_TICK, [
        step_delay(tick_seconds * 1000),
        step_try(
            try_block=[step_refresh_inputs(),
                       step_if_var_vs_var(v_seq, "notEqual", v_seen, if_block=on_change)],
            catch_block=[],  # swallow transient errors so the monitor keeps running
        ),
    ])

    # --- 10 minute block = N ticks -------------------------------------------
    ticks_per_block = max(1, round(600 / tick_seconds))
    block = custom_action(CA_BLOCK, [step_action(tick["id"]) for _ in range(ticks_per_block)])

    # --- Start = M blocks (coverage = hours) ----------------------------------
    n_blocks = max(1, round(hours * 6))
    # Runs on page load: n_blocks x 10-minute blocks = coverage per script load.
    start = custom_action(CA_START, [step_action(block["id"]) for _ in range(n_blocks)])

    banner = stack_container(
        [text_component(
            "⚠ NO REPLY SENT FOR {{%s}} MIN - the customer is waiting. "
            "Please answer this chat now or wrap it up." % v_min,
            bold=True, size=14, color="#ffffff")],
        horizontal=True, background="#c62828", padding=12, margin=[0, 0, 12, 0],
        visible_var=v_banner,
    )
    return [tick, block, start], start["id"], banner


def make_variables(existing=None):
    """Create (or reuse) the five script variables. Returns ({name: id}, [new variable dicts])."""
    existing = {v["name"]: v for v in (existing or [])}
    spec = [
        (ATTR_ALERT, "string", "", True, "Set by the Chat Idle Monitor workflow: 1 = alert active, 0 = cleared"),
        (ATTR_MINUTES, "string", "", True, "Set by the Chat Idle Monitor workflow: minutes since agent's last activity"),
        (ATTR_SEQ, "string", "", True, "Set by the Chat Idle Monitor workflow: increments on every change"),
        (VAR_SEEN, "string", "", False, "Internal: last IdleSeq the script reacted to"),
        (VAR_BANNER, "boolean", False, False, "Internal: drives visibility of the red idle banner"),
    ]
    ids, new_vars = {}, []
    for name, vtype, value, is_input, desc in spec:
        if name in existing:
            ids[name] = existing[name]["id"]
            if is_input and not existing[name].get("input"):
                print(f"WARNING: variable '{name}' already exists but is not an Input variable; "
                      f"set Input = Yes in the script editor.", file=sys.stderr)
        else:
            var = variable(name, vtype, value, desc, is_input=is_input)
            ids[name] = var["id"]
            new_vars.append(var)
    return ids, new_vars


# ---------------------------------------------------------------------------
# Fresh standalone script
# ---------------------------------------------------------------------------
def build_standalone(name, tick_seconds, hours):
    var_ids, variables = make_variables()
    actions, start_id, banner = build_idle_blocks(var_ids, tick_seconds, hours)

    page_id = new_id()
    root = stack_container([
        banner,
        text_component("Chat idle monitor is active on this interaction.", size=11, color="#6b7280",
                       margin=[0, 0, 8, 0]),
        text_component("Add your usual script content below this line, or merge this file into your "
                       "existing script with tools/build_script.py --merge.", size=12, color="#374151"),
    ], background="#ffffff", padding=16, height="stretch")

    page = {
        "id": page_id,
        "name": "Chat",
        "properties": {
            "onLoadAction": {"typeName": "action", "value": {"actionName": start_id, "actionArgumentValues": []}},
            "backgroundColor": {"typeName": "color", "value": "#ffffff"},
        },
        "dataVersion": 0,
        "rootContainer": root,
    }
    return {
        "name": name,
        "startPageId": page_id,
        "pages": [page],
        "features": features(chat=True, message=True),
        "variables": variables,
        "customActions": actions,
        "dataVersion": 0,
    }


# ---------------------------------------------------------------------------
# Merge into an existing exported script
# ---------------------------------------------------------------------------
def merge_into(existing: dict, tick_seconds, hours):
    script = copy.deepcopy(existing)
    var_ids, new_vars = make_variables(script.get("variables", []))
    script.setdefault("variables", []).extend(new_vars)

    # drop any previous copy of our custom actions (re-run safe)
    ours = {CA_TICK, CA_BLOCK, CA_START, CA_WRAPPER}
    script["customActions"] = [c for c in script.get("customActions", []) if c["name"] not in ours]
    actions, start_id, banner = build_idle_blocks(var_ids, tick_seconds, hours)
    script["customActions"].extend(actions)

    pages = script.get("pages", [])
    start_page = next((p for p in pages if p["id"] == script.get("startPageId")), None)
    if start_page is None:
        raise SystemExit("Could not find the start page (startPageId) in the existing script.")

    # banner goes to the top of the start page
    rc = start_page.setdefault("rootContainer", stack_container([], height="stretch"))
    children = rc.setdefault("children", [])
    children[:] = [c for c in children if not _is_our_banner(c, var_ids[VAR_BANNER])]
    children.insert(0, banner)

    # chain the monitor after whatever already runs on page load
    props = start_page.setdefault("properties", {})
    current = props.get("onLoadAction", {}).get("value") or {}
    if current.get("actionName") and current.get("actionName") != start_id:
        # runs the page's original on-load action, then starts the idle monitor
        wrapper = custom_action(CA_WRAPPER, [
            {"type": "action", "actionName": current["actionName"],
             "actionArgumentValues": current.get("actionArgumentValues", [])},
            step_action(start_id),
        ])
        script["customActions"].append(wrapper)
        start_id = wrapper["id"]
    props["onLoadAction"] = {"typeName": "action",
                             "value": {"actionName": start_id, "actionArgumentValues": []}}

    # the script must be allowed on chat + messaging interactions
    feats = script.setdefault("features", features())
    for key in ("scripter", "chat", "message"):
        feats.setdefault(key, {"properties": {}})["properties"]["enabled"] = {"typeName": "boolean", "value": True}

    # ids that belong to the source org must not be re-imported
    for k in ("id", "organizationId", "versionId", "createdDate", "modifiedDate"):
        script.pop(k, None)
    return script


def _is_our_banner(component, banner_var_id):
    vis = component.get("properties", {}).get("visible", {})
    return vis.get("value") == banner_var_id


# ---------------------------------------------------------------------------
# Validation: every referenced id must exist
# ---------------------------------------------------------------------------
def validate(script: dict):
    var_ids = {v["id"] for v in script["variables"]}
    ca_ids = {c["id"] for c in script["customActions"]}
    page_ids = {p["id"] for p in script["pages"]}
    problems = []

    def walk(o, path):
        if isinstance(o, dict):
            if o.get("typeName") == "variable" and "value" in o and o["value"] is not None:
                if o["value"] not in var_ids and not str(o["value"]).startswith("scripter."):
                    problems.append(f"{path}: unknown variable id {o['value']}")
            an = o.get("actionName")
            if an and "." not in an and an not in ca_ids:
                problems.append(f"{path}: unknown custom action id {an}")
            for k, v in o.items():
                walk(v, f"{path}/{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")

    walk(script, "$")
    if script["startPageId"] not in page_ids:
        problems.append("startPageId does not match a page")
    names = [v["name"] for v in script["variables"]]
    if len(names) != len(set(names)):
        problems.append("duplicate variable names")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="Chat Idle Alert", help="script name (standalone mode)")
    ap.add_argument("--tick", type=int, default=30, help="seconds between participant-data refreshes (default 30)")
    ap.add_argument("--hours", type=float, default=4, help="how long the monitor keeps running per script load (default 4)")
    ap.add_argument("--merge", metavar="EXPORTED.script", help="inject the alert into an exported script instead")
    ap.add_argument("--out", help="output path (default script/<name>.script)")
    args = ap.parse_args()

    if args.tick < 5:
        ap.error("--tick must be at least 5 seconds")

    if args.merge:
        src = json.loads(Path(args.merge).read_text(encoding="utf-8"))
        script = merge_into(src, args.tick, args.hours)
        out = Path(args.out or (Path(args.merge).stem + " + idle alert.script"))
    else:
        script = build_standalone(args.name, args.tick, args.hours)
        out = Path(args.out or f"script/{args.name}.script".replace(" ", "-"))

    problems = validate(script)
    if problems:
        print("Validation problems:\n  " + "\n  ".join(problems), file=sys.stderr)
        sys.exit(1)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(script, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    n_steps = sum(_count_steps(c["action"]["block"]) for c in script["customActions"])
    print(f"Wrote {out}  ({out.stat().st_size // 1024} KB, {len(script['customActions'])} custom actions, "
          f"{n_steps} steps, tick={args.tick}s, coverage={args.hours}h)")


def _count_steps(block):
    n = 0
    for s in block:
        n += 1
        for key in ("ifBlock", "elseBlock", "tryBlock", "catchBlock", "block"):
            if isinstance(s.get(key), list):
                n += _count_steps(s[key])
    return n


if __name__ == "__main__":
    main()
