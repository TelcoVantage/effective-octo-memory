#!/usr/bin/env python3
"""
Build the Architect workflow "Chat Idle Monitor" as an importable .i3WorkFlow
file (Architect > Workflows > Add > ... > Import), plus a readable decoded JSON
copy and the Trigger definition that starts it.

The file format is the one Architect's own "Export" button produces: the flow
configuration JSON, run through encodeURIComponent() and then base64 (btoa).
Architect regenerates every expression's parse tree from its text when the flow
is loaded and validated, so the expression *text* is what has to be right.

Usage
-----
    python3 tools/build_workflow.py
    python3 tools/build_workflow.py --threshold 5 --repeat 5 --max-polls 480
    python3 tools/build_workflow.py --get-action-id custom_-_<uuid> --set-action-id custom_-_<uuid>

Only the Python standard library is used.
"""
import argparse
import base64
import json
import re
import urllib.parse
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Architect expression type codes
# ---------------------------------------------------------------------------
STR, INT, BLN, DTM, DUR, DEC, STR_COLL = "str", "int", "bln", "dtm", "dur", "dec", "str_coll"
VAR_CLASS = {STR: "StringVariable", INT: "IntegerVariable", BLN: "BoolVariable", DTM: "DateTimeVariable"}
MODE_EXPR, MODE_LIT, MODE_VAR, MODE_NOVALUE = 0, 1, 2, 3

FUNC_TYPES = {
    "Count": INT, "FindFirst": INT, "ToDateTime": DTM, "GetCurrentDateTimeUtc": DTM,
    "DateTimeDiff": DUR, "ToInt": INT, "ToString": STR, "ToDecimal": DEC, "Floor": DEC,
}


def new_id():
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# A small parser for the subset of Architect expression syntax used here.
# Produces the AST shape Architect stores: {<op|func|lit|ref>: {pos, operands?, type}}
# ---------------------------------------------------------------------------
TOKEN_RE = re.compile(r"""
    \s*(?:
        (?P<str>"(?:[^"\\]|\\.)*") |
        (?P<num>\d+(?:\.\d+)?) |
        (?P<ident>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*) |
        (?P<op>==|!=|>=|<=|>|<|!|\+|-|\*|/|\(|\)|\[|\]|,)
    )""", re.VERBOSE)


