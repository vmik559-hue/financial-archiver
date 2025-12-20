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
        self.company_root = None  # Store the root path

    def sanitize(self, name):
        return re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()

    def extract_metadata(self, element):
        row = element.find_parent('li')
        search_text = row.get_text(" ", strip=True) if row else element.get_text(" ", strip=True)
        
        year_match = re.search(r'\b(20\d{2})\b', search_text)
        year = year_match.group(1) if year_match else "Unknown_Year"
        
        month_list = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
        month_name = "General"
        
        for m in month_list:
            if re.search(rf'\b{m}\b', search_text, re.I):
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
                self.downloaded_files.append(str(save_path))  # Store as string for serialization
                return True
            return False
        except Exception as e:
            return False

    def process_company(self, symbol, name, start_year, end_year):
        self.downloaded_files = []
        symbol_upper = str(symbol).upper()
        log_queue.put(f"STATUS|Fetching data for {name}...")
        url = f"{SCREENER_DOMAIN}/company/{quote(symbol)}/"
        
        try:
            resp = cffi_requests.get(url, headers=self.headers, impersonate="chrome120", timeout=30)
            soup = BeautifulSoup(resp.content, 'html.parser')
        except Exception as e:
            log_queue.put(f"ERROR|Connection failed: {str(e)}")
            return None

        comp_root = DOCUMENTS_ROOT / self.sanitize(name)
        self.company_root = str(comp_root)  # Store root path
        download_tasks = []
        
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

        all_links = soup.find_all('a', href=True)
        seen_urls = set()

        for link in all_links:
            link_text = link.get_text(strip=True).lower()
            href = link['href']
            if href in seen_urls or not href.startswith('http') or "consolidated" in href: 
                continue

            cat = None
            if "transcript" in link_text: cat = "Transcript"
            elif link_text == "ppt": cat = "PPT"
            
            if cat:
                year, month = self.extract_metadata(link)
                
                # Skip if year couldn't be extracted or is outside range
                if year == "Unknown_Year":
                    continue
                    
                year_int = int(year)
                if year_int < start_year or year_int > end_year:
                    continue
                
                seen_urls.add(href)
                save_dir = comp_root / year / cat
                
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
download_sessions = {}  # Changed from download_paths to download_sessions

