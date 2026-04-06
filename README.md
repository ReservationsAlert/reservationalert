# ReservationAlert.ai

Monitor restaurant, campground, and any reservation website — get notified the instant a spot opens up.

## Quick Start (Local)

```bash
python3 server.py
```

Open http://localhost:8080. No dependencies required — runs on Python 3.10+ standard library only.

## Deploy to Railway (Recommended)

Railway gives you a free hosted app with zero DevOps. Here's how:

### 1. Push to GitHub

```bash
# In this folder:
git init
git add .
git commit -m "Initial commit — ReservationAlert.ai MVP"
git branch -M main
gh repo create reservationalert --public --source=. --push
```

### 2. Deploy on Railway

1. Go to [railway.app](https://railway.app) and sign in with GitHub
2. Click **"New Project"** → **"Deploy from GitHub Repo"**
3. Select your `reservationalert` repo
4. Railway auto-detects the Dockerfile and deploys

### 3. Set Environment Variables

In Railway dashboard → your service → **Variables** tab, add:

| Variable | Value |
|----------|-------|
| `PORT` | `8080` |
| `CHECK_INTERVAL` | `300` |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your email |
| `SMTP_PASS` | your app password |
| `FROM_EMAIL` | `alerts@reservationalert.ai` |

### 4. Add Persistent Storage (for the database)

In Railway dashboard → your service → **Volumes** tab:
- Click "Add Volume"
- Mount path: `/app/data`

This keeps your SQLite database alive across deploys.

## Alternative: Deploy to Render

1. Push to GitHub (same steps as above)
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your GitHub repo
4. Set build command: (leave blank)
5. Set start command: `python3 server.py`
6. Add environment variables (same as Railway table above)

## Project Structure

```
reservationalert/
├── server.py          # Backend API + monitoring engine + email notifications
├── static/
│   └── index.html     # Frontend dashboard (single-page app)
├── Dockerfile         # Container config for cloud deployment
├── Procfile           # For Render/Heroku-style platforms
├── .env.example       # Environment variable template
└── .gitignore
```

## How It Works

1. **Create a Watch** — Enter a reservation URL, your desired date, and email
2. **Monitoring Engine** — Checks watched pages every 5 minutes for availability signals
3. **Smart Detection** — Scans for keywords like "available", "book now" while filtering out "fully booked", "no availability"
4. **Instant Alerts** — Sends a formatted HTML email with a direct booking link the moment a spot opens

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watches` | List all watches |
| POST | `/api/watches` | Create a new watch |
| PUT | `/api/watches/:id` | Update a watch |
| DELETE | `/api/watches/:id` | Delete a watch |
| POST | `/api/watches/:id/check` | Trigger an immediate check |
| GET | `/api/watches/:id/alerts` | Get alerts for a watch |
| GET | `/api/watches/:id/logs` | Get check history |
| GET | `/api/stats` | Dashboard stats |
| GET | `/api/alerts` | All alerts |
| GET | `/api/health` | Health check |

## Email Setup (Gmail)

1. Go to https://myaccount.google.com/apppasswords
2. Generate an app password for "Mail"
3. Use your Gmail address as `SMTP_USER` and the generated password as `SMTP_PASS`