class ExprParser:
    def __init__(self, text, variables):
        self.text = text
        self.vars = variables          # name -> {"id", "type"}
        self.tokens = []
        pos = 0
        while pos < len(text):
            m = TOKEN_RE.match(text, pos)
            if not m or m.end() == pos:
                if text[pos:].strip() == "":
                    break
                raise SystemExit(f"Cannot tokenise expression at {pos}: {text!r}")
            kind = m.lastgroup
            start = m.start(kind) + 1          # Architect positions are 1-based
            self.tokens.append((kind, m.group(kind), start))
            pos = m.end()
        self.i = 0
        self.refs = {}

    # -- helpers -----------------------------------------------------------
    def peek(self, k=0):
        return self.tokens[self.i + k] if self.i + k < len(self.tokens) else (None, None, None)

    def take(self, value=None):
        tok = self.tokens[self.i]
        if value is not None and tok[1] != value:
            raise SystemExit(f"Expected {value!r} at token {tok} in {self.text!r}")
        self.i += 1
        return tok

    @staticmethod
    def node(key, pos, operands, typ, text=None):
        body = {"pos": pos}
        if text is not None:
            body["text"] = text
        body["operands"] = operands
        body["type"] = typ
        return {key: body}

    @staticmethod
    def typeof(n):
        return next(iter(n.values()))["type"]

    # -- grammar -----------------------------------------------------------
    def parse(self):
        n = self.p_or()
        if self.i != len(self.tokens):
            raise SystemExit(f"Unexpected trailing tokens in {self.text!r}")
        root = next(iter(n.values()))
        root["text"] = self.text                  # Architect keeps the full text on the root only
        return n

    def p_or(self):
        left = self.p_and()
        while self.peek()[1] == "or":
            _, _, pos = self.take()
            right = self.p_and()
            left = self.node("or", pos, [left, right], BLN)
        return left

    def p_and(self):
        left = self.p_not()
        while self.peek()[1] == "and":
            _, _, pos = self.take()
            right = self.p_not()
            left = self.node("and", pos, [left, right], BLN)
        return left

    def p_not(self):
        # Architect spells logical NOT as "!" (the word "not" is not an operator there)
        if self.peek()[1] == "!":
            _, _, pos = self.take()
            return self.node("!", pos, [self.p_not()], BLN)
        return self.p_cmp()

    def p_cmp(self):
        left = self.p_add()
        while self.peek()[1] in ("==", "!=", ">=", "<=", ">", "<"):
            _, op, pos = self.take()
            right = self.p_add()
            left = self.node(op, pos, [left, right], BLN)
        return left

    def p_add(self):
        left = self.p_mul()
        while self.peek()[1] in ("+", "-"):
            _, op, pos = self.take()
            right = self.p_mul()
            left = self.node(op, pos, [left, right], self.typeof(left))
        return left

    def p_mul(self):
        left = self.p_postfix()
        while self.peek()[1] in ("*", "/"):
            _, op, pos = self.take()
            right = self.p_postfix()
            left = self.node(op, pos, [left, right], DEC if op == "/" else self.typeof(left))
        return left

    def p_postfix(self):
        n = self.p_primary()
        while self.peek()[1] == "[":
            _, _, pos = self.take()
            idx = self.p_or()
            self.take("]")
            coll_type = self.typeof(n)
            n = self.node("[", pos, [n, idx], coll_type.replace("_coll", ""))
        return n

    def p_primary(self):
        kind, val, pos = self.peek()
        if kind == "str":
            self.take()
            return {"lit": {"pos": pos + 1, "text": val[1:-1].replace('\\"', '"'), "type": STR}}
        if kind == "num":
            self.take()
            return {"lit": {"pos": pos, "text": val, "type": DEC if "." in val else INT}}
        if kind == "op" and val == "(":
            self.take()
            n = self.p_or()
            self.take(")")
            return n
        if kind == "ident":
            self.take()
            if val in ("true", "false"):
                return {"lit": {"pos": pos, "text": val, "type": BLN}}
            if self.peek()[1] == "(":                       # function call
                self.take("(")
                args = []
                if self.peek()[1] != ")":
                    args.append(self.p_or())
                    while self.peek()[1] == ",":
                        self.take(",")
                        args.append(self.p_or())
                self.take(")")
                if val == "If":
                    typ = self.typeof(args[1])
                elif val in FUNC_TYPES:
                    typ = FUNC_TYPES[val]
                else:
                    raise SystemExit(f"Unknown function {val} in {self.text!r}; add it to FUNC_TYPES")
                return self.node(val, pos, args, typ)
            if val not in self.vars:
                raise SystemExit(f"Unknown variable {val} in {self.text!r}")
            v = self.vars[val]
            self.refs[v["id"]] = {"id": v["id"], "type": v["type"], "isSysRef": False, "name": val}
            return {"ref": {"pos": pos, "text": val, "type": v["type"], "val": v["id"]}}
        raise SystemExit(f"Unexpected token {kind} {val!r} in {self.text!r}")


