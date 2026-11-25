# Muse MCP server (optional)

Lets an assistant you already use (Open WebUI, Claude Desktop, any MCP client) ask you Muse's
questions in conversation, record your answers, and fetch recommendations, your taste profile
and how your taste is evolving. Muse works exactly the same without it.

| tool | does |
|---|---|
| `muse_pending_questions` | the question queue, most valuable first, with the exact answer body |
| `muse_answer_question` | records an answer (ingestion API `/answers`, `muse` key) |
| `muse_dismiss_question` | closes a question without writing taste data |
| `muse_recommendations` | explained picks (`item_type`, optional `mood`) |
| `muse_taste_profile` / `muse_taste_evolution` | what Muse thinks your taste is / how it's changing |

Personal mode (one person, through the query API, docs/api/query-api.md): set `MUSE_QUERY_URL`
and `MUSE_TOKEN` (a token from Mono's "connect an assistant" or `POST /v1/tokens`), or connect
over HTTP with your own `Authorization: Bearer <token>` header. Extra tools:

| tool | does |
|---|---|
| `muse_whoami` | who the connection acts for, linked accounts |
| `me_questions` / `me_answer` / `me_skip` | questions as simple choices; answer with an option, optionally a note (kept as a fact) |
| `me_remember` / `me_facts` / `me_forget` | tell Muse what the user said about their taste (`likes` facts shape the profile) |
| `me_set_mood` / `me_set_context` / `me_context` | the current mood and context recommendations use |
| `me_recommendations` / `me_portrait` | picks, and the taste in words |
| `me_goals` / `me_add_goal` / `me_incognito` / `me_suggest_feature` | goals, pausing learning, ideas for Muse |

`pip install ./services/mcp-server` installs the `muse-mcp` command (stdio by default).

Run: `docker compose --profile mcp up -d mcp-server`, then point your MCP client at
`http://127.0.0.1:8002/mcp` (streamable HTTP). Or locally over stdio:
`MUSE_RECO_URL=http://127.0.0.1:8770 MUSE_INGEST_URL=http://127.0.0.1:8771 python server.py`.

Env: `MUSE_RECO_URL`, `MUSE_INGEST_URL`, `MUSE_READ_KEY`, `MUSE_ANSWER_KEY` (empty = read-only),
`MUSE_MCP_USER_ID` (default user), `MUSE_MCP_TRANSPORT` (`stdio` | `streamable-http`),
`MUSE_MCP_HOST`, `MUSE_MCP_PORT`. Built on the official MCP Python SDK 2.x (`MCPServer`).
