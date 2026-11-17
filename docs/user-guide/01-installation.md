# 1. Installation and first start

This chapter installs FormScanner on a machine with Docker, signs in as the first administrator and covers what an administrator needs afterwards: access from other machines, the OCR language, updates, backups and the configuration.

## Contents

1. [Requirements](#11-requirements)
2. [Install](#12-install)
3. [First start](#13-first-start)
4. [Access from another machine](#14-access-from-another-machine)
5. [OCR language](#15-ocr-language)
6. [Update](#16-update)
7. [Backup and restore](#17-backup-and-restore)
8. [Database access](#18-database-access)
9. [Configuration](#19-configuration)
10. [If it does not start](#110-if-it-does-not-start)

## 1.1 Requirements

- Docker Engine with the Compose plugin (`docker compose`, version 2), on Linux, macOS, Windows or a NAS that runs Docker.
- About 2 GB of RAM and 5 GB of disk for the images and the build, plus the space of your scans.
- `git`, to download the code (or download the repository as an archive and unpack it).
- A modern browser (Firefox, Chrome, Edge, Safari) on the machines of the users.

FormScanner runs as three containers: the database (PostgreSQL), the backend (the API, the PDF export and the reading of the scans) and the frontend (the web application). Everything is built on your machine from the repository: no external service is needed, and no data leaves the machine.

## 1.2 Install

Download the code into a new folder:

```bash
git clone https://github.com/superalberto76/FormScanner.git
cd FormScanner
cp .env.example .env
```

`.env` holds the configuration. Open it in a text editor and change, at least:

| Variable | Set it to |
|---|---|
| `JWT_SECRET` | A long random string, for example the output of `openssl rand -hex 32`. It signs the users' sessions: anyone who knows it can sign in as anyone |
| `POSTGRES_PASSWORD` | A password for the database. Write the same password inside `DATABASE_URL`, in place of `change_me_db_password` |
| `INITIAL_ADMIN_EMAIL` | The email of the first administrator, which is also the login name |
| `INITIAL_ADMIN_PASSWORD` | The password of the first administrator. Change it at the first sign-in, from **Profile** ([4.10](04-reference.md#410-your-account)) |

Create the data folders, then build and start:

```bash
mkdir -p data/postgres data/storage
docker compose up -d --build
```

Some Docker installations, such as Synology, do not create missing folders by themselves: create them as above in any case.

The first build takes several minutes. At every start the backend brings the database to the current schema and makes sure the administrator account exists: no other step is needed.

**The name of the folder matters.** Docker Compose names the installation after its folder (`formscanner` for a folder called `FormScanner`). If another Compose project with the same name already runs on the machine, the two share their containers: use a different folder name, or set `COMPOSE_PROJECT_NAME` in `.env`.

## 1.3 First start

1. Open <http://localhost:8080> in a browser on the same machine (the port is `FRONTEND_PORT` in `.env`).
2. Sign in with `INITIAL_ADMIN_EMAIL` and `INITIAL_ADMIN_PASSWORD`.
3. Change the password: open the menu of your avatar, top right, choose **Profile** and fill in **Change password** ([4.10](04-reference.md#410-your-account)). The new password does not go back into `.env`: the backend only uses `INITIAL_ADMIN_PASSWORD` to create the account, or to reset it ([1.9](#19-configuration)).
4. Create the accounts of the other users: **Add user** in the same menu ([4.11](04-reference.md#411-user-administration)).

You are ready to design the first form: [chapter 3](03-walkthrough.md) goes through it step by step.

`docker compose ps` shows whether the three services are running. If the page does not open, see [1.10](#110-if-it-does-not-start).

## 1.4 Access from another machine

The browser calls the API directly, at an address built into the web application. With the default values FormScanner works only from the machine it runs on. To open it from other machines, set in `.env` the name or the address of the Docker host, here `scanner.example.lan`:

```bash
VITE_API_BASE_URL=http://scanner.example.lan:8000/api/v1
CORS_ORIGINS=http://scanner.example.lan:8080
```

and rebuild:

```bash
docker compose up -d --build
```

Both ports, `FRONTEND_PORT` (8080) and `BACKEND_PORT` (8000), must be reachable from the users' machines. The users then open `http://scanner.example.lan:8080`.

`VITE_API_BASE_URL` is read when the web application is built: changing it always needs `--build`. `CORS_ORIGINS` lists every address the web application is opened from, separated by commas; if users open it both by name and by IP address, list both.

FormScanner serves plain HTTP. To reach it over a network you do not trust, put it behind a reverse proxy that serves HTTPS, and use the HTTPS addresses in the two variables.

## 1.5 OCR language

The handwriting in free-text areas is read with Tesseract, in English by default. The reading is a suggestion that a person always checks ([4.7](04-reference.md#47-review)), so the language only changes how good the suggestion is.

To change it, set `OCR_LANGUAGE` in `.env` to a Tesseract language code, or several joined with `+` (for example `ita+eng`), then restart:

```bash
docker compose up -d
```

The image includes English (`eng`) and Italian (`ita`). For another language add its `tesseract-ocr-<code>` package to `backend/Dockerfile` and rebuild with `docker compose up -d --build`. The backend does not start if a configured language is missing, and its log names the missing language.

The language is the same for the whole installation.

## 1.6 Update

From the folder of the installation:

```bash
git pull
docker compose up -d --build
```

The database is brought to the new schema when the backend starts. Take a [backup](#17-backup-and-restore) before updating.

## 1.7 Backup and restore

All the data of an installation is in the `data/` folder next to `docker-compose.yml`:

- `data/postgres`: the database (projects, forms, users, readings, reviews);
- `data/storage`: the files (pictures of the forms, profile pictures, uploaded scans).

The two belong together: the database refers to the files by their path. Stop the services before copying them, so that both come from the same moment:

```bash
docker compose stop
# copy the data/ folder somewhere safe
docker compose start
```

`data/postgres` belongs to the user of the database container: copying it may need `sudo`. Keep `.env` with the backup: the database password is in it.

To restore, stop the services, put the copied `data/` folder back in place of the current one, and start them again with `docker compose up -d`.

## 1.8 Database access

The database is not published on the host: only the backend reaches it. For a quick look, this needs no published port (use your `POSTGRES_USER` and `POSTGRES_DB`):

```bash
docker compose exec db psql -U formscanner formscanner
```

To reach it from the host, for example with a database client, create a file `docker-compose.override.yml` next to `docker-compose.yml`:

```yaml
services:
  db:
    ports:
      - "127.0.0.1:5432:5432"
```

and run `docker compose up -d`. The database then answers on port 5432 of the host itself, not from other machines.

## 1.9 Configuration

The main variables of `.env`. After a change, run `docker compose up -d` (with `--build` for `VITE_API_BASE_URL`).

| Variable | Default in `.env.example` | Description |
|---|---|---|
| `FRONTEND_PORT` | `8080` | Port of the web application |
| `BACKEND_PORT` | `8000` | Port of the API |
| `VITE_API_BASE_URL` | `http://localhost:8000/api/v1` | Address of the API as the browser sees it ([1.4](#14-access-from-another-machine)); needs a rebuild |
| `CORS_ORIGINS` | `http://localhost:8080,http://127.0.0.1:8080` | Addresses the web application is opened from |
| `JWT_SECRET` | — | Secret that signs the sessions: change it |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `480` | How long a session lasts before the user signs in again |
| `INITIAL_ADMIN_EMAIL`, `INITIAL_ADMIN_PASSWORD` | `admin@example.com`, `ChangeMe!` | First administrator |
| `FORCE_RESET_ADMIN_PASSWORD` | `false` | Set it to `true` for one start to reset the administrator's password to `INITIAL_ADMIN_PASSWORD`, then set it back to `false` |
| `POSTGRES_PASSWORD`, `DATABASE_URL` | — | Database password, repeated in the connection string |
| `OCR_LANGUAGE` | `eng` | Language of the OCR ([1.5](#15-ocr-language)) |
| `MAX_SUBMISSION_PDF_BYTES` | `52428800` (50 MiB) | Largest PDF of scanned sheets that can be uploaded |
| `TZ` | `UTC` | Time zone of the containers. Times in the exports are always in UTC |

**The administrator account.** At every start the backend makes sure the account of `INITIAL_ADMIN_EMAIL` exists, is enabled and has the administrator role. If you lose its password, set `FORCE_RESET_ADMIN_PASSWORD=true`, run `docker compose up -d`, sign in with `INITIAL_ADMIN_PASSWORD`, then set the variable back to `false` and run `docker compose up -d` again: while it is `true` the password is reset at every start.

Every variable, with its default, is listed in the [configuration reference](../specification/architecture.md#8-configuration-reference).

## 1.10 If it does not start

- `docker compose ps` shows the state of the three services. The backend is `healthy` once it answers.
- `docker compose logs backend` shows the messages of the backend: a wrong database password, a missing OCR language, a database that cannot be reached.
- **The page opens but sign-in says "Connection error — is the backend reachable?":** the browser cannot reach the API. Check `VITE_API_BASE_URL` and `CORS_ORIGINS` ([1.4](#14-access-from-another-machine)), and that `BACKEND_PORT` is reachable.
- **The database password was changed after the first start:** the `POSTGRES_*` variables only take effect when the database is created, at the first start. Put back the old password, or change it inside the database.
- **A container of another installation was replaced:** two Compose projects with the same name ([1.2](#12-install)).