# ---------------------------------------------------------------------------
# Expression value wrappers (the {config,text,type,uiMetaData,metaData,version} blobs)
# ---------------------------------------------------------------------------
class Flow:
    def __init__(self):
        self.vars = {}            # "Scope.Name" -> {"id","type","model"}
        self.flow_vars, self.state_vars = [], []
        self.tracking = 10
        self.actions = []

    def tid(self):
        self.tracking += 1
        return self.tracking

    def var(self, name, typ, *, collection=False, initial=None, is_input=False, description=""):
        vid = new_id()
        vtype = typ + "_coll" if collection else typ
        if initial is None:
            init = {"config": {"emp": {"pos": 1, "text": "", "type": vtype}}, "text": "", "type": vtype,
                    "uiMetaData": {"mode": MODE_NOVALUE}, "metaData": {}, "version": 2}
        else:
            init = {"config": {"lit": {"pos": 1, "text": str(initial), "type": typ}}, "text": str(initial),
                    "type": typ, "uiMetaData": {"mode": MODE_LIT}, "metaData": {}, "version": 2}
        model = {
            "id": vid, "isCollection": collection, "isInternal": False, "isOutput": False,
            "isInput": is_input, "isSecure": False, "version": "1.0", "__type": VAR_CLASS[typ],
            "name": name, "description": description, "initialValue": init,
        }
        self.vars[name] = {"id": vid, "type": vtype}
        (self.flow_vars if name.startswith("Flow.") else self.state_vars).append(model)
        return name

    # value blobs ---------------------------------------------------------
    def expr(self, text, typ=None):
        """Expression mode: any Architect expression."""
        p = ExprParser(text, self.vars)
        ast = p.parse()
        root = next(iter(ast.values()))
        if typ is None:
            typ = root["type"]
        root["type"] = typ
        return {"config": ast, "text": text, "type": typ, "uiMetaData": {"mode": MODE_EXPR},
                "metaData": {"references": list(p.refs.values())} if p.refs else {}, "version": 2}

    def ref(self, name):
        """Variable mode: a bare variable reference (used for outputs / assignment targets)."""
        v = self.vars[name]
        return {"config": {"ref": {"pos": 1, "text": name, "type": v["type"], "val": v["id"]}},
                "text": name, "type": v["type"], "uiMetaData": {"mode": MODE_VAR},
                "metaData": {"references": [{"id": v["id"], "type": v["type"], "isSysRef": False, "name": name}]},
                "version": 2}

    @staticmethod
    def lit(text, typ):
        return {"config": {"lit": {"pos": 1, "text": text, "type": typ}}, "text": text, "type": typ,
                "uiMetaData": {"mode": MODE_LIT}, "metaData": {}, "version": 2}

    @staticmethod
    def emp(typ):
        return {"config": {"emp": {"pos": 1, "text": "", "type": typ}}, "text": "", "type": typ,
                "uiMetaData": {"mode": MODE_NOVALUE}, "metaData": {}, "version": 2}

    # actions -------------------------------------------------------------
    def action(self, kind, name, **fields):
        a = {"trackingId": self.tid(), "id": new_id(), "name": name, "uiMetaData": {"collapsed": False},
             "__type": kind}
        a.update(fields)
        self.actions.append(a)
        return a


def path(output_id, label, first=None):
    p = {"label": label, "outputId": output_id, "uiMetaData": {"collapsed": False}}
    if first is not None:
        p["nextActionId"] = first["id"]
    return p


def chain(*actions):
    """Link a linear sequence with nextAction and return the first action."""
    acts = [a for a in actions if a is not None]
    for a, b in zip(acts, acts[1:]):
        a["nextAction"] = b["id"]
    return acts[0]


# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------
GET_NAME = "GC - Get Message Conversation Activity"
SET_NAME = "GC - Set Idle Alert Attributes"
GET_OUTPUTS = ["customerParticipantIds", "customerMessageTimes", "agentMessageTimes",
               "connectedAgentUserIds", "connectedAgentTimes", "alertingAgentUserIds"]
SET_INPUTS = ["conversationId", "participantId", "idleAlert", "idleMinutes", "idleSeq"]


