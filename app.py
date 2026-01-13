import os
import sys
import time
import re
import logging
import pandas as pd
import zipfile
import io
from pathlib import Path
from urllib.parse import urljoin, quote, urlparse
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
import warnings
import threading
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template_string, request, jsonify, Response, send_file

warnings.filterwarnings("ignore")

CSV_URL = "https://raw.githubusercontent.com/vmik559-hue/financial-archiver/refs/heads/main/all-listed-companies.csv"
DOCUMENTS_ROOT = Path('/tmp') / "Financial_Archive"
SCREENER_DOMAIN = "https://www.screener.in"
DOCUMENTS_ROOT.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(message)s')

log_queue = queue.Queue()
MAX_WORKERS = 3

class ScreenerUnifiedFetcher:
    def __init__(self):
        self.headers = {
            'authority': 'www.screener.in',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
        self.downloaded_files = []
        self.company_root = None

    def sanitize(self, name):
        return re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()

    def extract_metadata(self, element):
        """Extract year and month from element - checks multiple places"""
        # Try to get text from parent li first, then link itself
        row = element.find_parent('li')
        search_text = row.get_text(" ", strip=True) if row else element.get_text(" ", strip=True)
        
        # Also check the href attribute for year
        href = element.get('href', '')
        combined_text = search_text + " " + href
        
        # Look for 4-digit year (2000-2099)
        year_matches = re.findall(r'\b(20\d{2})\b', combined_text)
        year = year_matches[0] if year_matches else "Unknown_Year"
        
        # Look for month names
        month_list = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
        month_name = "General"
        
        for m in month_list:
            if re.search(rf'\b{m}\b', combined_text, re.I):
                month_name = m.capitalize()
                break
        
        return year, month_name

    def download_file(self, url, save_path):
        try:
            full_url = urljoin(SCREENER_DOMAIN, url)
            headers = self.headers.copy()
            domain = urlparse(full_url).netloc
            if 'bseindia' in domain: headers['Referer'] = 'https://www.bseindia.com/'
            elif 'nseindia' in domain: headers['Referer'] = 'https://www.nseindia.com/'
            else: headers['Referer'] = SCREENER_DOMAIN

            r = cffi_requests.get(full_url, headers=headers, impersonate="chrome120", timeout=60, allow_redirects=True)
            
            if r.status_code == 200 and len(r.content) > 1000:
                save_path.parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, 'wb') as f:
                    f.write(r.content)
                self.downloaded_files.append(str(save_path))
                return True
            return False
        except Exception as e:
            return False

    def process_company(self, symbol, name, start_year, end_year, download_type='all'):
        self.downloaded_files = []
        symbol_upper = str(symbol).upper()
        log_queue.put(f"STATUS|Fetching data for {name}...")
        url = f"{SCREENER_DOMAIN}/company/{quote(symbol)}/"
        
        # Parse download_type to support multiple comma-separated values
        selected_types = [t.strip() for t in download_type.split(',')]
        
        try:
            resp = cffi_requests.get(url, headers=self.headers, impersonate="chrome120", timeout=30)
            soup = BeautifulSoup(resp.content, 'html.parser')
        except Exception as e:
            log_queue.put(f"ERROR|Connection failed: {str(e)}")
            return None

        comp_root = DOCUMENTS_ROOT / self.sanitize(name)
        self.company_root = str(comp_root)
        download_tasks = []
        
        # ===== ANNUAL REPORTS - FIXED LOGIC =====
        if 'all' in selected_types or 'annual_reports' in selected_types:
            ar_section = soup.find('div', id='annual-reports')
            if not ar_section:
                header = soup.find(lambda tag: tag.name in ['h2', 'h3'] and 'annual report' in tag.text.lower())
                if header: ar_section = header.find_next('div')

            if ar_section:
                ar_items = ar_section.find_all('li')
                for li in ar_items:
                    link = li.find('a', href=True)
                    if not link:
                        continue
                        
                    full_row_text = li.get_text(" ", strip=True)
                    year_match = re.search(r'\b(20\d{2})\b', full_row_text)
                    
                    if not year_match:
                        continue
                        
                    year = year_match.group(1)
                    year_int = int(year)
                    
                    # Skip if outside year range
                    if year_int < start_year or year_int > end_year:
                        continue
                    
                    save_dir = comp_root / "Annual_Reports" / year
                    file_path = save_dir / f"Annual_Report_{year}.pdf"
                    download_tasks.append(('Annual Report', year, link['href'], file_path))

        # ===== PPT & TRANSCRIPTS - FIXED LOGIC =====
        if 'all' in selected_types or 'ppt' in selected_types or 'transcript' in selected_types:
            all_links = soup.find_all('a', href=True)
            seen_urls = set()

            for link in all_links:
                link_text = link.get_text(strip=True).lower()
                href = link['href']
                if href in seen_urls or not href.startswith('http') or "consolidated" in href: 
                    continue

                cat = None
                if "transcript" in link_text and ('all' in selected_types or 'transcript' in selected_types): 
                    cat = "Transcript"
                elif link_text == "ppt" and ('all' in selected_types or 'ppt' in selected_types): 
                    cat = "PPT"
                
                if cat:
                    year, month = self.extract_metadata(link)
                    
                    # FIXED: Skip if year is unknown OR outside range
                    if year == "Unknown_Year":
                        continue
                        
                    year_int = int(year)
                    if year_int < start_year or year_int > end_year:
                        continue
                    
                    seen_urls.add(href)
                    save_dir = comp_root / cat / year
                    
                    fname = f"{symbol_upper}_{month}_{year}_{cat}.pdf"
                    file_path = save_dir / fname
                    
                    counter = 1
                    while file_path.exists():
                        file_path = save_dir / f"{symbol_upper}_{month}_{year}_{cat}_{counter}.pdf"
                        counter += 1
                    
                    download_tasks.append((cat, f"{year}-{month}", href, file_path))

        total_files = len(download_tasks)
        
        if total_files == 0:
            log_queue.put("STATUS|No files found in the specified year range")
            log_queue.put("COMPLETE|0|0|")
            return None

        log_queue.put(f"TOTAL|{total_files}")
        
        completed = 0
        start_time = time.time()
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_task = {
                executor.submit(self.download_file, task[2], task[3]): task 
                for task in download_tasks
            }
            
            for future in as_completed(future_to_task):
                completed += 1
                elapsed = time.time() - start_time
                avg_time = elapsed / completed
                remaining = total_files - completed
                eta_seconds = int(avg_time * remaining)
                
                log_queue.put(f"PROGRESS|{completed}|{total_files}|{eta_seconds}")
                time.sleep(0.05)
        
        log_queue.put(f"COMPLETE|{completed}|{total_files}|{self.company_root}")
        return self.company_root

