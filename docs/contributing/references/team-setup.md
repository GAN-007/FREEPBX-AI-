# Linear MCP Setup (Optional)

This document explains how contributors can connect their AI IDE to the project's Linear workspace via MCP (Model Context Protocol).

This is **optional** – join our [Discord server](https://discord.gg/CAVACtaY) for community support and feature discussions. MCP just makes it easier for tools like AVA to read/update issues directly.

---

## 1. Prerequisites

- Access to the project’s Linear workspace (you may need an invite).
- A personal Linear API key.
- An IDE or tool that supports MCP (e.g., Cursor, Windsurf, or other MCP-enabled clients).

Each developer uses their **own** Linear API key; do not share maintainer tokens.

---

## 2. Create a Linear API Key

1. Log into Linear in your browser.
2. Go to **Settings → API**.
3. Create a new personal API key.
4. Copy the key and store it securely (you will need it for MCP configuration).

Do **not** paste this key directly into chat with AI assistants.

---

## 3. Configure the `linear-mcp-server`

Follow your IDE’s instructions for configuring MCP servers. The exact steps vary by client, but the pattern is:

1. Add the Linear MCP server to your IDE’s MCP configuration.
2. Provide:
   - Your Linear API key (usually via environment variable or config file).
   - The workspace/organization as needed.
3. Restart or reload the IDE so the MCP server is active.

Refer to your IDE’s documentation for the precise configuration syntax.

---

## 4. How AVA Uses Linear via MCP

When `linear-mcp-server` is available and configured:

- AVA can:
  - Look up AAVA issues by key (e.g., `AAVA-63`, `AAVA-64`, `AAVA-65`, `AAVA-66`).
  - Read the latest description, status, and acceptance criteria.
  - Suggest appropriate status transitions (e.g., “In Progress” → “In Review”) and PR linking.
- AVA **does not**:
  - Ask for or echo API keys.
  - Assume every user has MCP configured.

If MCP is not available or fails, AVA falls back to:

- The project [Discord server](https://discord.gg/CAVACtaY) for community discussions
- `docs/ROADMAP.md` and milestone files

as the canonical specs for community-requested features.

---

## 5. Recommended Workflow for Contributors

1. **Onboard** using:
   - `docs/DEVELOPER_ONBOARDING.md`
   - An AI-powered IDE (e.g., Windsurf via the referral link in that doc)
   - AVA as your project manager persona (`AVA.mdc`)
2. **(Optional) Enable Linear MCP**:
   - Configure `linear-mcp-server` with your API key.
3. **Choose a feature**:
   - Ask AVA which AAVA issues are good starting points (e.g., queue transfer, new providers, calendar tool).
4. **Implement and test**:
   - Follow AVA’s playbook and the architecture/dev rules.
5. **Update Linear (if MCP is available)**:
   - Move the issue to “In Progress” or “In Review”.
   - Link your PR and include call IDs / RCA summaries.

This keeps the repo docs and Linear issues in sync, while letting AVA coordinate work across all tools and IDEs.