def build(args):
    f = Flow()
    # --- trigger inputs (names must match the event's top-level attributes) ---
    for n in ("conversationId", "participantId", "userId", "queueId", "mediaType"):
        f.var(f"Flow.{n}", STR, is_input=True, description="Set by the trigger (TopLevelPrimitives)")

    # --- settings ---
    f.var("State.ThresholdMinutes", INT, initial=args.threshold,
          description="Minutes of agent silence before the first alert")
    f.var("State.RepeatEveryMinutes", INT, initial=args.repeat,
          description="Re-alert interval while still idle; 0 = alert once")
    f.var("State.OnlyWhenCustomerWaiting", BLN, initial="true" if args.only_when_waiting else "false",
          description="Alert only when the customer's last message is newer than the agent's")
    f.var("State.MaxPolls", INT, initial=args.max_polls, description="Safety cap on one-minute polls")

    # --- working variables ---
    for n in ("Seq", "LastAlertMinutes", "Failures", "AgentIndex", "IdleMinutes", "PollIndex"):
        f.var(f"State.{n}", INT, initial=0 if n in ("Seq", "LastAlertMinutes", "Failures") else None)
    for n in ("AlertActive", "HasAgentMsg", "HasCustomerMsg", "CustomerWaiting"):
        f.var(f"State.{n}", BLN, initial="false" if n == "AlertActive" else None)
    for n in ("AgentConnectedTime", "LastAgentMsg", "LastCustomerMsg", "IdleSince"):
        f.var(f"State.{n}", DTM)
    f.var("State.CustomerParticipantId", STR)
    for n in GET_OUTPUTS:
        f.var(f"State.{n}", STR, collection=True, description="Output of " + GET_NAME)

    get_id = args.get_action_id or "custom_-_" + new_id()
    set_id = args.set_action_id or "custom_-_" + new_id()
    category = {"id": args.integration_id or "", "name": args.integration_name}
    timeout = f.lit("P0D0H0M20S0F", DUR)

    def set_action(name, alert_value):
        return f.action("DataAction", name, actionName=SET_NAME, actionId=set_id, category=category,
                        useSuggestedTimeout=True, errorBindings=[],
                        inputs=[
                            {"name": "conversationId", "value": f.expr("Flow.conversationId")},
                            {"name": "participantId", "value": f.expr("State.CustomerParticipantId")},
                            {"name": "idleAlert", "value": f.lit(alert_value, STR)},
                            {"name": "idleMinutes", "value": f.expr("ToString(State.IdleMinutes)")},
                            {"name": "idleSeq", "value": f.expr("ToString(State.Seq)")},
                        ],
                        outputs=[], timeout=dict(timeout),
                        paths=[path("__SUCCESS__", "Success"), path("__FAILURE__", "Failure"),
                               path("__TIMEOUT__", "Timeout")])

    def failure_branch():
        bump = f.action("UpdateVariableAction", "Count failure", variables=[
            {"expression": f.expr("State.Failures + 1"), "variable": f.ref("State.Failures")}])
        give_up = f.action("ExitLoopAction", "Give up after repeated failures")
        retry = f.action("NextLoopAction", "Try again next minute")
        dec = f.action("DecisionAction", "Too many failures?", expression=f.expr("State.Failures >= 5"),
                       paths=[path("__YES__", "Yes", give_up), path("__NO__", "No", retry)])
        return chain(bump, dec)

    # ---- loop body -----------------------------------------------------------
    wait = f.action("WaitAction", "Wait 1 minute", duration=f.lit("P0D0H1M0S0F", DUR),
                    trimDurationNearMaxRunningTime=f.lit("false", BLN))

    get = f.action("DataAction", "Get conversation activity", actionName=GET_NAME, actionId=get_id,
                   category=category, useSuggestedTimeout=True, errorBindings=[],
                   inputs=[{"name": "conversationId", "value": f.expr("Flow.conversationId")}],
                   outputs=[{"name": n, "value": f.ref(f"State.{n}")} for n in GET_OUTPUTS],
                   timeout=dict(timeout), paths=[])

    find_agent = f.action("UpdateVariableAction", "Find our agent", variables=[
        {"expression": f.expr("FindFirst(State.connectedAgentUserIds, Flow.userId)"),
         "variable": f.ref("State.AgentIndex")}])

    still_alerting_next = f.action("NextLoopAction", "Still ringing - check again")
    gone_exit = f.action("ExitLoopAction", "Agent left the conversation")
    still_alerting = f.action("DecisionAction", "Still alerting?",
                              expression=f.expr("FindFirst(State.alertingAgentUserIds, Flow.userId) >= 0"),
                              paths=[path("__YES__", "Yes", still_alerting_next), path("__NO__", "No", gone_exit)])

    compute = f.action("UpdateVariableAction", "Compute idle time", variables=[
        {"expression": f.expr("ToDateTime(State.connectedAgentTimes[State.AgentIndex])"),
         "variable": f.ref("State.AgentConnectedTime")},
        {"expression": f.expr("Count(State.agentMessageTimes) > 0"), "variable": f.ref("State.HasAgentMsg")},
        {"expression": f.expr("Count(State.customerMessageTimes) > 0"), "variable": f.ref("State.HasCustomerMsg")},
        {"expression": f.expr("If(State.HasAgentMsg, ToDateTime(State.agentMessageTimes[Count(State.agentMessageTimes) - 1]), State.AgentConnectedTime)"),
         "variable": f.ref("State.LastAgentMsg")},
        {"expression": f.expr("If(State.HasCustomerMsg, ToDateTime(State.customerMessageTimes[Count(State.customerMessageTimes) - 1]), State.AgentConnectedTime)"),
         "variable": f.ref("State.LastCustomerMsg")},
        {"expression": f.expr("If(State.LastAgentMsg > State.AgentConnectedTime, State.LastAgentMsg, State.AgentConnectedTime)"),
         "variable": f.ref("State.IdleSince")},
        {"expression": f.expr("ToInt(ToInt(DateTimeDiff(GetCurrentDateTimeUtc(), State.IdleSince)) / 60000)"),
         "variable": f.ref("State.IdleMinutes")},
        {"expression": f.expr("State.HasCustomerMsg and (!State.HasAgentMsg or State.LastCustomerMsg > State.LastAgentMsg)"),
         "variable": f.ref("State.CustomerWaiting")},
        {"expression": f.expr('If(Count(State.customerParticipantIds) > 0, State.customerParticipantIds[0], "")'),
         "variable": f.ref("State.CustomerParticipantId")},
    ])

    raise_update = f.action("UpdateVariableAction", "Mark alert raised", variables=[
        {"expression": f.expr("State.Seq + 1"), "variable": f.ref("State.Seq")},
        {"expression": f.expr("true"), "variable": f.ref("State.AlertActive")},
        {"expression": f.expr("State.IdleMinutes"), "variable": f.ref("State.LastAlertMinutes")},
    ])
    raise_set = set_action("Set IdleAlert = 1", "1")
    raise_dec = f.action("DecisionAction", "New alert or repeat due?",
                         expression=f.expr("!State.AlertActive or (State.RepeatEveryMinutes > 0 and State.IdleMinutes - State.LastAlertMinutes >= State.RepeatEveryMinutes)"),
                         paths=[path("__YES__", "Yes", chain(raise_update, raise_set)), path("__NO__", "No")])

    clear_update = f.action("UpdateVariableAction", "Mark alert cleared", variables=[
        {"expression": f.expr("State.Seq + 1"), "variable": f.ref("State.Seq")},
        {"expression": f.expr("false"), "variable": f.ref("State.AlertActive")},
        {"expression": f.expr("0"), "variable": f.ref("State.LastAlertMinutes")},
    ])
    clear_set = set_action("Set IdleAlert = 0", "0")
    clear_dec = f.action("DecisionAction", "Clear active alert?", expression=f.expr("State.AlertActive"),
                         paths=[path("__YES__", "Yes", chain(clear_update, clear_set)), path("__NO__", "No")])

    idle_dec = f.action("DecisionAction", "Idle beyond threshold?",
                        expression=f.expr("(State.IdleMinutes >= State.ThresholdMinutes) and (State.CustomerWaiting or !State.OnlyWhenCustomerWaiting)"),
                        paths=[path("__YES__", "Yes", raise_dec), path("__NO__", "No", clear_dec)])

    connected_dec = f.action("DecisionAction", "Agent still connected?", expression=f.expr("State.AgentIndex >= 0"),
                             paths=[path("__YES__", "Yes", chain(compute, idle_dec)),
                                    path("__NO__", "No", still_alerting)])

    get["paths"] = [path("__SUCCESS__", "Success", chain(find_agent, connected_dec)),
                    path("__FAILURE__", "Failure", failure_branch()),
                    path("__TIMEOUT__", "Timeout", failure_branch())]

    body_first = chain(wait, get)

    end = f.action("EndWorkflowAction", "End Workflow", exitReason=f.emp(STR))
    loop = f.action("LoopAction", "Poll every minute", currentIndex=f.ref("State.PollIndex"),
                    loopCount=f.expr("State.MaxPolls"),
                    path={"nextActionId": body_first["id"], "label": "Loop", "outputId": "__LOOP__",
                          "uiMetaData": {"collapsed": False}})
    chain(loop, end)

    # ---- containers -----------------------------------------------------------
    state = {"startAction": loop["id"], "trackingId": 10, "id": new_id(), "name": "Initial State",
             "uiMetaData": {"collapsed": False}, "__type": "State", "actionList": f.actions,
             "variables": f.state_vars}

    def flattened(props):
        return {"type": "object", "properties": {k: {"type": v, "name": k, "required": False} for k, v in props}}

    get_in = [("conversationId", "string")]
    get_out = [(n, "array") for n in GET_OUTPUTS]
    set_in = [(n, "string") for n in SET_INPUTS]
    set_out = [("status", "string")]

    def asa(aid, name, ins, outs):
        return {"id": aid, "name": name, "category": args.integration_name,
                "contract": {"input": {"inputSchemaFlattened": {"schema": flattened(ins)}},
                             "output": {"successSchemaFlattened": {"schema": flattened(outs)}}},
                "inputSchema": flattened(ins), "successSchema": flattened(outs)}

    def manifest_entry(aid, name, usages):
        return {"name": name, "id": aid,
                "context": [{"actionName": "DataAction", "id": a["id"], "name": a["name"]} for a in usages]}

    flow = {
        "nextTrackingNumber": f.tracking + 1,
        "defaultLanguage": "en-US",
        "description": "Alerts the agent script when a chat/messaging interaction has had no agent reply for "
                       "State.ThresholdMinutes. Started per interaction by a trigger on "
                       "v2.detail.events.conversation.{id}.user.start.",
        "initialSequence": state["id"],
        "name": args.name,
        "uiMetaData": {"bridgeServerActions": [], "screenPops": [],
                       "actionServiceActions": [asa(get_id, GET_NAME, get_in, get_out),
                                                asa(set_id, SET_NAME, set_in, set_out)]},
        "supportedLanguages": ["en-US"],
        "manifest": {"dataAction": [manifest_entry(get_id, GET_NAME, [get]),
                                    manifest_entry(set_id, SET_NAME, [raise_set, clear_set])],
                     "language": [{"id": "en-US"}], "userPrompt": [], "systemPrompt": []},
        "type": "workflow",
        "debugSettings": {},
        "errorHandling": {"handlingType": "EndWorkflow"},
        "customDefinitions": {"dataTypeDefinitions": []},
        "defaultSettings": {},
        "flowMetaData": {"flowDocumentVersion": "1.0", "minimumServerVersion": "1.0", "features": []},
        "flowSequenceItemList": [state],
        "supportedLanguageOptions": [{"language": "en-US"}],
        "variables": f.flow_vars,
        "versionMetaData": {
            "inputSchema": {"$schema": "http://json-schema.org/draft-04/schema#", "title": args.name,
                            "type": "object", "additionalProperties": False,
                            "properties": {v["name"].split(".", 1)[1]: {"type": "string"} for v in f.flow_vars}},
            "outputSchema": {"$schema": "http://json-schema.org/draft-04/schema#", "title": args.name,
                             "type": "object", "additionalProperties": False, "properties": {}},
        },
    }
    return flow


