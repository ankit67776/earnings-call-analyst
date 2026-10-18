# Frontend

Next.js (App Router) UI for the analysis service.

- `/` lists analyzed filings and queues new ones (`POST /api/analyses`, then polls the job).
- `/documents/[id]` shows a filing's claims next to the filing text. Selecting a claim scrolls to its paragraph and highlights the quoted span; the underline on each citation shows how the quote matched (exact, shortened, close).

```bash
npm install
npm run dev     # http://localhost:3000, expects the backend on http://localhost:8000
npm run lint
npm run build
```

Set `BACKEND_URL` if the backend runs elsewhere. Server Components call it directly, and the browser reaches it through the `/api/*` rewrite in `next.config.ts`.
