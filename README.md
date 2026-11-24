# Genesys Cloud – idle chat alert in the agent script panel

**Problem.** Agents accept a web messaging / chat interaction, get distracted, and leave the
customer without a reply. We want the agent to get an unmistakable alert *inside the script
panel* after X minutes with no activity from our side.

**Constraint.** Nothing outside Genesys Cloud: no web app, no yarn/vue, no external server.
Everything here is built from native Genesys Cloud pieces: a Script (importable `.script`
file), two Data Actions, one Architect Workflow and one Trigger.

---

## 1. Why it has to be built this way

The script panel has no timer that can watch the conversation and nothing in the platform
pushes a change into a running script on its own. What *does* exist natively:

| Native capability | Used for |
|---|---|
| Scripter custom actions support a **Delay** step (milliseconds), **If/Else**, **Try/On Error** and calling other custom actions | A polling loop inside the script, started by the page's **On Load** action |
| Built-in action **Scripter.Refresh Input Variables** re-reads participant data into Input variables | The script sees changes the workflow makes during the chat |
| Built-in action **Scripter.Alert** shows a pop-up toast; a boolean variable can drive a component's **Visible** property | The pop-up plus a persistent red banner |
| **Trigger** on `v2.detail.events.conversation.{id}.user.start` starts a **Workflow** per interaction | Server-side timing logic, independent of what the agent's browser is doing |
| Workflow **Wait**, **Loop**, date/time expressions, **Call Data Action** | Measure "minutes since the agent's last message", repeat every minute |
| Data Action `PATCH /participants/{id}/attributes` | Hand the result to the script as participant data |

Things that were checked and ruled out:

* **Native "Inactivity handling" on the queue (Nov 2025)**: one timer for both sides, action is
  disconnect/reroute only, and it has no agent-facing alert. Useful as a last resort behind this.
* **Workflow "Send Notification" action**: email or group chat only, not a toast in the client.
* **Live-bound input variables**: participant data is read on script load only; it must be
  refreshed explicitly, which is exactly what the loop does.

---

## 2. How it works

```
agent assigned ──trigger──▶ Workflow "Chat Idle Monitor"
                               every 60 s: GET conversation → who spoke last, when?
                               idle ≥ X min & customer waiting → PATCH participant attributes
                                     IdleAlert=1  IdleMinutes=4  IdleSeq=1   (customer participant)
                               agent replies → IdleAlert=0 IdleSeq=2
                               agent disconnects → workflow ends

Script panel (per interaction, started by page On Load):
   every 30 s: Delay → Refresh Input Variables → IdleSeq changed?
        IdleAlert == "1" → red banner ON + Scripter.Alert pop-up ("no reply for 4 min…")
        IdleAlert == "0" → red banner OFF
```

Re-alerts (every `RepeatEveryMinutes`) bump `IdleSeq` again, so the agent gets a new pop-up each
time while still ignoring the chat. Reloading the script is harmless: a changed `IdleSeq` is
picked up on the next tick.

---

## 3. What is in this repository

| Path | What it is |
|---|---|
| `script/Chat-Idle-Alert.script` | **Ready to import** standalone script (Admin > Scripts > Import). Banner + alert loop, 30 s tick, 4 h coverage. |
| `tools/build_script.py` | Generator. Rebuild with other timings, or **merge the alert into your existing script** (`--merge`). Standard-library Python only. |
| `data-actions/GC - Get Message Conversation Activity.json` | Data action: GET message conversation → last message times per side, connected agent, customer participant id |
| `data-actions/GC - Set Idle Alert Attributes.json` | Data action: PATCH the three attributes on the customer participant |
| `workflow/Chat-Idle-Monitor-workflow.md` | Step-by-step Architect workflow and trigger build, with every expression |

---

## 4. Setup order (about an hour)

1. **Data actions**: Admin > Integrations > Actions > Import both files, Save & Publish, test the
   GET one with a real messaging conversation ID. Make sure the Data Actions integration's OAuth
   role can read conversations and edit participant attributes (see the workflow guide).
2. **Workflow**: build `Chat Idle Monitor` from `workflow/Chat-Idle-Monitor-workflow.md`.
   Set `Task.ThresholdMinutes` to your X. Publish.
