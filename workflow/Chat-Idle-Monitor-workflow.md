# Architect workflow: `Chat Idle Monitor`

This workflow is the "brain" of the solution. One instance runs per chat/messaging
interaction from the moment an agent is assigned to it. Every minute it checks who
sent the last message and when, and it writes the result to participant data on the
customer participant. The agent script (`script/Chat-Idle-Alert.script`) only reads
those three attributes:

| Participant attribute | Meaning |
|---|---|
| `IdleAlert`   | `"1"` while an alert is active, `"0"` when it is cleared |
| `IdleMinutes` | Minutes since the agent's last activity (last message sent, or connect time) |
| `IdleSeq`     | Counter incremented on **every** change. The script reacts only when it changes. |

Everything below is built in **Architect > Workflows** with the standard toolbox.
No Archy, Terraform or external services are needed.

---

## 0. Prerequisites

1. Integration **Genesys Cloud Data Actions** is active (Admin > Integrations).
   Its OAuth client's role must be allowed to read conversations and edit participant
   attributes. Check the "Required permissions" box in the API Explorer for
   `GET /api/v2/conversations/messages/{conversationId}` and
   `PATCH /api/v2/conversations/{conversationId}/participants/{participantId}/attributes`
   and add them to that role (typically *Conversation > Communication > View* and
   *Conversation > Communication > Edit*).
2. Import the two data actions from `data-actions/` (Admin > Integrations > Actions >
   **Import**), then **Save & Publish** each. Test the GET one with a real conversation ID
   from a finished web messaging interaction and confirm the arrays come back populated.

---

## 1. Create the workflow

Architect > Flows > **Workflow** > Add. Name: `Chat Idle Monitor`.

### 1.1 Flow-level **input** variables (names must match the trigger event exactly)

| Name | Type | Input |
|---|---|---|
| `Flow.conversationId` | String | yes |
| `Flow.participantId`  | String | yes |
| `Flow.userId`         | String | yes |
| `Flow.queueId`        | String | yes |
| `Flow.mediaType`      | String | yes |

### 1.2 Task variables (settings you tune)

| Name | Type | Default | Purpose |
|---|---|---|---|
| `Task.ThresholdMinutes`        | Integer | `3`    | Minutes of agent silence before the first alert |
| `Task.RepeatEveryMinutes`      | Integer | `3`    | Re-alert interval while still idle (`0` = alert once) |
| `Task.OnlyWhenCustomerWaiting` | Boolean | `true` | Alert only if the customer's last message is newer than the agent's (recommended) |
| `Task.MaxPolls`                | Integer | `240`  | Safety cap: 240 one-minute polls = 4 hours per interaction |

### 1.3 Task working variables

`Task.Seq` (Integer, 0), `Task.AlertActive` (Boolean, false), `Task.LastAlertMinutes` (Integer, 0),
`Task.Failures` (Integer, 0), `Task.AgentIndex` (Integer), `Task.AgentConnectedTime` (DateTime),
`Task.LastAgentMsg` (DateTime), `Task.LastCustomerMsg` (DateTime), `Task.IdleSince` (DateTime),
`Task.IdleMinutes` (Integer), `Task.HasAgentMsg` (Boolean), `Task.HasCustomerMsg` (Boolean),
`Task.CustomerWaiting` (Boolean), `Task.CustomerParticipantId` (String), `Task.PollIndex` (Integer)

Data-action outputs (String **collections**):
`Task.customerParticipantIds`, `Task.customerMessageTimes`, `Task.agentMessageTimes`,
`Task.connectedAgentUserIds`, `Task.connectedAgentTimes`, `Task.alertingAgentUserIds`

---

## 2. Flow logic

