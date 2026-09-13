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