HTML_TEMPLATE = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Financial Document Archiver</title><link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700;800&display=swap" rel="stylesheet"><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:'Poppins',sans-serif;background:linear-gradient(135deg,#667eea 0%,#764ba2 50%,#f093fb 100%);min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px;animation:gradientShift 15s ease infinite;background-size:400% 400%;overflow-x:hidden}@keyframes gradientShift{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}.floating-shapes{position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0}.shape{position:absolute;border-radius:50%;background:rgba(255,255,255,0.1);animation:float 20s infinite}.shape:nth-child(1){width:80px;height:80px;left:10%;top:20%;animation-delay:0s}.shape:nth-child(2){width:120px;height:120px;right:10%;top:60%;animation-delay:2s}.shape:nth-child(3){width:60px;height:60px;left:70%;top:10%;animation-delay:4s}.shape:nth-child(4){width:100px;height:100px;right:30%;bottom:20%;animation-delay:6s}@keyframes float{0%,100%{transform:translateY(0) rotate(0deg)}50%{transform:translateY(-30px) rotate(180deg)}}.container{background:rgba(255,255,255,0.98);backdrop-filter:blur(30px);border-radius:40px;box-shadow:0 40px 120px rgba(0,0,0,0.25),0 0 0 1px rgba(255,255,255,0.3);max-width:950px;width:100%;padding:60px;animation:slideUp 0.8s cubic-bezier(0.34,1.56,0.64,1);position:relative;z-index:1}@keyframes slideUp{from{opacity:0;transform:translateY(50px) scale(0.95)}to{opacity:1;transform:translateY(0) scale(1)}}.logo-container{text-align:center;margin-bottom:15px}.logo{width:80px;height:80px;margin:0 auto 15px;background:linear-gradient(135deg,#667eea,#764ba2);border-radius:20px;display:flex;align-items:center;justify-content:center;font-size:40px;animation:logoFloat 3s ease-in-out infinite;box-shadow:0 15px 40px rgba(102,126,234,0.4)}@keyframes logoFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}.brand-name{font-size:1.1em;font-weight:700;background:linear-gradient(135deg,#667eea,#764ba2);-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:2px}h1{background:linear-gradient(135deg,#667eea,#764ba2,#f093fb);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:12px;font-size:3.2em;text-align:center;font-weight:800;letter-spacing:-2px;line-height:1.1}h1 .icon{display:inline-block;animation:bounce 2s ease-in-out infinite}@keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}.subtitle{text-align:center;color:#666;margin-bottom:45px;font-size:1.15em;font-weight:500}.search-box{display:flex;gap:15px;margin-bottom:30px;position:relative}.search-box::before{content:'';position:absolute;inset:-3px;background:linear-gradient(135deg,#667eea,#764ba2);border-radius:18px;opacity:0;transition:opacity 0.3s;z-index:-1}.search-box:focus-within::before{opacity:0.15}input[type="text"]{flex:1;padding:20px 26px;border:3px solid #e8e8e8;border-radius:18px;font-size:17px;font-weight:500;transition:all 0.3s;font-family:'Poppins',sans-serif}input[type="text"]:focus{outline:none;border-color:#667eea;box-shadow:0 8px 30px rgba(102,126,234,0.2);transform:translateY(-2px)}input[type="text"]::placeholder{color:#aaa}.year-selector{display:flex;gap:20px;margin-bottom:30px;align-items:center;justify-content:center;padding:25px;background:linear-gradient(135deg,#f5f7fa 0%,#c3cfe2 100%);border-radius:22px;box-shadow:inset 0 2px 10px rgba(0,0,0,0.05)}.year-input-group{display:flex;flex-direction:column;gap:10px}.year-input-group label{font-weight:700;color:#333;font-size:13px;letter-spacing:1px;text-transform:uppercase}.year-divider{font-size:28px;color:#667eea;font-weight:900;margin-top:30px;animation:pulse 2s ease-in-out infinite}@keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.1)}}input[type="number"]{padding:16px 22px;border:3px solid #e8e8e8;border-radius:14px;font-size:18px;width:150px;transition:all 0.3s;font-family:'Poppins',sans-serif;font-weight:700;text-align:center;background:white}input[type="number"]:focus{outline:none;border-color:#667eea;box-shadow:0 6px 25px rgba(102,126,234,0.2);transform:translateY(-2px)}button{padding:20px 45px;background:linear-gradient(135deg,#667eea,#764ba2);color:white;border:none;border-radius:18px;font-size:18px;font-weight:700;cursor:pointer;transition:all 0.4s;box-shadow:0 10px 35px rgba(102,126,234,0.35);position:relative;overflow:hidden;font-family:'Poppins',sans-serif}button::before{content:'';position:absolute;top:50%;left:50%;width:0;height:0;border-radius:50%;background:rgba(255,255,255,0.3);transform:translate(-50%,-50%);transition:width 0.6s,height 0.6s}button:hover::before{width:300px;height:300px}button:hover{transform:translateY(-4px);box-shadow:0 15px 45px rgba(102,126,234,0.45)}button:active{transform:translateY(-1px)}.recommendations{display:none;margin-bottom:30px;padding:30px;background:linear-gradient(135deg,#fdfbfb 0%,#ebedee 100%);border-radius:25px;border:3px solid #e0e0e0;animation:slideIn 0.5s ease}.recommendations.show{display:block}@keyframes slideIn{from{opacity:0;transform:translateX(-20px)}to{opacity:1;transform:translateX(0)}}.rec-item{padding:18px 24px;margin:12px 0;background:white;border-radius:16px;cursor:pointer;transition:all 0.3s;border:3px solid transparent;font-weight:600;position:relative;overflow:hidden}.rec-item::before{content:'';position:absolute;left:0;top:0;height:100%;width:4px;background:linear-gradient(135deg,#667eea,#764ba2);transform:scaleY(0);transition:transform 0.3s}.rec-item:hover{border-color:#667eea;transform:translateX(12px);box-shadow:0 8px 30px rgba(102,126,234,0.25)}.rec-item:hover::before{transform:scaleY(1)}.progress-container{display:none;margin-top:35px;padding:35px;background:linear-gradient(135deg,#fdfbfb 0%,#ebedee 100%);border-radius:25px;border:3px solid #e0e0e0;animation:slideIn 0.5s ease}.progress-container.show{display:block}.progress-stats{display:flex;gap:20px;margin-bottom:25px}.stat-box{background:white;padding:25px;border-radius:18px;text-align:center;flex:1;box-shadow:0 8px 25px rgba(0,0,0,0.08);transition:all 0.3s;border:2px solid transparent}.stat-box:hover{transform:translateY(-5px);border-color:#667eea;box-shadow:0 12px 35px rgba(102,126,234,0.2)}.stat-label{color:#666;font-weight:700;margin-top:10px;font-size:13px;letter-spacing:1px;text-transform:uppercase}.stat-number{font-size:2.8em;font-weight:900;background:linear-gradient(135deg,#667eea,#764ba2);-webkit-background-clip:text;-webkit-text-fill-color:transparent;animation:countUp 0.5s ease}.stat-icon{font-size:24px;margin-bottom:8px}@keyframes countUp{from{transform:scale(0.5);opacity:0}to{transform:scale(1);opacity:1}}.progress-bar-container{background:#ddd;height:45px;border-radius:25px;overflow:hidden;position:relative;box-shadow:inset 0 3px 8px rgba(0,0,0,0.15)}.progress-bar{height:100%;background:linear-gradient(90deg,#667eea,#764ba2,#f093fb);transition:width 0.4s cubic-bezier(0.4,0,0.2,1);display:flex;align-items:center;justify-content:center;color:white;font-weight:800;font-size:17px;box-shadow:0 0 20px rgba(102,126,234,0.6);position:relative;overflow:hidden}.progress-bar::after{content:'';position:absolute;top:0;left:0;bottom:0;right:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,0.3),transparent);animation:shimmer 2s infinite}@keyframes shimmer{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}.status{text-align:center;margin:30px 0;font-weight:700;font-size:1.35em;color:#333;padding:20px;border-radius:16px;background:rgba(102,126,234,0.05)}.download-btn{display:none;margin:25px auto;padding:22px 55px;background:linear-gradient(135deg,#11998e,#38ef7d);font-size:1.25em;box-shadow:0 12px 40px rgba(17,153,142,0.4);letter-spacing:0.5px}.download-btn.show{display:block;animation:popIn 0.6s cubic-bezier(0.34,1.56,0.64,1)}@keyframes popIn{from{transform:scale(0.8);opacity:0}to{transform:scale(1);opacity:1}}.download-btn:hover{background:linear-gradient(135deg,#38ef7d,#11998e);transform:translateY(-4px) scale(1.05);box-shadow:0 18px 50px rgba(17,153,142,0.5)}.loading{display:inline-block;width:24px;height:24px;border:4px solid #f3f3f3;border-top:4px solid #667eea;border-radius:50%;animation:spin 0.8s linear infinite;margin-right:12px;vertical-align:middle}@keyframes spin{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}</style></head><body><div class="floating-shapes"><div class="shape"></div><div class="shape"></div><div class="shape"></div><div class="shape"></div></div><div class="container"><div class="logo-container"><div class="logo">💼</div><div class="brand-name">FINARCH</div></div><h1><span class="icon">📊</span> Financial Data Archiver</h1><p class="subtitle">Extract Annual Reports, Presentations & Transcripts with Ease</p><div class="search-box"><input type="text" id="searchInput" placeholder="🔍 Enter Company Name, NSE or BSE Code..."/><button onclick="searchCompany()">🚀 Search</button></div><div class="year-selector"><div class="year-input-group"><label>📅 From Year</label><input type="number" id="startYear" value="2015" min="2000" max="2025"/></div><div class="year-divider">→</div><div class="year-input-group"><label>📅 To Year</label><input type="number" id="endYear" value="2025" min="2000" max="2025"/></div></div><div id="recommendations" class="recommendations"></div><div id="status" class="status"></div><button id="downloadBtn" class="download-btn" onclick="downloadZip()">📥 Download ZIP Archive</button><div id="progressContainer" class="progress-container"><div class="progress-stats"><div class="stat-box"><div class="stat-icon">✅</div><div class="stat-number" id="completedCount">0</div><div class="stat-label">Downloaded</div></div><div class="stat-box"><div class="stat-icon">📁</div><div class="stat-number" id="totalCount">0</div><div class="stat-label">Total Files</div></div><div class="stat-box"><div class="stat-icon">⏱️</div><div class="stat-number" id="etaCount">0s</div><div class="stat-label">Time Left</div></div></div><div class="progress-bar-container"><div class="progress-bar" id="progressBar" style="width:0%">0%</div></div></div></div><script>let selectedCompany=null;let eventSource=null;let sessionId=null;document.getElementById('searchInput').addEventListener('keypress',function(e){if(e.key==='Enter')searchCompany()});async function searchCompany(){const query=document.getElementById('searchInput').value.trim();if(!query){alert('⚠️ Please enter a company name or code');return}document.getElementById('status').innerHTML='<div class="loading"></div> Searching database...';document.getElementById('recommendations').classList.remove('show');document.getElementById('progressContainer').classList.remove('show');const response=await fetch('/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:query})});const data=await response.json();if(data.error){document.getElementById('status').innerHTML='❌ '+data.error;return}if(data.matches.length===1){selectedCompany=data.matches[0];startExtraction()}else{showRecommendations(data.matches)}}function showRecommendations(matches){document.getElementById('status').innerHTML='✨ Found '+matches.length+' matches. Select your company:';const recDiv=document.getElementById('recommendations');recDiv.innerHTML=matches.map((m,idx)=>'<div class="rec-item" onclick="selectCompany('+idx+')"><strong>'+m.Name+'</strong><br><span style="color:#888;font-size:0.9em">NSE: '+(m.NSE_Code||'N/A')+' | BSE: '+(m.BSE_Code||'N/A')+'</span></div>').join('');recDiv.classList.add('show');window.currentMatches=matches}function selectCompany(idx){selectedCompany=window.currentMatches[idx];document.getElementById('recommendations').classList.remove('show');startExtraction()}function startExtraction(){const startYear=parseInt(document.getElementById('startYear').value);const endYear=parseInt(document.getElementById('endYear').value);if(startYear>endYear){alert('⚠️ Start year must be less than or equal to end year');return}document.getElementById('status').innerHTML='<div class="loading"></div> Initializing extraction for <strong>'+selectedCompany.Name+'</strong>...';document.getElementById('progressContainer').classList.add('show');document.getElementById('downloadBtn').classList.remove('show');if(eventSource)eventSource.close();const url='/extract?symbol='+encodeURIComponent(selectedCompany.symbol)+'&name='+encodeURIComponent(selectedCompany.Name)+'&start_year='+startYear+'&end_year='+endYear;eventSource=new EventSource(url);eventSource.onmessage=function(event){const data=event.data.split('|');const type=data[0];if(type==='STATUS'){document.getElementById('status').innerHTML='<div class="loading"></div> '+data[1]}else if(type==='TOTAL'){document.getElementById('totalCount').textContent=data[1]}else if(type==='PROGRESS'){const completed=parseInt(data[1]);const total=parseInt(data[2]);const eta=parseInt(data[3]);const percentage=Math.round((completed/total)*100);document.getElementById('completedCount').textContent=completed;document.getElementById('etaCount').textContent=eta+'s';document.getElementById('progressBar').style.width=percentage+'%';document.getElementById('progressBar').textContent=percentage+'%'}else if(type==='COMPLETE'){sessionId=data[3];document.getElementById('status').innerHTML='🎉 <strong>Extraction Complete!</strong> Successfully downloaded '+data[1]+'/'+data[2]+' files for <strong>'+selectedCompany.Name+'</strong>';document.getElementById('downloadBtn').classList.add('show');eventSource.close()}else if(type==='ERROR'){document.getElementById('status').innerHTML='❌ Error: '+data[1];eventSource.close()}}}function downloadZip(){if(sessionId){document.getElementById('status').innerHTML='<div class="loading"></div> Preparing your ZIP archive...';window.location.href='/download?session='+sessionId}}</script></body></html>'''

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
    session_id = f"{symbol}_{int(time.time())}"

    def generate():
        fetcher = ScreenerUnifiedFetcher()
        
        def run_extraction():
            company_root = fetcher.process_company(symbol, name, start_year, end_year)
            if company_root:
                # Store the session data
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
        
        # Don't delete the session immediately - keep it for 5 minutes
        # Clean up old sessions (older than 5 minutes)
        current_time = time.time()
        sessions_to_delete = [
            sid for sid, data in download_sessions.items() 
            if current_time - data['timestamp'] > 300  # 5 minutes
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