def encode_i3(flow: dict) -> str:
    """Exactly what Architect's export does: btoa(encodeURIComponent(JSON.stringify(cfg)))."""
    raw = json.dumps(flow, separators=(",", ":"), ensure_ascii=False)
    uri = urllib.parse.quote(raw, safe="-_.!~*'()")
    return base64.b64encode(uri.encode("ascii")).decode("ascii")


def decode_i3(content: str) -> dict:
    return json.loads(urllib.parse.unquote(base64.b64decode(content).decode("ascii")))


def validate(flow):
    ids = {a["id"] for a in flow["flowSequenceItemList"][0]["actionList"]}
    problems = []
    for a in flow["flowSequenceItemList"][0]["actionList"]:
        if a.get("nextAction") and a["nextAction"] not in ids:
            problems.append(f"{a['name']}: bad nextAction")
        for p in a.get("paths", []) + ([a["path"]] if "path" in a else []):
            if p.get("nextActionId") and p["nextActionId"] not in ids:
                problems.append(f"{a['name']}: bad path target")
    # every action must be reachable exactly once as a chain head or via nextAction
    referenced = [a.get("nextAction") for a in flow["flowSequenceItemList"][0]["actionList"] if a.get("nextAction")]
    for a in flow["flowSequenceItemList"][0]["actionList"]:
        referenced += [p["nextActionId"] for p in a.get("paths", []) if p.get("nextActionId")]
        if "path" in a and a["path"].get("nextActionId"):
            referenced.append(a["path"]["nextActionId"])
    referenced.append(flow["flowSequenceItemList"][0]["startAction"])
    from collections import Counter
    c = Counter(referenced)
    for i in ids:
        if c[i] != 1:
            name = next(a["name"] for a in flow["flowSequenceItemList"][0]["actionList"] if a["id"] == i)
            problems.append(f"{name}: referenced {c[i]} times (must be exactly 1)")
    return problems


