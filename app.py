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
                self.downloaded_files.append(save_path)
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
            return []

        comp_root = DOCUMENTS_ROOT / self.sanitize(name)
        download_tasks = []
        
        ar_section = soup.find('div', id='annual-reports')
        if not ar_section:
            header = soup.find(lambda tag: tag.name in ['h2', 'h3'] and 'annual report' in tag.text.lower())
            if header: ar_section = header.find_next('div')

        if ar_section:
            ar_items = ar_section.find_all('li')
            for li in ar_items:
                full_row_text = li.get_text(" ", strip=True)
                year_match = re.search(r'\b(20\d{2})\b', full_row_text)
                year = year_match.group(1) if year_match else "Unknown_Year"
                
                if year != "Unknown_Year" and (int(year) < start_year or int(year) > end_year):
                    continue
                
                link = li.find('a', href=True)
                if link:
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
                
                if year != "Unknown_Year" and (int(year) < start_year or int(year) > end_year):
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
            return []

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
        
        log_queue.put(f"COMPLETE|{completed}|{total_files}|{str(comp_root)}")
        return self.downloaded_files

app = Flask(__name__)
download_paths = {}

HTML_TEMPLATE = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Financial Document Archiver</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:'Inter','Segoe UI',sans-serif;background:linear-gradient(135deg,#1e3c72 0%,#2a5298 50%,#7e22ce 100%);min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px;animation:gradientShift 10s ease infinite;background-size:200% 200%}@keyframes gradientShift{0%,100%{background-position:0% 50%}50%{background-position:100% 50%}}.container{background:rgba(255,255,255,0.95);backdrop-filter:blur(20px);border-radius:30px;box-shadow:0 30px 90px rgba(0,0,0,0.4);max-width:900px;width:100%;padding:50px;animation:slideUp 0.6s ease}@keyframes slideUp{from{opacity:0;transform:translateY(30px)}to{opacity:1;transform:translateY(0)}}h1{background:linear-gradient(135deg,#1e3c72,#7e22ce);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:10px;font-size:3em;text-align:center;font-weight:800}.subtitle{text-align:center;color:#555;margin-bottom:40px;font-size:1.2em}.search-box{display:flex;gap:15px;margin-bottom:25px}input[type="text"]{flex:1;padding:18px 24px;border:3px solid #e0e0e0;border-radius:15px;font-size:17px}input[type="text"]:focus{outline:none;border-color:#7e22ce}.year-selector{display:flex;gap:15px;margin-bottom:25px;align-items:center;justify-content:center}.year-input-group{display:flex;flex-direction:column;gap:8px}.year-input-group label{font-weight:600;color:#555}input[type="number"]{padding:15px 20px;border:3px solid #e0e0e0;border-radius:12px;font-size:16px;width:140px;text-align:center}button{padding:18px 40px;background:linear-gradient(135deg,#1e3c72,#7e22ce);color:white;border:none;border-radius:15px;font-size:17px;font-weight:700;cursor:pointer}button:hover{transform:translateY(-3px)}.recommendations{display:none;margin-bottom:25px;padding:25px;background:#f8f9fa;border-radius:20px}.recommendations.show{display:block}.rec-item{padding:16px 20px;margin:10px 0;background:white;border-radius:12px;cursor:pointer}.rec-item:hover{border:3px solid #7e22ce}.progress-container{display:none;margin-top:30px;padding:30px;background:#f8f9fa;border-radius:20px}.progress-container.show{display:block}.stat-box{background:white;padding:20px;border-radius:15px;text-align:center;flex:1}.stat-number{font-size:2.5em;font-weight:800}.progress-bar{height:40px;background:linear-gradient(90deg,#1e3c72,#7e22ce);color:white;font-weight:700;font-size:16px;display:flex;align-items:center;justify-content:center}.status{text-align:center;margin:25px 0;font-weight:700;font-size:1.3em}.download-btn{display:none;margin:20px auto;padding:20px 50px;background:linear-gradient(135deg,#10b981,#059669);font-size:1.2em}.download-btn.show{display:block}</style></head><body><div class="container"><h1>📊 Financial Data</h1><p class="subtitle">Extract Reports, PPTs & Transcripts</p><div class="search-box"><input type="text" id="searchInput" placeholder="Enter Company Name or Code..."/><button onclick="searchCompany()">Search</button></div><div class="year-selector"><div class="year-input-group"><label>FROM</label><input type="number" id="startYear" value="2015" min="2000" max="2025"/></div><div>→</div><div class="year-input-group"><label>TO</label><input type="number" id="endYear" value="2025" min="2000" max="2025"/></div></div><div id="recommendations" class="recommendations"></div><div id="status" class="status"></div><button id="downloadBtn" class="download-btn" onclick="downloadZip()">Download ZIP</button><div id="progressContainer" class="progress-container"><div id="completedCount">0</div><div id="totalCount">0</div><div class="progress-bar" id="progressBar" style="width:0%">0%</div></div></div><script>let selectedCompany=null;let eventSource=null;let sessionId=null;document.getElementById('searchInput').addEventListener('keypress',function(e){if(e.key==='Enter')searchCompany()});async function searchCompany(){const query=document.getElementById('searchInput').value.trim();if(!query){alert('Enter company name');return}document.getElementById('status').innerHTML='Searching...';const response=await fetch('/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:query})});const data=await response.json();if(data.error){document.getElementById('status').innerHTML='❌ '+data.error;return}if(data.matches.length===1){selectedCompany=data.matches[0];startExtraction()}else{showRecommendations(data.matches)}}function showRecommendations(matches){document.getElementById('status').innerHTML='Found '+matches.length+' matches:';const recDiv=document.getElementById('recommendations');recDiv.innerHTML=matches.map((m,idx)=>'<div class="rec-item" onclick="selectCompany('+idx+')">'+m.Name+'</div>').join('');recDiv.classList.add('show');window.currentMatches=matches}function selectCompany(idx){selectedCompany=window.currentMatches[idx];document.getElementById('recommendations').classList.remove('show');startExtraction()}function startExtraction(){const startYear=parseInt(document.getElementById('startYear').value);const endYear=parseInt(document.getElementById('endYear').value);document.getElementById('status').innerHTML='Extracting...';document.getElementById('progressContainer').classList.add('show');if(eventSource)eventSource.close();const url='/extract?symbol='+encodeURIComponent(selectedCompany.symbol)+'&name='+encodeURIComponent(selectedCompany.Name)+'&start_year='+startYear+'&end_year='+endYear;eventSource=new EventSource(url);eventSource.onmessage=function(event){const data=event.data.split('|');const type=data[0];if(type==='TOTAL'){document.getElementById('totalCount').textContent=data[1]}else if(type==='PROGRESS'){const completed=parseInt(data[1]);const total=parseInt(data[2]);const percentage=Math.round((completed/total)*100);document.getElementById('completedCount').textContent=completed;document.getElementById('progressBar').style.width=percentage+'%';document.getElementById('progressBar').textContent=percentage+'%'}else if(type==='COMPLETE'){sessionId=data[3];document.getElementById('status').innerHTML='✅ Complete! Downloaded '+data[1]+'/'+data[2]+' files';document.getElementById('downloadBtn').classList.add('show');eventSource.close()}}}function downloadZip(){if(sessionId){window.location.href='/download?session='+sessionId}}</script></body></html>'''

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
            fetcher.process_company(symbol, name, start_year, end_year)
        
        thread = threading.Thread(target=run_extraction)
        thread.start()

        while thread.is_alive() or not log_queue.empty():
            try:
                log_line = log_queue.get(timeout=0.1)
                if log_line.startswith('COMPLETE'):
                    parts = log_line.split('|')
                    if len(parts) > 3 and parts[3]:
                        download_paths[session_id] = parts[3]
                        yield f"data: COMPLETE|{parts[1]}|{parts[2]}|{session_id}\n\n"
                    else:
                        yield f"data: {log_line}\n\n"
                else:
                    yield f"data: {log_line}\n\n"
            except queue.Empty:
                continue

    return Response(generate(), mimetype='text/event-stream')

@app.route('/download')
def download():
    session_id = request.args.get('session')
    
    if not session_id or session_id not in download_paths:
        return "No files available", 404
    
    download_path = download_paths[session_id]
    
    if not download_path or not Path(download_path).exists():
        return "Files not found", 404
    
    memory_file = io.BytesIO()
    
    try:
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
            root_path = Path(download_path)
            for file_path in root_path.rglob('*.pdf'):
                arcname = file_path.relative_to(root_path.parent)
                zipf.write(file_path, arcname)
        
        memory_file.seek(0)
        company_name = Path(download_path).name
        zip_filename = f"{company_name}_Documents.zip"
        
        del download_paths[session_id]
        
        return send_file(memory_file, mimetype='application/zip', as_attachment=True, download_name=zip_filename)
    except Exception as e:
        return f"Error: {str(e)}", 500

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
