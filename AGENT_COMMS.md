# Shared agent communication

Copilot CLI, OpenCode, and Antigravity CLI are configured to use the same local MCP message bus. Agents are peers: there is no role hierarchy, message priority, or privileged agent.

For a task that benefits from collaboration:

1. Agree on a short lowercase topic slug for that task (for example, `mobile-auth`); call `list_sessions` first if the topic is unclear, and reuse the matching session.
2. Call `open_session` with the same topic and a concise purpose before planning or implementation. Use your CLI's name as `agent`.
3. Use `send_message` to share plans, findings, questions, blockers, and review feedback directly with peers. Read messages with `read_messages`, keeping the returned cursor so you can check for newer messages.
4. Coordinate substantive changes and resolve disagreements with evidence; no agent's view has automatic precedence.
5. Talk to the user directly for requirements, decisions, or the final result. Do not relay routine peer discussion through the user.

Messages are stored locally and persist across CLI restarts. This is a pull-based channel: agents must call `read_messages` to see updates; it does not wake or interrupt another agent.
