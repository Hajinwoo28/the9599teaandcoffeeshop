# Deploy 9599 Tea & Coffee to Vercel

## Your Project Folder Structure

Make sure your files are arranged exactly like this:

```
9599-system/
├── api/
│   └── index.py          ← Vercel entrypoint (new)
├── static/
│   └── images/
│       └── 9599.jpg      ← Your shop logo
├── app.py                ← Main Flask app
├── requirements.txt      ← Python dependencies (new)
└── vercel.json           ← Vercel config (new)
```

---

## Step-by-Step Deployment

### Step 1 — Install Vercel CLI (optional but helpful)
```
npm install -g vercel
```
Or just use the Vercel website — no CLI needed.

### Step 2 — Push your files to GitHub

1. Go to **https://github.com** → New Repository
2. Name it `9599-system` (or anything you like)
3. Upload ALL files including:
   - `app.py`
   - `vercel.json`
   - `requirements.txt`
   - `api/index.py`
   - `static/images/9599.jpg`

### Step 3 — Connect to Vercel

1. Go to **https://vercel.com** → Sign up / Log in
2. Click **"Add New Project"**
3. Click **"Import Git Repository"**
4. Select your `9599-system` GitHub repo
5. Click **"Import"**

### Step 4 — Set Environment Variables

Before clicking Deploy, click **"Environment Variables"** and add:

| Name | Value |
|---|---|
| `SECRET_KEY` | any long random string e.g. `9599-secret-key-abc123xyz` |
| `ADMIN_PIN` | your PIN number e.g. `12345` |
| `DATABASE_URL` | your PostgreSQL URL (see Step 5) |
| `RENDER` | `true` |

### Step 5 — Add a Database (PostgreSQL)

Vercel does NOT include a database. Use one of these free options:

**Option A — Neon (Recommended, free)**
1. Go to **https://neon.tech** → Sign up free
2. Create a new project → copy the **Connection String**
3. Paste it as the `DATABASE_URL` environment variable in Vercel

**Option B — Supabase (free)**
1. Go to **https://supabase.com** → New project
2. Go to Settings → Database → copy **Connection String (URI)**
3. Replace `[YOUR-PASSWORD]` with your project password
4. Paste as `DATABASE_URL` in Vercel

### Step 6 — Deploy

1. Click **"Deploy"**
2. Wait 1–2 minutes
3. Your site will be live at:
   ```
   https://your-project-name.vercel.app
   ```

### Step 7 — Generate Your Customer Link

1. Go to `https://your-project-name.vercel.app/login`
2. Enter your admin PIN
3. Go to **Settings → Store Link & Schedule**
4. Enter your PIN → click **Generate Permanent Link**
5. Copy the link and share it with customers

---

## Important Notes

### ⚠️ Vercel Limitations
- Vercel is **serverless** — each request spins up fresh
- Sessions work via cookies (already configured in app.py)
- File uploads are NOT persistent — always use PostgreSQL for data
- Free plan has 100GB bandwidth/month and 100 serverless function invocations/day

### ⚠️ Static Files (Logo)
Vercel serves static files automatically from the `/static` folder.
Make sure `9599.jpg` is inside `static/images/`.

### ✅ What works on Vercel
- Full POS system
- Admin dashboard
- Order management
- Inventory tracking
- Receipts
- Backup & restore
- Google Login
- Store schedule (open/close)

---

## Troubleshooting

| Error | Fix |
|---|---|
| `No flask entrypoint found` | Make sure `api/index.py` and `vercel.json` are in your repo |
| `Module not found: app` | Make sure `app.py` is in the ROOT folder, not inside `api/` |
| `Database error` | Check `DATABASE_URL` is set correctly in Vercel environment variables |
| `500 Internal Server Error` | Check Vercel logs: Dashboard → Your Project → Deployments → View Logs |
| Sessions not working | Make sure `SECRET_KEY` is set in environment variables |



git commit --allow-empty -m "Trigger redeploy"
git push

git add app.py
git commit -m "Fix merge conflict in app.py"
git push

api key: re_6iRt7XVu_NKgv4tzBJyWaxCuUeUbiyFTZ

git commit --allow-empty -m "Enable email verification"
git push

git add .
git commit -m "Fix duplicate app.run"
git push

git add app.py
git commit -m "Fix duplicate app.run from merge conflict"
git push

# Replace a specific HTML template
git checkout origin/main -- templates/index.html

# Replace a CSS file
git checkout origin/main -- static/style.css

# Replace a config file
git checkout origin/main -- requirements.txt

# Replace a file inside a folder
git checkout origin/main -- static/leaflet/leaflet.min.js

git checkout origin/main -- app.py

git add .
git commit -m "Fix duplicate app.run"
git push

git commit
git push

git add .
git commit -m ".env"
git push

lwit58
kzu21s
85ga6v
xu1pq6
8ds6pq
qan887
mvklgt
z09rjt
4f1cxa
m62a1p
5ouvdo
uy6zqd