```
Start
└─ Loop  "Poll"           (Loop count: Task.MaxPolls, index: Task.PollIndex)
   ├─ Wait                 (Duration literal: 0 days 0 hours 1 minute 0 seconds)
   ├─ Call Data Action     "GC - Get Message Conversation Activity"
   │     input  conversationId = Flow.conversationId
   │     outputs -> the six Task.* collections above
   │     Failure path: Update Data Task.Failures = Task.Failures + 1
   │                   Decision Task.Failures >= 5 ? Exit Loop : Next Loop
   ├─ Update Data          Task.AgentIndex = FindFirst(Task.connectedAgentUserIds, Flow.userId)
   ├─ Decision  "Agent still connected?"   Task.AgentIndex >= 0
   │     No  ─ Decision "Still alerting?"  FindFirst(Task.alertingAgentUserIds, Flow.userId) >= 0
   │             Yes -> Next Loop        (agent has not accepted yet)
   │             No  -> Exit Loop        (agent disconnected / transferred / chat ended)
   │     Yes ─ continue
   ├─ Update Data  "Compute activity"   (one Update Data action, statements in this order)
   │     Task.AgentConnectedTime    = ToDateTime(Task.connectedAgentTimes[Task.AgentIndex])
   │     Task.HasAgentMsg           = Count(Task.agentMessageTimes) > 0
   │     Task.HasCustomerMsg        = Count(Task.customerMessageTimes) > 0
   │     Task.LastAgentMsg          = If(Task.HasAgentMsg,
   │                                     ToDateTime(Task.agentMessageTimes[Count(Task.agentMessageTimes) - 1]),
   │                                     Task.AgentConnectedTime)
   │     Task.LastCustomerMsg       = If(Task.HasCustomerMsg,
   │                                     ToDateTime(Task.customerMessageTimes[Count(Task.customerMessageTimes) - 1]),
   │                                     Task.AgentConnectedTime)
   │     Task.IdleSince             = If(Task.LastAgentMsg > Task.AgentConnectedTime, Task.LastAgentMsg, Task.AgentConnectedTime)
   │     Task.IdleMinutes           = ToInt(ToInt(DateTimeDiff(GetCurrentDateTimeUtc(), Task.IdleSince)) / 60000)
   │     Task.CustomerWaiting       = Task.HasCustomerMsg and (not Task.HasAgentMsg or Task.LastCustomerMsg > Task.LastAgentMsg)
   │     Task.CustomerParticipantId = If(Count(Task.customerParticipantIds) > 0, Task.customerParticipantIds[0], "")
   ├─ Decision  "Idle?"
   │     (Task.IdleMinutes >= Task.ThresholdMinutes) and (Task.CustomerWaiting or not Task.OnlyWhenCustomerWaiting)
   │     Yes ─ Decision "Raise / repeat?"
   │     │       not Task.AlertActive or (Task.RepeatEveryMinutes > 0 and Task.IdleMinutes - Task.LastAlertMinutes >= Task.RepeatEveryMinutes)
   │     │       Yes ─ Update Data   Task.Seq = Task.Seq + 1 ; Task.AlertActive = true ; Task.LastAlertMinutes = Task.IdleMinutes
   │     │             Call Data Action "GC - Set Idle Alert Attributes"
   │     │                 conversationId = Flow.conversationId
   │     │                 participantId  = Task.CustomerParticipantId
   │     │                 idleAlert      = "1"
   │     │                 idleMinutes    = ToString(Task.IdleMinutes)
   │     │                 idleSeq        = ToString(Task.Seq)
   │     │       No  ─ (nothing)
   │     No  ─ Decision "Clear active alert?"   Task.AlertActive
   │             Yes ─ Update Data   Task.Seq = Task.Seq + 1 ; Task.AlertActive = false ; Task.LastAlertMinutes = 0
   │                   Call Data Action "GC - Set Idle Alert Attributes"  (same inputs, idleAlert = "0")
   │             No  ─ (nothing)
   └─ (loop continues)
End Workflow
```

Notes on expressions:

* `FindFirst(collection, value)` returns `-1` when not found; `Count()` and `[index]` work on collections.
* `DateTimeDiff(a, b)` returns a Duration; `ToInt(duration)` gives milliseconds, hence `/ 60000`.
  If your editor flags `DateTimeDiff`, use `ToInt(GetCurrentDateTimeUtc() - Task.IdleSince)` instead.
* The data action returns the last 30 message timestamps per side (`messages[-30:]`). The
  conversation API lists messages in chronological order, so the last element is the newest.
* Attribute values are strings. The script compares `IdleAlert` to the text `1`.

---

## 3. Trigger (Admin > Triggers > Add)

| Field | Value |
|---|---|
| Name | `Chat Idle Monitor - agent assigned` |
| Topic | `v2.detail.events.conversation.{id}.user.start` |
| Target | Workflow `Chat Idle Monitor` |
| Data format | **TopLevelPrimitives** (so `conversationId`, `participantId`, `userId`, `queueId`, `mediaType` arrive as flow inputs) |
| Match criteria | `mediaType` **Equal** `MESSAGE` — open the topic's schema in the trigger UI and copy the exact enum spelling it shows |
| Optional criteria | `queueId` **In** `[<your chat queue IDs>]` to limit it to chat queues |

If you still run legacy ACD web chat (media type `CHAT`), create a second trigger with
`mediaType` Equal `CHAT` and point it at a copy of the workflow whose GET data action calls
`/api/v2/conversations/chats/${input.conversationId}/messages` instead (that API returns
`entities[].utcTime` and `entities[].sender.id`; filter by the sender participant purpose).

Enable the trigger last, after the workflow is published and the script is assigned to the queue.

---

## 4. Optional escalation

Inside the "Raise / repeat?" Yes branch add a Decision `Task.Seq >= 3`. On Yes, add a
**Send Notification** action (email or group chat to the team lead) with
`Flow.conversationId` and `Task.IdleMinutes` in the body. That action is capped at three
sends per workflow run, so keep it to the third alert and above.
