# Audiobookshelf adapter

Status: implemented (polling the Audiobookshelf API with an admin token). Setup:
[`docs/adapters/audiobookshelf.md`](../../docs/adapters/audiobookshelf.md).

Emits `cataloged` for every book (title, author, narrator, series, genres, description),
`played` (once per book per day while listening) and `completed` from each user's media
progress. Podcasts are skipped for now.
