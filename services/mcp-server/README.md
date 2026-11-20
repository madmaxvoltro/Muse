# Muse MCP server

Lets an AI assistant (Claude, or any MCP client) know a person's taste, search everything Muse
has seen, get recommendations, ask the questions Muse has, and tell Muse what it learns. It
calls the [query API](../query-api/README.md) with the connection's token, so a personal token
can only ever see and teach that one person.

## Get a token

* In **Mono**: Settings → Muse → "Connect an AI assistant" (Mono asks Muse for a token for
  your account and shows a ready-to-paste config).
* Or as household admin:

      curl -X POST http://127.0.0.1:8772/v1/tokens -H "Authorization: Bearer $MUSE_ADMIN_TOKEN" \
           -H 'content-type: application/json' -d '{"user": "<user id>", "label": "Claude"}'

  `GET /v1/users` lists user ids. `{"user": null}` makes a household token (all users).

## Connect

Local (stdio), e.g. Claude Desktop / Claude Code config:

```json
{
  "mcpServers": {
    "muse": {
      "command": "muse-mcp",
      "env": { "MUSE_QUERY_URL": "http://127.0.0.1:8772", "MUSE_TOKEN": "muse_..." }
    }
  }
}
```

(`pip install ./services/mcp-server` provides `muse-mcp`; `python -m muse_mcp` works too.)

Shared HTTP endpoint (the `mcp-server` compose service, `127.0.0.1:8773` with the Vesper
override): `http://<host>:8773/mcp` with header `Authorization: Bearer muse_...`. Each client
sends its own token. Expose it only over your tailnet, like the rest of Muse.

## Tools

| Know | Ask | Teach | Accounts |
|---|---|---|---|
| `whoami`, `list_users`, `capabilities` | `get_questions` | `remember`, `list_facts`, `forget` | `list_identities` |
| `get_taste_profile` | `answer_question` | `rate_item` | `create_link_code` |
| `search`, `similar_items`, `get_item` | `skip_question` | `update_settings` | `link_account` |
| `recommend` (incl. `taste_from`) | `ask_later` | `suggest_feature`, `list_feature_suggestions` | `unlink_account` |
| `get_history` | | | |

Prompts: `taste_interview` (a short interview that fills profile gaps) and `use_my_taste`
(picks in one domain from taste in another, e.g. films for your music taste).

The server's instructions tell the assistant to ask one question at a time, only when it fits,
to store only taste and listening/viewing context (nothing sensitive), to say what it stored,
and never to mix household members' data.
