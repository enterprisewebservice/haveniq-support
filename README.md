# HavenIQ voice support agent

A voice-based customer support agent for HavenIQ (a fictional smart-home company), built on the same
pattern as upstreambeat.ai's live room: LiveKit Cloud is the media plane, everything else runs on the
OpenShift cluster and is declared in git.

| Piece | What it is | Where |
|---|---|---|
| `voice/` | LiveKit Agents worker: greets the caller, looks up the account, answers device and order questions, records the call, hands off to a person | Deployment in ns `haveniq` |
| `cloud/` | HavenIQ cloud: the customer's account, devices, orders (mock data) exposed as MCP tools behind the platform's MCP gateway, plus the caller page and the human desk | Deployment + Route |
| `crm-mcp/` | Zammad bridge: ticket, note, attachment and handoff tools behind the same gateway | Deployment |
| `deploy/zammad/` | Zammad (the helpdesk the human agents work in) with Postgres, Redis and memcached | Helm values + manifests |
| `deploy/openshift/` | Namespace, LiveKit credentials (Vault → ExternalSecret), recordings bucket (NooBaa), gateway registrations, builds | Argo application |

## How a call flows

1. The caller opens `/call`, types the phone number on the account (optional) and starts the call. The page asks the
   cloud service for a LiveKit token; that token creates a room and asks LiveKit to dispatch the `haveniq-support` worker
   with the phone number as metadata.
2. The worker starts recording (LiveKit Egress into the cluster's NooBaa bucket), looks the account up through the MCP
   gateway before the first word, and greets the caller by name.
3. Every question is answered with a tool call that goes through the gateway: `home_get_orders`, `home_device_state`,
   `home_set_thermostat`. The gateway authorizes the worker's own key and logs each call. The worker holds no other credential.
4. When the caller asks for a person, the model calls `crm_open_ticket` (summary into Zammad), `crm_request_handoff`
   (ticket raised, Mattermost `#haveniq-support` paged with a join link) and `transfer_to_human` (summary registered for the
   desk page). A human opens the link, reads the summary, joins the same room; the worker mutes itself.
5. When the last person leaves, the worker stops the egress, waits for the file, and the CRM bridge attaches the recording to
   the ticket (a presigned link, and the file itself when small enough).

## Running it

```bash
# images (binary builds from the repository root)
for b in haveniq-cloud haveniq-crm haveniq-voice; do oc start-build $b -n haveniq --from-dir=. ; done
# everything else is in git and synced by Argo (deploy/argo/haveniq.yaml bootstraps the two Applications)
```

Secrets that a person writes once (nothing else is typed anywhere; the rest is generated or mirrored by External Secrets):

```bash
oc exec -n vault vault-0 -- vault kv put agent-office/haveniq-livekit LIVEKIT_URL=wss://<project>.livekit.cloud LIVEKIT_API_KEY=<key> LIVEKIT_API_SECRET=<secret>
oc exec -n vault vault-0 -- vault kv put agent-office/haveniq-zammad ZAMMAD_TOKEN=<token created in Zammad: Profile → Token Access>
```

Test without a microphone: `lk dispatch create --new-room --agent-name haveniq-support --metadata '{"phone":"+1 612 555 0142"}'`,
then join the room with a token and type in LiveKit Meet's chat; the worker treats typed text as speech.

## The case worker

A second agent, declared the framework way in `deploy/manifests/60-case-worker.yaml`: an AgentGateway, an AgentWorkstation
with its own generated gateway caller key, and a Skill artifact that says how to file a call. When a call ends the voice
worker posts the transcript to the case worker's OpenAI-compatible face; the case worker confirms the account, opens the
ticket if the voice agent could not, adds one structured note (category, questions and answers with their facts, actions,
follow-ups from the account state, sentiment) and replies with a three-line filing. Support staff can also ask it about a
call in its Mattermost channel. Its brain is the `claude-direct` ModelConnection (`61-model-connection.yaml`): the same
models and key as `claude-work`, straight to the model desk, because the guardrails orchestrator rejects the structured
message content the openclaw runtime sends.