def trigger_json(args):
    return {
        "name": args.name + " - agent assigned",
        "description": "Starts the Chat Idle Monitor workflow each time an agent is assigned a messaging interaction.",
        "topicName": "v2.detail.events.conversation.{id}.user.start",
        "enabled": False,
        "delayBySeconds": 0,
        "eventTTLSeconds": 600,
        "target": {"type": "Workflow", "id": "<WORKFLOW_ID - paste the id of the published 'Chat Idle Monitor' workflow>",
                   "workflowTargetSettings": {"dataFormat": "TopLevelPrimitives"}},
        "matchCriteria": [
            {"jsonPath": "mediaType", "operator": "Equal", "value": "MESSAGE"}
        ],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="Chat Idle Monitor")
    ap.add_argument("--threshold", type=int, default=3, help="minutes of agent silence before the first alert")
    ap.add_argument("--repeat", type=int, default=3, help="re-alert interval in minutes (0 = once)")
    ap.add_argument("--max-polls", type=int, default=240, help="one-minute polls before giving up (240 = 4 h)")
    ap.add_argument("--only-when-waiting", type=lambda s: s.lower() != "false", default=True,
                    help="true/false: alert only when the customer is waiting for a reply")
    ap.add_argument("--get-action-id", help="your org's id of '%s' (custom_-_<uuid>)" % GET_NAME)
    ap.add_argument("--set-action-id", help="your org's id of '%s' (custom_-_<uuid>)" % SET_NAME)
    ap.add_argument("--integration-id", default="", help="id of the Genesys Cloud Data Actions integration (optional)")
    ap.add_argument("--integration-name", default="Genesys Cloud Data Actions")
    ap.add_argument("--out-dir", default="workflow")
    ap.add_argument("--trigger-dir", default="trigger")
    args = ap.parse_args()

    flow = build(args)
    problems = validate(flow)
    if problems:
        raise SystemExit("Validation problems:\n  " + "\n  ".join(problems))

    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    i3 = out / f"{args.name}.i3WorkFlow"
    content = encode_i3(flow)
    i3.write_text(content, encoding="ascii")
    assert decode_i3(content) == flow
    (out / f"{args.name}.decoded.json").write_text(json.dumps(flow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    tdir = Path(args.trigger_dir); tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{args.name}.trigger.json").write_text(json.dumps(trigger_json(args), indent=2) + "\n", encoding="utf-8")

    n = len(flow["flowSequenceItemList"][0]["actionList"])
    print(f"Wrote {i3} ({i3.stat().st_size // 1024} KB, {n} actions), {out / (args.name + '.decoded.json')}, "
          f"{tdir / (args.name + '.trigger.json')}")
    if not (args.get_action_id and args.set_action_id):
        print("Note: data action ids are placeholders; Architect resolves them by name on import "
              "(names must match the imported data actions). Pass --get-action-id/--set-action-id to bake in your ids.")


if __name__ == "__main__":
    main()
