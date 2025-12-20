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
        """Extract year and month from element"""
        row = element.find_parent('li')
        search_text = row.get_text(" ", strip=True) if row else element.get_text(" ", strip=True)
        
        href = element.get('href', '')
        combined_text = search_text + " " + href
        
        year_matches = re.findall(r'\b(20\d{2})\b', combined_text)
        year = year_matches[0] if year_matches else "Unknown_Year"
        
        month_list = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
        month_name = "General"
        
        for m in month_list:
            if re.search(rf'\b{m}\b', combined_text, re.I):
                month_name = m.capitalize()
                break
        
        return year, month_name

    def download_file(self, url, save_path):
        try:
            full_url = urljoin(SCREENER_DOMAIN, url) if not url.startswith('http') else url
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
        self.company_root = str(comp_root)
        download_tasks = []
        
        # ===== ANNUAL REPORTS =====
        log_queue.put(f"STATUS|Searching for annual reports...")
        
        # Find ALL links on the page that might be annual reports
        all_page_links = soup.find_all('a', href=True)
        
        for link in all_page_links:
            href = link.get('href', '')
            link_text = link.get_text(strip=True).lower()
            
            # Skip non-PDF links
            if not href or not href.startswith('http'):
                continue
            
            # Check if this looks like an annual report
            is_annual_report = False
            
            # Method 1: Check link text
            if 'annual' in link_text and 'report' in link_text:
                is_annual_report = True
            
            # Method 2: Check href
            if 'annual' in href.lower() and 'report' in href.lower():
                is_annual_report = True
            
            # Method 3: Check parent context
            parent = link.find_parent(['li', 'div', 'tr', 'td'])
            if parent:
                parent_text = parent.get_text(" ", strip=True).lower()
                if 'annual' in parent_text and 'report' in parent_text:
                    is_annual_report = True
            
            if not is_annual_report:
                continue
            
            # Extract year from anywhere we can find it
            combined_text = f"{link_text} {href}"
            if parent:
                combined_text += f" {parent.get_text()}"
            
            year_matches = re.findall(r'\b(20\d{2})\b', combined_text)
            
            if not year_matches:
                continue
            
            year = year_matches[0]
            year_int = int(year)
            
            # Check year range
            if year_int < start_year or year_int > end_year:
                continue
            
            save_dir = comp_root / "Annual_Reports" / year
            file_path = save_dir / f"Annual_Report_{year}.pdf"
            
            # Avoid duplicates
            counter = 1
            while any(str(file_path) == str(task[3]) for task in download_tasks):
                file_path = save_dir / f"Annual_Report_{year}_{counter}.pdf"
                counter += 1
            
            download_tasks.append(('Annual Report', year, href, file_path))
            log_queue.put(f"STATUS|Found Annual Report: {year}")

        # ===== PPT & TRANSCRIPTS =====
        log_queue.put(f"STATUS|Searching for presentations and transcripts...")
        seen_urls = set()

        for link in all_page_links:
            link_text = link.get_text(strip=True).lower()
            href = link['href']
            
            if href in seen_urls or not href.startswith('http') or "consolidated" in href: 
                continue

            cat = None
            if "transcript" in link_text: 
                cat = "Transcript"
            elif link_text == "ppt": 
                cat = "PPT"
            
            if cat:
                year, month = self.extract_metadata(link)
                
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
        log_queue.put(f"STATUS|Starting download of {total_files} files...")
        
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

