# Open WebUI

Status: implemented as an integration, not an adapter: Open WebUI talks to Muse the way any
assistant does, instead of Muse reading chat logs (which would be far more personal data than
recommendations need).

Two ways to connect, both with a **personal** Muse token (Mono: Settings -> Muse -> "Connect an
AI assistant", or `POST /v1/tokens`):

1. **MCP** (Open WebUI versions with MCP tool servers): add the Muse MCP endpoint
   `http://<muse-host>:8773/mcp` with header `Authorization: Bearer muse_...`.
2. **OpenAPI tool server** (any recent Open WebUI): Settings -> Tools -> add
   `http://<muse-host>:8772` (the query API; it serves `/openapi.json`) with the same bearer
   token. The model then sees Muse's operations (search, recommendations, remember, questions...).

Each person in Open WebUI should use their own token, so the assistant only ever sees and teaches
that person's profile. What the assistant learns goes through `remember` / `answer_question`,
visible and removable in Mono's Muse screen (or `GET /v1/users/me/facts`).
