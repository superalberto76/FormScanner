# FormScanner frontend

React + TypeScript + Vite single-page application: projects and variants (the canvas editor of the printable pages), batches of scanned submissions, review and export.

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `VITE_API_BASE_URL` | Base URL of the backend API | `/api/v1` |

The value is read at **build time** (Vite inlines it into the bundle): change it, then build again. For local development, set it in a `.env.local` file:

```bash
VITE_API_BASE_URL=http://localhost:8000/api/v1
```

With the default `/api/v1` the frontend expects the API on its own origin (a reverse proxy in front of both). With a full URL the browser calls the backend directly, which then has to allow the frontend's origin in CORS.

## Development

```bash
npm install
npm run dev       # dev server with hot reload on http://localhost:5173
```

The dev server has no proxy: the backend must be running at `VITE_API_BASE_URL`.

## Build

```bash
npm run build     # type check, then production build into dist/
npm run preview   # serve dist/ locally
```

The `Dockerfile` builds the app (with the `VITE_API_BASE_URL` build argument) and serves `dist/` with nginx (`nginx.conf`), falling back to `index.html` for client-side routes.

## Checks

```bash
npm test                 # unit tests of the pure modules (node --test)
npm run typecheck        # the app
npm run typecheck:test   # the tests and the e2e
npm run build
```

Always run `tsc` with `-p`: the root `tsconfig.json` only references the two projects. Browser end-to-end tests (Playwright) run on request with `npm run e2e`: see [e2e/README.md](e2e/README.md).

## Structure of `src/`

| Folder | Contents |
|---|---|
| `api/` | HTTP client, error type, endpoint functions and the types aligned with the backend schemas |
| `auth/` | Session store, bootstrap of the current user, password policy |
| `components/` | UI components; the object editors in `object-editors/` and `ObjectForms/` |
| `hooks/` | Data and image hooks shared by pages and components |
| `pages/` | One component per route |
| `theme/` | Light / dark theme |
| `utils/` | Helpers shared across the app, among them the PDF layout constants (`exportLayout.ts`) |
| `styles/` | Stylesheets of single screens; the global one is `index.css` |

Logic that can be tested without React lives in plain `.ts` modules (no JSX), each with its `*.test.ts` next to it.