# HTML template stored separately to avoid string issues
HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Financial Document Archiver</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Poppins', sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 50%, #f093fb 100%); min-height: 100vh; display: flex; justify-content: center; align-items: center; padding: 20px; }
.container { background: rgba(255,255,255,0.98); border-radius: 40px; box-shadow: 0 40px 120px rgba(0,0,0,0.25); max-width: 950px; width: 100%; padding: 60px; }
h1 { background: linear-gradient(135deg, #667eea, #764ba2, #f093fb); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 12px; font-size: 3.2em; text-align: center; font-weight: 800; }
.subtitle { text-align: center; color: #666; margin-bottom: 45px; font-size: 1.15em; }
.search-box { display: flex; gap: 15px; margin-bottom: 30px; }
input[type="text"] { flex: 1; padding: 20px 26px; border: 3px solid #e8e8e8; border-radius: 18px; font-size: 17px; font-weight: 500; }
input[type="number"] { padding: 16px 22px; border: 3px solid #e8e8e8; border-radius: 14px; font-size: 18px; width: 150px; text-align: center; }
button { padding: 20px 45px; background: linear-gradient(135deg, #667eea, #764ba2); color: white; border: none; border-radius: 18px; font-size: 18px; font-weight: 700; cursor: pointer; }
.year-selector { display: flex; gap: 20px; margin-bottom: 30px; align-items: center; justify-content: center; padding: 25px; background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%); border-radius: 22px; }
.recommendations { display: none; margin-bottom: 30px; padding: 30px; background: #f5f5f5; border-radius: 25px; }
.recommendations.show { display: block; }
.rec-item { padding: 18px 24px; margin: 12px 0; background: white; border-radius: 16px; cursor: pointer; transition: all 0.3s; }
.rec-item:hover { border: 3px solid #667eea; transform: translateX(12px); }
.progress-container { display: none; margin-top: 35px; padding: 35px; background: #f5f5f5; border-radius: 25px; }
.progress-container.show { display: block; }
.progress-stats { display: flex; gap: 20px; margin-bottom: 25px; }
.stat-box { background: white; padding: 25px; border-radius: 18px; text-align: center; flex: 1; }
.stat-number { font-size: 2.8em; font-weight: 900; background: linear-gradient(135deg, #667eea, #764ba2); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.progress-bar-container { background: #ddd; height: 45px; border-radius: 25px; overflow: hidden; }
.progress-bar { height: 100%; background: linear-gradient(90deg, #667eea, #764ba2, #f093fb); transition: width 0.4s; display: flex; align-items: center; justify-content: center; color: white; font-weight: 800; }
.status { text-align: center; margin: 30px 0; font-weight: 700; font-size: 1.35em; color: #333; }
.download-btn { display: none; margin: 25px auto; background: linear-gradient(135deg, #11998e, #38ef7d); }
.download-btn.show { display: block; }
.loading { display: inline-block; width: 24px; height: 24px; border: 4px solid #f3f3f3; border-top: 4px solid #667eea; border-radius: 50%; animation: spin 0.8s linear infinite; margin-right: 12px; }
@keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="container">
<h1>📊 Financial Data Archiver</h1>
<p class="subtitle">Extract Annual Reports, Presentations & Transcripts</p>
<div class="search-box">
<input type="text" id="searchInput" placeholder="🔍 Enter Company Name, NSE or BSE Code..."/>
<button onclick="searchCompany()">🚀 Search</button>
</div>
<div class="year-selector">
<div><label>📅 From Year</label><br><input type="number" id="startYear" value="2015" min="2000" max="2030"/></div>
<div style="font-size:28px;font-weight:900;">→</div>
<div><label>📅 To Year</label><br><input type="number" id="endYear" value="2024" min="2000" max="2030"/></div>
</div>
<div id="recommendations" class="recommendations"></div>
<div id="status" class="status"></div>
<button id="downloadBtn" class="download-btn" onclick="downloadZip()">📥 Download ZIP</button>
<div id="progressContainer" class="progress-container">
<div class="progress-stats">
<div class="stat-box"><div class="stat-number" id="completedCount">0</div><div>Downloaded</div></div>
<div class="stat-box"><div class="stat-number" id="totalCount">0</div><div>Total Files</div></div>
<div class="stat-box"><div class="stat-number" id="etaCount">0s</div><div>Time Left</div></div>
</div>
<div class="progress-bar-container">
<div class="progress-bar" id="progressBar" style="width:0%">0%</div>
</div>
</div>
</div>
<script>
let selectedCompany = null;
let eventSource = null;
let sessionId = null;

document.getElementById('searchInput').addEventListener('keypress', function(e) {
    if(e.key === 'Enter') searchCompany();
});

async function searchCompany() {
    const query = document.getElementById('searchInput').value.trim();
    if(!query) { alert('⚠️ Please enter a company name or code'); return; }
    
    document.getElementById('status').innerHTML = '<div class="loading"></div> Searching...';
    document.getElementById('recommendations').classList.remove('show');
    document.getElementById('progressContainer').classList.remove('show');
    
    const response = await fetch('/search', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({query: query})
    });
    
    const data = await response.json();
    
    if(data.error) {
        document.getElementById('status').innerHTML = '❌ ' + data.error;
        return;
    }
    
    if(data.matches.length === 1) {
        selectedCompany = data.matches[0];
        startExtraction();
    } else {
        showRecommendations(data.matches);
    }
}

function showRecommendations(matches) {
    document.getElementById('status').innerHTML = '✨ Found ' + matches.length + ' matches:';
    const recDiv = document.getElementById('recommendations');
    recDiv.innerHTML = matches.map((m, idx) => 
        '<div class="rec-item" onclick="selectCompany(' + idx + ')"><strong>' + m.Name + 
        '</strong><br><span style="color:#888;">NSE: ' + (m.NSE_Code || 'N/A') + 
        ' | BSE: ' + (m.BSE_Code || 'N/A') + '</span></div>'
    ).join('');
    recDiv.classList.add('show');
    window.currentMatches = matches;
}

function selectCompany(idx) {
    selectedCompany = window.currentMatches[idx];
    document.getElementById('recommendations').classList.remove('show');
    startExtraction();
}

function startExtraction() {
    const startYear = parseInt(document.getElementById('startYear').value);
    const endYear = parseInt(document.getElementById('endYear').value);
    
    if(startYear > endYear) {
        alert('⚠️ Start year must be ≤ end year');
        return;
    }
    
    document.getElementById('status').innerHTML = '<div class="loading"></div> Extracting...';
    document.getElementById('progressContainer').classList.add('show');
    document.getElementById('downloadBtn').classList.remove('show');
    
    if(eventSource) eventSource.close();
    
    const url = '/extract?symbol=' + encodeURIComponent(selectedCompany.symbol) + 
                '&name=' + encodeURIComponent(selectedCompany.Name) + 
                '&start_year=' + startYear + '&end_year=' + endYear;
    
    eventSource = new EventSource(url);
    
    eventSource.onmessage = function(event) {
        const data = event.data.split('|');
        const type = data[0];
        
        if(type === 'STATUS') {
            document.getElementById('status').innerHTML = '<div class="loading"></div> ' + data[1];
        } else if(type === 'TOTAL') {
            document.getElementById('totalCount').textContent = data[1];
        } else if(type === 'PROGRESS') {
            const completed = parseInt(data[1]);
            const total = parseInt(data[2]);
            const eta = parseInt(data[3]);
            const percentage = Math.round((completed/total)*100);
            
            document.getElementById('completedCount').textContent = completed;
            document.getElementById('etaCount').textContent = eta + 's';
            document.getElementById('progressBar').style.width = percentage + '%';
            document.getElementById('progressBar').textContent = percentage + '%';
        } else if(type === 'COMPLETE') {
            sessionId = data[3];
            document.getElementById('status').innerHTML = '🎉 Complete! Downloaded ' + data[1] + '/' + data[2] + ' files';
            document.getElementById('downloadBtn').classList.add('show');
            eventSource.close();
        } else if(type === 'ERROR') {
            document.getElementById('status').innerHTML = '❌ Error: ' + data[1];
            eventSource.close();
        }
    };
}

function downloadZip() {
    if(sessionId) {
        document.getElementById('status').innerHTML = '<div class="loading"></div> Preparing ZIP...';
        window.location.href = '/download?session=' + sessionId;
    }
}
</script>
</body>
</html>"""

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
    end_year = int(request.args.get('end_year', 2024))
    session_id = f"{symbol}_{int(time.time())}"

    def generate():
        fetcher = ScreenerUnifiedFetcher()
        
        def run_extraction():
            company_root = fetcher.process_company(symbol, name, start_year, end_year)
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