app = Flask(__name__)
download_sessions = {}

HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>FinArch - Premium Financial Data Archiver</title>
<link href="https://fonts.googleapis.com" rel="preconnect"/>
<link crossorigin="" href="https://fonts.gstatic.com" rel="preconnect"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Playfair+Display:ital,wght@0,400;0,600;0,700;1,400&display=swap" rel="stylesheet"/>
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght,FILL@100..700,0..1&display=swap" rel="stylesheet"/>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--primary:#3b82f6;--accent:#ca8a04;--bg-dark:#000000;--surface-dark:#0a0a0a;--surface-card:#121212;--text-main:#cbd5e1;--text-muted:#64748b}
body{font-family:'Inter',sans-serif;background:var(--bg-dark);color:var(--text-main);min-height:100vh;overflow-x:hidden;position:relative}
.bg-grid{position:fixed;inset:0;z-index:0;background-image:url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%23334155' fill-opacity='0.08'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E");pointer-events:none}
.bg-glow{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none}
.bg-glow::before{content:'';position:absolute;top:-20%;left:-10%;width:50vw;height:50vw;background:rgba(30,58,138,0.15);border-radius:50%;filter:blur(120px)}
.bg-glow::after{content:'';position:absolute;top:40%;right:-10%;width:40vw;height:40vw;background:rgba(120,53,15,0.1);border-radius:50%;filter:blur(120px)}
nav{position:relative;z-index:50;width:100%;max-width:1280px;margin:0 auto;padding:2rem 1.5rem;display:flex;justify-content:space-between;align-items:center}
.logo-wrap{display:flex;align-items:center;gap:0.75rem}
.logo-icon{width:40px;height:40px;background:linear-gradient(135deg,#fff,#94a3b8);border-radius:10px;display:flex;align-items:center;justify-content:center;box-shadow:0 0 15px rgba(255,255,255,0.1)}
.logo-icon .material-symbols-outlined{color:#000;font-size:24px;font-weight:700}
.logo-text{font-family:'Playfair Display',serif;font-size:1.25rem;font-weight:700;color:#fff;letter-spacing:-0.5px}
main{position:relative;z-index:10;flex-grow:1;display:flex;flex-direction:column;align-items:center;padding:1rem 1rem 3rem}
.hero-section{text-align:center;max-width:900px;margin:0 auto 2.5rem}
.badge{display:inline-flex;align-items:center;gap:0.5rem;padding:0.35rem 0.85rem;border-radius:9999px;background:rgba(30,58,138,0.25);border:1px solid rgba(59,130,246,0.3);color:#93c5fd;font-size:0.7rem;font-weight:600;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:1rem;box-shadow:0 0 10px rgba(59,130,246,0.1)}
.badge-dot{width:6px;height:6px;border-radius:50%;background:#60a5fa;animation:pulse-dot 2s ease-in-out infinite}
@keyframes pulse-dot{0%,100%{opacity:1}50%{opacity:0.4}}
h1{font-family:'Playfair Display',serif;font-size:clamp(2.5rem,6vw,3.75rem);font-weight:700;color:#fff;letter-spacing:-0.03em;line-height:1.15;margin-bottom:1rem}
h1 .gradient-text{background:linear-gradient(90deg,#60a5fa,#a5b4fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.subtitle{font-size:1.1rem;color:var(--text-muted);max-width:600px;margin:0 auto;line-height:1.7;font-weight:300}
.glass-panel{width:100%;max-width:1000px;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);background:linear-gradient(160deg,rgba(20,20,20,0.7) 0%,rgba(10,10,10,0.85) 100%);border:1px solid rgba(51,65,85,0.4);border-radius:1.25rem;padding:2rem;position:relative;overflow:hidden;box-shadow:0 25px 80px -20px rgba(0,0,0,0.6)}
.glass-panel::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(59,130,246,0.5),transparent)}
.search-row{display:flex;gap:0.75rem;margin-bottom:1.5rem;position:relative}
.search-input-wrap{flex:1;position:relative}
.search-input-wrap .material-symbols-outlined{position:absolute;left:1rem;top:50%;transform:translateY(-50%);color:var(--text-muted);font-size:22px;transition:color 0.3s}
.search-input-wrap:focus-within .material-symbols-outlined{color:var(--primary)}
#searchInput{width:100%;padding:1.15rem 1rem 1.15rem 3rem;background:var(--surface-card);border:1px solid rgba(51,65,85,0.5);border-radius:0.75rem;color:#fff;font-size:1rem;font-family:'Inter',sans-serif;transition:all 0.3s;box-shadow:inset 0 2px 4px rgba(0,0,0,0.2)}
#searchInput::placeholder{color:var(--text-muted)}
#searchInput:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px rgba(59,130,246,0.15),inset 0 2px 4px rgba(0,0,0,0.2)}
.search-btn{padding:0 2rem;background:linear-gradient(135deg,#2563eb,#1d4ed8);color:#fff;border:none;border-radius:0.75rem;font-size:0.95rem;font-weight:600;cursor:pointer;display:flex;align-items:center;gap:0.5rem;transition:all 0.3s;box-shadow:0 4px 15px rgba(37,99,235,0.3)}
.search-btn:hover{background:linear-gradient(135deg,#3b82f6,#2563eb);transform:translateY(-2px);box-shadow:0 8px 25px rgba(37,99,235,0.4)}
.controls-grid{display:grid;grid-template-columns:1fr 1.5fr;gap:1.25rem}
@media(max-width:768px){.controls-grid{grid-template-columns:1fr}}
.control-box{background:rgba(18,18,18,0.6);border:1px solid rgba(51,65,85,0.4);border-radius:0.85rem;padding:1.25rem}
.control-label{display:block;font-size:0.7rem;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:0.12em;margin-bottom:0.85rem}
.year-range{display:flex;align-items:center;gap:0.75rem}
.year-select-wrap{flex:1;position:relative}
.year-select-wrap .material-symbols-outlined{position:absolute;left:0.75rem;top:50%;transform:translateY(-50%);color:var(--text-muted);font-size:16px;pointer-events:none}
.year-select{width:100%;padding:0.7rem 0.75rem 0.7rem 2.25rem;background:var(--surface-card);border:1px solid rgba(51,65,85,0.5);border-radius:0.5rem;color:var(--text-main);font-size:0.9rem;font-family:'Inter',sans-serif;cursor:pointer;appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%2394a3b8' d='M6 8L1 3h10z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 0.75rem center}
.year-select:focus{outline:none;border-color:var(--primary)}
.year-arrow{color:var(--text-muted)}
.doc-types{display:flex;flex-wrap:wrap;gap:0.65rem}
.file-type-btn{padding:0.65rem 1rem;background:var(--surface-card);border:1px solid rgba(51,65,85,0.5);border-radius:0.5rem;color:var(--text-muted);font-size:0.85rem;font-weight:500;cursor:pointer;display:flex;align-items:center;gap:0.5rem;transition:all 0.25s;font-family:'Inter',sans-serif}
.file-type-btn .material-symbols-outlined{font-size:18px}
.file-type-btn:hover{background:rgba(30,41,59,0.5);border-color:rgba(71,85,105,0.6);color:var(--text-main)}
.file-type-btn.active{background:rgba(30,58,138,0.25);border-color:rgba(59,130,246,0.5);color:#93c5fd}
#recommendations{display:none;margin-top:1.5rem;background:rgba(18,18,18,0.6);border:1px solid rgba(51,65,85,0.4);border-radius:0.85rem;padding:1rem;max-height:300px;overflow-y:auto}
#recommendations.show{display:block;animation:fadeIn 0.3s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:translateY(0)}}
.rec-item{padding:1rem 1.25rem;margin:0.5rem 0;background:var(--surface-card);border:1px solid transparent;border-radius:0.65rem;cursor:pointer;transition:all 0.25s;font-weight:500}
.rec-item:hover{border-color:rgba(59,130,246,0.4);background:rgba(30,41,59,0.4);transform:translateX(5px)}
.rec-item strong{color:#fff}
.rec-item span{color:var(--text-muted);font-size:0.85rem}
#status{text-align:center;margin-top:1.5rem;padding:1rem;font-weight:500;font-size:1rem;color:var(--text-main);background:rgba(30,41,59,0.3);border-radius:0.65rem;min-height:60px;display:flex;align-items:center;justify-content:center}
#status:empty{display:none}
#progressContainer{display:none;margin-top:1.5rem;background:rgba(18,18,18,0.6);border:1px solid rgba(51,65,85,0.4);border-radius:0.85rem;padding:1.5rem}
#progressContainer.show{display:block;animation:fadeIn 0.4s ease}
.progress-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;margin-bottom:1.25rem}
.stat-box{background:var(--surface-card);border:1px solid rgba(51,65,85,0.4);border-radius:0.65rem;padding:1.25rem;text-align:center;transition:all 0.3s}
.stat-box:hover{border-color:rgba(59,130,246,0.3);transform:translateY(-3px)}
.stat-icon{font-size:1.5rem;margin-bottom:0.5rem}
.stat-number{font-size:2rem;font-weight:700;background:linear-gradient(135deg,#60a5fa,#a5b4fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-label{font-size:0.7rem;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.08em;margin-top:0.35rem}
.progress-bar-wrap{background:rgba(30,41,59,0.5);height:2.5rem;border-radius:1.25rem;overflow:hidden;position:relative;box-shadow:inset 0 2px 4px rgba(0,0,0,0.3)}
#progressBar{height:100%;background:linear-gradient(90deg,#2563eb,#7c3aed,#c026d3);transition:width 0.4s cubic-bezier(0.4,0,0.2,1);display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:0.9rem;position:relative;min-width:3rem}
#progressBar::after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,0.2),transparent);animation:shimmer 2s infinite}
@keyframes shimmer{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}
#downloadBtn{display:none;margin:1.5rem auto 0;padding:1rem 2.5rem;background:linear-gradient(135deg,#059669,#10b981);color:#fff;border:none;border-radius:0.75rem;font-size:1.1rem;font-weight:600;cursor:pointer;box-shadow:0 8px 30px rgba(16,185,129,0.3);transition:all 0.3s;font-family:'Inter',sans-serif}
#downloadBtn.show{display:flex;align-items:center;gap:0.5rem;animation:popIn 0.5s cubic-bezier(0.34,1.56,0.64,1)}
@keyframes popIn{from{transform:scale(0.8);opacity:0}to{transform:scale(1);opacity:1}}
#downloadBtn:hover{background:linear-gradient(135deg,#10b981,#34d399);transform:translateY(-3px);box-shadow:0 12px 40px rgba(16,185,129,0.4)}
.loading{display:inline-block;width:20px;height:20px;border:3px solid rgba(255,255,255,0.3);border-top-color:#fff;border-radius:50%;animation:spin 0.8s linear infinite;margin-right:0.75rem}
@keyframes spin{to{transform:rotate(360deg)}}
.features-section{width:100%;max-width:1000px;margin-top:3rem}
.features-title{text-align:center;font-size:0.75rem;font-weight:600;letter-spacing:0.2em;color:var(--text-muted);text-transform:uppercase;margin-bottom:2rem}
.features-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1.25rem}
@media(max-width:768px){.features-grid{grid-template-columns:1fr}}
.feature-card{position:relative;background:var(--surface-card);border:1px solid rgba(51,65,85,0.4);border-radius:1rem;padding:1.5rem;overflow:hidden;transition:all 0.35s}
.feature-card:hover{transform:translateY(-5px);border-color:rgba(59,130,246,0.3);box-shadow:0 15px 40px -15px rgba(59,130,246,0.2)}
.feature-card .bg-icon{position:absolute;top:0;right:0;font-size:5rem;color:rgba(255,255,255,0.03);pointer-events:none}
.feature-icon{width:48px;height:48px;border-radius:0.75rem;display:flex;align-items:center;justify-content:center;margin-bottom:1rem;transition:background 0.3s}
.feature-icon.blue{background:rgba(30,58,138,0.3)}
.feature-icon.amber{background:rgba(120,53,15,0.25)}
.feature-icon.purple{background:rgba(88,28,135,0.3)}
.feature-card:hover .feature-icon.blue{background:#2563eb}
.feature-card:hover .feature-icon.amber{background:#d97706}
.feature-card:hover .feature-icon.purple{background:#7c3aed}
.feature-icon .material-symbols-outlined{font-size:24px;transition:color 0.3s}
.feature-icon.blue .material-symbols-outlined{color:#60a5fa}
.feature-icon.amber .material-symbols-outlined{color:#fbbf24}
.feature-icon.purple .material-symbols-outlined{color:#a78bfa}
.feature-card:hover .feature-icon .material-symbols-outlined{color:#fff}
.feature-card h3{font-family:'Playfair Display',serif;font-size:1.15rem;font-weight:600;color:#fff;margin-bottom:0.5rem;transition:color 0.3s}
.feature-card:hover h3{color:#93c5fd}
.feature-card p{font-size:0.875rem;color:var(--text-muted);line-height:1.6}
footer{position:relative;z-index:10;width:100%;padding:1.5rem;margin-top:auto;border-top:1px solid rgba(51,65,85,0.3);background:rgba(0,0,0,0.6);backdrop-filter:blur(8px)}
.footer-inner{max-width:1280px;margin:0 auto;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:1rem;font-size:0.75rem;color:var(--text-muted)}
.footer-links{display:flex;gap:1.5rem}
.footer-links a{color:var(--text-muted);text-decoration:none;transition:color 0.25s}
.footer-links a:hover{color:#fff}
</style>
</head>
<body>
<div class="bg-grid"></div>
<div class="bg-glow"></div>

<nav>
    <div class="logo-wrap">
        <div class="logo-icon"><span class="material-symbols-outlined">account_balance</span></div>
        <span class="logo-text">FINARCH</span>
    </div>
</nav>

<main>
    <section class="hero-section">
        <div class="badge"><span class="badge-dot"></span>v3.0 Premium Access</div>
        <h1>Financial Data <span class="gradient-text">Archiver</span></h1>
        <p class="subtitle">Securely extract Annual Reports, Investor Presentations & Transcripts.<br/>Engineered for investment banking professionals.</p>
    </section>

    <div class="glass-panel">
        <div class="search-row">
            <div class="search-input-wrap">
                <span class="material-symbols-outlined">search</span>
                <input type="text" id="searchInput" placeholder="Search Company Name, NSE or BSE Code (e.g., RELIANCE)..."/>
            </div>
            <button class="search-btn" onclick="searchCompany()">
                <span>Search</span>
                <span class="material-symbols-outlined" style="font-size:18px">arrow_forward</span>
            </button>
        </div>

        <div class="controls-grid">
            <div class="control-box">
                <label class="control-label">Fiscal Year Range</label>
                <div class="year-range">
                    <div class="year-select-wrap">
                        <span class="material-symbols-outlined">calendar_today</span>
                        <select class="year-select" id="startYear">
                            <option value="2010">2010</option>
                            <option value="2011">2011</option>
                            <option value="2012">2012</option>
                            <option value="2013">2013</option>
                            <option value="2014">2014</option>
                            <option value="2015" selected>2015</option>
                            <option value="2016">2016</option>
                            <option value="2017">2017</option>
                            <option value="2018">2018</option>
                            <option value="2019">2019</option>
                            <option value="2020">2020</option>
                            <option value="2021">2021</option>
                            <option value="2022">2022</option>
                            <option value="2023">2023</option>
                            <option value="2024">2024</option>
                            <option value="2025">2025</option>
                        </select>
                    </div>
                    <span class="material-symbols-outlined year-arrow">arrow_right_alt</span>
                    <div class="year-select-wrap">
                        <span class="material-symbols-outlined">calendar_today</span>
                        <select class="year-select" id="endYear">
                            <option value="2015">2015</option>
                            <option value="2016">2016</option>
                            <option value="2017">2017</option>
                            <option value="2018">2018</option>
                            <option value="2019">2019</option>
                            <option value="2020">2020</option>
                            <option value="2021">2021</option>
                            <option value="2022">2022</option>
                            <option value="2023">2023</option>
                            <option value="2024">2024</option>
                            <option value="2025" selected>2025</option>
                        </select>
                    </div>
                </div>
            </div>
            <div class="control-box">
                <label class="control-label">Document Types</label>
                <div class="doc-types">
                    <button class="file-type-btn active" onclick="selectFileType('all')" data-type="all">
                        <span class="material-symbols-outlined">folder</span>All Files
                    </button>
                    <button class="file-type-btn" onclick="selectFileType('annual_reports')" data-type="annual_reports">
                        <span class="material-symbols-outlined">description</span>Annual Reports
                    </button>
                    <button class="file-type-btn" onclick="selectFileType('ppt')" data-type="ppt">
                        <span class="material-symbols-outlined">pie_chart</span>Presentations
                    </button>
                    <button class="file-type-btn" onclick="selectFileType('transcript')" data-type="transcript">
                        <span class="material-symbols-outlined">mic</span>Transcripts
                    </button>
                </div>
            </div>
        </div>

        <div id="recommendations" class="recommendations"></div>
        <div id="status" class="status"></div>
        
        <button id="downloadBtn" class="download-btn" onclick="downloadZip()">
            <span class="material-symbols-outlined">download</span>Download ZIP Archive
        </button>

        <div id="progressContainer" class="progress-container">
            <div class="progress-stats">
                <div class="stat-box">
                    <div class="stat-icon">✅</div>
                    <div class="stat-number" id="completedCount">0</div>
                    <div class="stat-label">Downloaded</div>
                </div>
                <div class="stat-box">
                    <div class="stat-icon">📁</div>
                    <div class="stat-number" id="totalCount">0</div>
                    <div class="stat-label">Total Files</div>
                </div>
                <div class="stat-box">
                    <div class="stat-icon">⏱️</div>
                    <div class="stat-number" id="etaCount">0s</div>
                    <div class="stat-label">Time Left</div>
                </div>
            </div>
            <div class="progress-bar-wrap">
                <div id="progressBar" style="width:0%">0%</div>
            </div>
        </div>
    </div>

    <section class="features-section">
        <h2 class="features-title">Related Intelligence Tools</h2>
        <div class="features-grid">
            <div class="feature-card">
                <span class="material-symbols-outlined bg-icon">cloud_download</span>
                <div class="feature-icon blue"><span class="material-symbols-outlined">folder_zip</span></div>
                <h3>Files Downloader</h3>
                <p>Bulk extract filings and datasets directly to your local server.</p>
            </div>
            <div class="feature-card">
                <span class="material-symbols-outlined bg-icon">psychology</span>
                <div class="feature-icon amber"><span class="material-symbols-outlined">analytics</span></div>
                <h3>Sentimental Analysis</h3>
                <p>AI-driven insights on market mood from earnings calls.</p>
            </div>
            <div class="feature-card">
                <span class="material-symbols-outlined bg-icon">dataset</span>
                <div class="feature-icon purple"><span class="material-symbols-outlined">database</span></div>
                <h3>FADA Data</h3>
                <p>Federated Auto Dealers Association proprietary statistics.</p>
            </div>
        </div>
    </section>
</main>

<footer>
    <div class="footer-inner">
        <p>© 2024 FinArch Inc. All rights reserved.</p>
        <div class="footer-links">
            <a href="#">Privacy Policy</a>
            <a href="#">Terms of Service</a>
            <a href="#">Support</a>
        </div>
    </div>
</footer>

<script>
let selectedCompany=null;
let eventSource=null;
let sessionId=null;
let downloadType='all';

document.getElementById('searchInput').addEventListener('keypress',function(e){
    if(e.key==='Enter')searchCompany()
});

function selectFileType(type){
    const btn=document.querySelector(`.file-type-btn[data-type="${type}"]`);
    const allBtn=document.querySelector(`.file-type-btn[data-type="all"]`);
    const otherBtns=document.querySelectorAll(`.file-type-btn:not([data-type="all"])`);
    
    if(type==='all'){
        // If "All Files" is clicked, select only "All Files" and deselect others
        otherBtns.forEach(b=>b.classList.remove('active'));
        allBtn.classList.add('active');
        downloadType='all';
    }else{
        // Toggle the clicked button
        btn.classList.toggle('active');
        // Deselect "All Files" when any specific type is selected
        allBtn.classList.remove('active');
        
        // Collect all currently selected types (excluding "all")
        const selectedTypes=[];
        otherBtns.forEach(b=>{
            if(b.classList.contains('active')){
                selectedTypes.push(b.getAttribute('data-type'));
            }
        });
        
        // If none selected, fallback to "All Files"
        if(selectedTypes.length===0){
            allBtn.classList.add('active');
            downloadType='all';
        }else{
            // Join selected types with comma
            downloadType=selectedTypes.join(',');
        }
    }
}

async function searchCompany(){
    const query=document.getElementById('searchInput').value.trim();
    if(!query){
        alert('⚠️ Please enter a company name or code');
        return
    }
    document.getElementById('status').innerHTML='<div class="loading"></div> Searching database...';
    document.getElementById('recommendations').classList.remove('show');
    document.getElementById('progressContainer').classList.remove('show');
    const response=await fetch('/search',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({query:query})
    });
    const data=await response.json();
    if(data.error){
        document.getElementById('status').innerHTML='❌ '+data.error;
        return
    }
    if(data.matches.length===1){
        selectedCompany=data.matches[0];
        startExtraction()
    }else{
        showRecommendations(data.matches)
    }
}

function showRecommendations(matches){
    document.getElementById('status').innerHTML='✨ Found '+matches.length+' matches. Select your company:';
    const recDiv=document.getElementById('recommendations');
    recDiv.innerHTML=matches.map((m,idx)=>'<div class="rec-item" onclick="selectCompany('+idx+')"><strong>'+m.Name+'</strong><br><span>NSE: '+(m.NSE_Code||'N/A')+' | BSE: '+(m.BSE_Code||'N/A')+'</span></div>').join('');
    recDiv.classList.add('show');
    window.currentMatches=matches
}

function selectCompany(idx){
    selectedCompany=window.currentMatches[idx];
    document.getElementById('recommendations').classList.remove('show');
    startExtraction()
}

function startExtraction(){
    const startYear=parseInt(document.getElementById('startYear').value);
    const endYear=parseInt(document.getElementById('endYear').value);
    if(startYear>endYear){
        alert('⚠️ Start year must be less than or equal to end year');
        return
    }
    document.getElementById('status').innerHTML='<div class="loading"></div> Initializing extraction for <strong>'+selectedCompany.Name+'</strong>...';
    document.getElementById('progressContainer').classList.add('show');
    document.getElementById('downloadBtn').classList.remove('show');
    if(eventSource)eventSource.close();
    const url='/extract?symbol='+encodeURIComponent(selectedCompany.symbol)+'&name='+encodeURIComponent(selectedCompany.Name)+'&start_year='+startYear+'&end_year='+endYear+'&download_type='+encodeURIComponent(downloadType);
    eventSource=new EventSource(url);
    eventSource.onmessage=function(event){
        const data=event.data.split('|');
        const type=data[0];
        if(type==='STATUS'){
            document.getElementById('status').innerHTML='<div class="loading"></div> '+data[1]
        }else if(type==='TOTAL'){
            document.getElementById('totalCount').textContent=data[1]
        }else if(type==='PROGRESS'){
            const completed=parseInt(data[1]);
            const total=parseInt(data[2]);
            const eta=parseInt(data[3]);
            const percentage=Math.round((completed/total)*100);
            document.getElementById('completedCount').textContent=completed;
            document.getElementById('etaCount').textContent=eta+'s';
            document.getElementById('progressBar').style.width=percentage+'%';
            document.getElementById('progressBar').textContent=percentage+'%'
        }else if(type==='COMPLETE'){
            sessionId=data[3];
            document.getElementById('status').innerHTML='🎉 <strong>Extraction Complete!</strong> Successfully downloaded '+data[1]+'/'+data[2]+' files for <strong>'+selectedCompany.Name+'</strong>';
            document.getElementById('downloadBtn').classList.add('show');
            eventSource.close()
        }else if(type==='ERROR'){
            document.getElementById('status').innerHTML='❌ Error: '+data[1];
            eventSource.close()
        }
    }
}

function downloadZip(){
    if(sessionId){
        document.getElementById('status').innerHTML='<div class="loading"></div> Preparing your ZIP archive...';
        window.location.href='/download?session='+sessionId
    }
}
</script>
</body>
</html>'''

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/search', methods=['POST'])
def search():
    try:
        df = pd.read_csv(CSV_URL)
        df['NSE Code'] = df['NSE Code'].astype(str).str.replace('nan', '')
        df['BSE Code'] = df['BSE Code'].apply(lambda x: str(int(x)) if pd.notnull(x) and str(x).replace('.0','').isdigit() else '')
    except:
        return jsonify({'error': 'Database error'})

    query = request.json.get('query', '').strip().lower()
    
    match = df[
        (df['Name'].str.lower().str.contains(query, na=False)) | 
        (df['NSE Code'].str.lower() == query) | 
        (df['BSE Code'] == query)
    ]

    if match.empty:
        return jsonify({'error': 'No company found'})

    matches = []
    for _, row in match.head(10).iterrows():
        symbol = row['NSE Code'] if row['NSE Code'] != '' else row['BSE Code']
        matches.append({
            'Name': row['Name'],
            'NSE_Code': row['NSE Code'],
            'BSE_Code': row['BSE Code'],
            'symbol': symbol
        })

    return jsonify({'matches': matches})

@app.route('/extract')
def extract():
    symbol = request.args.get('symbol')
    name = request.args.get('name')
    start_year = int(request.args.get('start_year', 2015))
    end_year = int(request.args.get('end_year', 2025))
    download_type = request.args.get('download_type', 'all')
    session_id = f"{symbol}_{int(time.time())}"

    def generate():
        fetcher = ScreenerUnifiedFetcher()
        
        def run_extraction():
            company_root = fetcher.process_company(symbol, name, start_year, end_year, download_type)
            if company_root:
                download_sessions[session_id] = {
                    'path': company_root,
                    'timestamp': time.time()
                }
        
        thread = threading.Thread(target=run_extraction)
        thread.start()

        while thread.is_alive() or not log_queue.empty():
            try:
                log_line = log_queue.get(timeout=0.1)
                if log_line.startswith('COMPLETE'):
                    parts = log_line.split('|')
                    yield f"data: COMPLETE|{parts[1]}|{parts[2]}|{session_id}\n\n"
                else:
                    yield f"data: {log_line}\n\n"
            except queue.Empty:
                continue

    return Response(generate(), mimetype='text/event-stream')

@app.route('/download')
def download():
    session_id = request.args.get('session')
    
    if not session_id or session_id not in download_sessions:
        return "No files available", 404
    
    session_data = download_sessions[session_id]
    download_path = session_data['path']
    
    if not download_path or not Path(download_path).exists():
        return "Files not found", 404
    
    memory_file = io.BytesIO()
    
    try:
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
            root_path = Path(download_path)
            file_count = 0
            for file_path in root_path.rglob('*.pdf'):
                arcname = file_path.relative_to(root_path.parent)
                zipf.write(file_path, arcname)
                file_count += 1
            
            if file_count == 0:
                return "No PDF files found", 404
        
        memory_file.seek(0)
        company_name = Path(download_path).name
        zip_filename = f"{company_name}_Documents.zip"
        
        # Clean up old sessions (older than 5 minutes)
        current_time = time.time()
        sessions_to_delete = [
            sid for sid, data in download_sessions.items() 
            if current_time - data['timestamp'] > 300
        ]
        for sid in sessions_to_delete:
            del download_sessions[sid]
        
        return send_file(
            memory_file, 
            mimetype='application/zip', 
            as_attachment=True, 
            download_name=zip_filename
        )
    except Exception as e:
        logging.error(f"Download error: {str(e)}")
        return f"Error creating ZIP: {str(e)}", 500

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