3. **Script**: Admin > Scripts > Import `script/Chat-Idle-Alert.script`, open it, check the
   three Input variables (`IdleAlert`, `IdleMinutes`, `IdleSeq`) show **Input = Yes**, Publish.
   Assign it to the chat/messaging queue(s) (Queue > Routing > Script, or the inbound message
   flow's Set Screen Pop / script setting).
   *Already have a script?* Export it and run
   `python3 tools/build_script.py --merge "My Script.script" --out "My Script + idle.script"`,
   then import the result with "replace existing" selected. The merge adds the variables, the
   custom actions, the banner at the top of the start page and chains the monitor after your
   current On Load action.
4. **Trigger**: Admin > Triggers, topic `v2.detail.events.conversation.{id}.user.start`,
   TopLevelPrimitives, criteria `mediaType = MESSAGE`, target the workflow. Enable it last.

---

## 5. Test plan

1. Start a web messaging chat as a customer, accept it as an agent, send nothing.
2. After `ThresholdMinutes` (+ up to 1 min poll + up to 30 s tick) the red banner appears and a
   pop-up shows "no reply from you for N min".
3. Reply as the agent. Within ~90 s the banner disappears.
4. As the customer send another message and stay silent as agent: alert returns and repeats
   every `RepeatEveryMinutes`.
5. Open a second interaction and switch between the two: confirm the alert for the idle one still
   shows (the toast is raised by the script of that interaction).
6. Disconnect the chat: Architect flow history shows the workflow exiting on the next poll.

Check in Performance > Workflow / flow history and in the data action's execution history if
something does not fire.

---

## 6. Tuning and limits

* **Poll rates**: workflow waits 1 min (change the Wait literal); the script ticks every 30 s
  (`--tick`, or edit the Delay step in custom action *Idle Monitor - Tick*). Worst-case latency
  from "X minutes idle" to pop-up is poll + tick.
* **Coverage**: the script loop runs for `--hours` (default 4) per script load and the workflow
  for `Task.MaxPolls` minutes. Raise both for very long-running async conversations.
* **Only when customer waiting**: default on. Turn `Task.OnlyWhenCustomerWaiting` off to alert on
  agent silence even when the customer has gone quiet too.
* **Multi-page scripts**: the loop lives on the start page's On Load action. If your script
  navigates to other pages, call *Idle Monitor - Start* from those pages' On Load actions too,
  or keep the banner container on a shared layout.
* **Preview mode** in the script editor shows "would have been updated" toasts instead of
  refreshing; test on a real interaction.
* **Legacy ACD web chat** (media type `CHAT`) needs the variant GET data action described at the
  end of the workflow guide; the script is identical.
* **API load**: one GET per minute per open chat from the workflow, one PATCH per state change.
  The script refresh reads the client's conversation state, not the API.

---

## 7. Sources used while designing this

* Scripter actions (Alert, Refresh Input Variables): https://help.genesys.cloud/articles/available-script-actions/
* Custom action steps (Delay, If/Else, Try/On Error, Info Banner, Wait Until): https://help.genesys.cloud/articles/define-custom-script-action/
* Input/output variables and which participant holds them: https://help.genesys.cloud/articles/script-input-output-variables/
* Community: workflow-set participant data shown in a script via Delay + Refresh Input Variables: https://community.genesys.com/discussion/is-it-possible-to-display-participant-data-set-in-a-trigger-workflow-in-an-agent-script
* Community: Alert action behaviour: https://community.genesys.com/discussion/dynamically-display-an-alertbanner-in-agent-script
* Triggers overview and topics: https://developer.genesys.cloud/platform/process-automation/ , https://developer.genesys.cloud/api/rest/v2/notifications/available_topics
* Native inactivity handling and its limits: https://community.genesys.com/discussion/automatic-handling-of-inactive-messaging-interactions , https://community.genesys.com/discussion/separate-inactivity-handling-for-customer-and-agent-inactivity
* Send Notification action: https://help.mypurecloud.com/?p=312349
* Data action response configuration: https://help.genesys.cloud/articles/response-configuration-data-actions/
* Date math in Architect (`DateTimeDiff`, `ToInt` → ms): https://community.genesys.com/discussion/calculate-the-difference-between-two-dates-in-architect
* Script import format: https://developer.genesys.cloud/routing/scripts/importscripts
