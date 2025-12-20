# 🚀 Deployment Guide - Render.com (100% Free)

## Step 1: Prepare Your Files

Create a new folder with these 4 files:

1. **app.py** - Your main application (provided above)
2. **requirements.txt** - Python dependencies (provided above)
3. **Procfile** - Tells Render how to run the app (provided above)
4. **all-listed-companies.csv** - Your company database CSV file

---

## Step 2: Upload CSV to GitHub (For Public Access)

### Option A: Create New Repository
1. Go to [github.com](https://github.com) and sign in
2. Click **"New Repository"**
3. Name it: `financial-archiver`
4. Keep it **Public**
5. Click **"Create Repository"**

### Upload CSV File:
1. Click **"Add File"** → **"Upload Files"**
2. Drag your `all-listed-companies.csv` file
3. Click **"Commit changes"**

### Get CSV URL:
1. Click on your uploaded CSV file
2. Click the **"Raw"** button
3. Copy the URL (it looks like this):
   ```
   https://raw.githubusercontent.com/YOUR_USERNAME/financial-archiver/main/all-listed-companies.csv
   ```

### Update app.py:
Open `app.py` and replace line 20:
```python
CSV_URL = "https://raw.githubusercontent.com/YOUR_USERNAME/financial-archiver/main/all-listed-companies.csv"
```
Replace `YOUR_USERNAME` with your actual GitHub username.

---

## Step 3: Deploy to Render

### A. Create Render Account
1. Go to [render.com](https://render.com)
2. Click **"Get Started"**
3. Sign up with **GitHub** (easiest option)

### B. Connect GitHub Repository
1. After signing up, you'll be on the Render dashboard
2. Click **"New +"** button (top right)
3. Select **"Web Service"**
4. Click **"Connect account"** to link your GitHub
5. Find your `financial-archiver` repository
6. Click **"Connect"**

### C. Configure Your Service

Fill in these settings:

| Field | Value |
|-------|-------|
| **Name** | `financial-archiver` (or any name you like) |
| **Region** | Choose closest to you (e.g., Singapore) |
| **Branch** | `main` |
| **Root Directory** | Leave blank |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn app:app --timeout 120 --workers 2` |

### D. Choose Free Plan
1. Scroll down to **"Instance Type"**
2. Select **"Free"** ($0/month)

### E. Deploy!
1. Click **"Create Web Service"**
2. Wait 3-5 minutes for deployment
3. You'll see build logs - wait until you see: ✅ **"Live"**

---

## Step 4: Get Your Live URL

Once deployed, you'll get a URL like:
```
https://financial-archiver-xyz.onrender.com
```

**Share this URL with your clients!**

---

## 📋 What Each File Does

### app.py
- Main Flask application
- Handles web scraping from Screener.in
- Creates ZIP downloads
- **Modified for cloud**: Uses `/tmp` storage, loads CSV from URL

### requirements.txt
- Lists all Python packages needed
- Render installs these automatically

### Procfile
- Tells Render how to start your app
- Uses Gunicorn (production web server)
- Sets timeout to 120 seconds for large downloads

---

## ⚠️ Important Notes

### Free Tier Limitations:
- ✅ **Unlimited bandwidth** (no traffic limits!)
- ✅ **Auto-SSL certificate** (HTTPS enabled)
- ⚠️ **Sleeps after 15 minutes of inactivity** (wakes up in ~30 seconds on next visit)
- ⚠️ **Files in /tmp are temporary** (deleted after ~24 hours)
- ⚠️ **512 MB RAM limit** (good for ~50 concurrent downloads)

### How Download Works:
1. User searches company → Files download to `/tmp` on server
2. Files are zipped in memory
3. User clicks "Download ZIP" → ZIP sent to their browser
4. Files deleted from server after ~24 hours (automatic cleanup)

### To Keep Server Always Awake (Optional):
Use a free service like [UptimeRobot](https://uptimerobot.com):
1. Sign up free
2. Add your Render URL
3. Set to ping every 5 minutes
4. Server never sleeps!

---

## 🔧 Updating Your App

After deployment, if you need to make changes:

1. Edit files on GitHub (or push changes via Git)
2. Render **auto-deploys** on every commit
3. Wait 2-3 minutes for redeployment
4. Changes are live!

---

## 🐛 Troubleshooting

### "CSV error" on search:
- Check your CSV_URL is correct (line 20 in app.py)
- Make sure GitHub repo is **Public**
- Test URL in browser - should download CSV

### App is slow:
- First request after sleep takes 30-60 seconds
- Use UptimeRobot to keep it awake

### "Build failed":
- Check `requirements.txt` has no typos
- Check Render logs for specific error

### Downloads not working:
- Check browser console for JavaScript errors
- Ensure HTTPS is enabled (Render does this automatically)

---

## 📊 Monitoring

View logs in Render dashboard:
1. Go to your service
2. Click **"Logs"** tab
3. See real-time server activity

---

## 💡 Next Steps (Optional Upgrades)

### Add Authentication:
Protect your app with password (Flask-Login)

### Database Storage:
Replace CSV with PostgreSQL (Render offers free 90-day DB)

### Email Notifications:
Send download links via email (SendGrid free tier)

### Custom Domain:
Point your own domain to Render (e.g., archive.yourdomain.com)

---

## ✅ Deployment Checklist

- [ ] Created GitHub repository
- [ ] Uploaded CSV file to GitHub
- [ ] Got raw CSV URL
- [ ] Updated CSV_URL in app.py
- [ ] Uploaded all 4 files to GitHub
- [ ] Created Render account
- [ ] Connected GitHub repo
- [ ] Configured web service
- [ ] Selected FREE plan
- [ ] Deployed successfully
- [ ] Tested with sample company
- [ ] Shared URL with client

---

**Your app is now LIVE! 🎉**

Share your Render URL with clients and they can start downloading financial documents immediately!