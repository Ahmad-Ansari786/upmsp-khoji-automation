import os
import re
import sys
import hashlib
import requests
from bs4 import BeautifulSoup
import boto3
from botocore.exceptions import NoCredentialsError
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from datetime import datetime
from google import genai            # Naya import
from google.genai import types      # Config ke liye naya import
import json
from json_repair import repair_json
import time
import pytz 

# =====================================================================
# ⚙️ FULL CONFIGURATION BLOCK (GitHub Secrets Encryption Layer)
# =====================================================================
CLOUDFLARE_ACCESS_KEY = os.environ.get("CF_ACCESS_KEY", "").strip()
CLOUDFLARE_SECRET_KEY = os.environ.get("CF_SECRET_KEY", "").strip()
CLOUDFLARE_ENDPOINT = os.environ.get("CF_ENDPOINT", "").strip()
CLOUDFLARE_PUBLIC_BASE_URL = os.environ.get("CF_PUBLIC_URL", "").strip()
CLOUDFLARE_BUCKET_NAME = os.environ.get("CF_BUCKET_NAME", "").strip()

# 🌟 Gemini API Key Setup (Naya Tarika)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
client = None
if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)

FIREBASE_SERVICE_ACCOUNT_JSON = "serviceAccountKey.json"

# =====================================================================
# 🚀 CHANNELS INITIALIZATION (Core Connectivity Engine)
# =====================================================================
print("🔄 Initializing cloud connections...")

if not os.path.exists(FIREBASE_SERVICE_ACCOUNT_JSON):
    print(f"❌ Error: '{FIREBASE_SERVICE_ACCOUNT_JSON}' file nahi mili! Automation workflow aborted.")
    sys.exit(1)

cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_JSON)
firebase_admin.initialize_app(cred)
db = firestore.client()
firestore_collection = db.collection("live_notices")

r2_client = boto3.client(
    service_name='s3',
    endpoint_url=CLOUDFLARE_ENDPOINT,
    aws_access_key_id=CLOUDFLARE_ACCESS_KEY,
    aws_secret_access_key=CLOUDFLARE_SECRET_KEY,
    region_name='auto'
)

# =====================================================================
# 🤖 GOOGLE AI (GEMINI) SUMMARY GENERATOR
# =====================================================================
def generate_ai_data(bytes_payload, mime_type, title):
    if not GEMINI_API_KEY or not client:
        return None
    
    if len(bytes_payload) > 18 * 1024 * 1024:
        print(f"⚠️ File is too large for inline AI processing (>18MB). Skipping AI extraction.")
        return None

    OPTIMIZED_PROMPT = (
        "You are an elite, enterprise-grade Document Intelligence and OCR Specialist AI.\n"
        "Your core directive is to perform a deep, comprehensive multimodal analysis of the attached document (PDF or Image) and extract its structure and contents into a flawless JSON object.\n\n"
        "### REQUIRED JSON SCHEMA:\n"
        "You must return a JSON object containing exactly these four keys:\n"
        "{\n"
        "  \"summary\": \"A high-quality, dense 5-6 line (in bullet poins) summary written in formal, professional HINDI (शुद्ध और प्रशासनिक हिंदी). It must capture the issuing authority, the exact core objective, critical dates/deadlines, and specific action items. Avoid vague sentences.\",\n"
        "  \"englishSummary\": \"A detailed, high-quality 5-6 line summary in formal ENGLISH that perfectly mirrors the depth, context, and structural facts of the Hindi summary.\",\n"
        "  \"search_keywords\": [\"An array of exactly 12-18 highly relevant keywords, proper nouns, abbreviations, department names, and semantic search terms extracted directly from the text. Include both Hindi and English variations to optimize for downstream Typesense search index matching.\"],\n"
        "  \"fullText\": \"The absolute complete, verbatim text extraction (OCR) of the entire document from the first word to the last. Do not truncate, do not summarize, and do not skip any section, header, table, or footer. Capture everything precisely with exact text characters.\"\n"
        "}\n\n"
        "### CRITICAL EXTRACTION RULES:\n"
        "1. Strict Output Format: Return ONLY the raw JSON object string. Do not use markdown wrappers, do not include ```json, and do not add conversational pleasantries.\n"
        "2. Character Escaping: Carefully escape all control characters, internal double quotes (\\\"), and ensure line breaks are correctly preserved as standard '\\n' inside the text values to ensure json.loads never fails.\n"
        "3. Multi-lingual Robustness: Maintain flawless native character encoding for all scripts present (English, Hindi, Urdu, Sindhi, etc.). Do not convert regional text into unicode symbols or escape strings like \\uXXXX. Keep them native.\n"
        "4. Deep Scan Capability: Actively extract text from low-contrast, stamped, or handwritten elements typically found in scanned government orders or official notices."
    )
        
    try:
        prompt_input = [
          f"Notice Title Context: {title}", 
         types.Part.from_bytes(data=bytes_payload, mime_type=mime_type),
         OPTIMIZED_PROMPT
         ]

        # जेमिनी को JSON मोड में कॉल करना (Naya SDK Method)
        ai_response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt_input,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        raw_text = ai_response.text.strip()
        repaired_json_str = repair_json(raw_text)
        ai_data = json.loads(repaired_json_str)
        return ai_data

    except Exception as e:
        print(f"⚠️ Google AI Extraction Error: {e}")
        return None

# =====================================================================
# 🛠️ HELPER PARSING FUNCTIONS (Data Security & Formatting)
# =====================================================================
def clean_document_id(file_name):
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', file_name.replace(".pdf", ""))
    if len(safe_name) > 50:
        hash_suffix = hashlib.md5(file_name.encode()).hexdigest()[:6]
        return f"{safe_name[:40]}_{hash_suffix}"
    return safe_name

def get_smart_content_type(extension):
    types_map = {
        'pdf': 'application/pdf',
        'jpeg': 'image/jpeg',
        'jpg': 'image/jpeg',
        'png': 'image/png',
        'gif': 'image/gif',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'doc': 'application/msword',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'xls': 'application/vnd.ms-excel',
        'zip': 'application/zip',
        'rar': 'application/x-rar-compressed'
    }
    return types_map.get(extension.lower(), 'application/octet-stream')

def send_fcm_push_notification(notice_title, is_webpage_link):
    try:
        display_title = "📢 UPMSP BOARD ALERT!"
        
        if is_webpage_link:
            display_body = f"🔗 New Portal Link Open:\n{notice_title}"
        else:
            display_body = f"📄 New Document Released:\n{notice_title}"
            
        if len(display_body) > 120:
            display_body = display_body[:117] + "..."

        message = messaging.Message(
            data={
                'title': display_title,
                'body': display_body,
                'badge': '1',
                'channel_id': 'upmsp_notices_channel'  
            },
            topic="all_users"
        )
        
        response = messaging.send(message)
        print(f"📢 STYLISH PUSH NOTIFICATION SENT SUCCESSFULLY -> Token ID: {response}")
    except Exception as n_err:
        print(f"⚠️ Notification Dispatch System Error: {n_err}")

# =====================================================================
# 🎯 MAIN EXTRACTION ENGINE (Universal Link & Multi-File Handler)
# =====================================================================
def run_upmsp_pipeline():
    print(f"\n🌐 [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Connecting to official UPMSP Notice Portal...")
    portal_url = "https://upmsp.edu.in/"  
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        response = requests.get(portal_url, headers=headers, timeout=20)
        if response.status_code != 200:
            print(f"❌ Portal Down: HTTP Status {response.status_code}")
            return
    except Exception as e:
        print(f"❌ Connection Failed: {e}")
        return

    soup = BeautifulSoup(response.text, 'html.parser')
    all_notice_rows = soup.find_all('tr', class_='trspace')
    print(f"🔍 Found {len(all_notice_rows)} verified notice row nodes in table layout. Processing...")

    success_count = 0
    skip_count = 0

    for row in all_notice_rows:
        ist_timezone = pytz.timezone('Asia/Kolkata')
        current_ist_time = datetime.now(ist_timezone)
        live_entry_date = current_ist_time.strftime("%d-%m-%Y")
        
        cells = row.find_all('td')
        if len(cells) < 4:
            continue  
            
        raw_title = cells[1].get_text(separator=" ").strip()
        cleaned_title = re.sub(r'\s+', ' ', raw_title).replace("Download", "").replace("new", "").strip()
        
        original_website_date = cells[2].get_text().strip()
        if not original_website_date:
            original_website_date = live_entry_date

        links_to_process = []

        download_anchor = cells[3].find('a', href=True)
        if download_anchor:
            links_to_process.append({
                "title": cleaned_title,
                "url": download_anchor['href'].strip(),
                "is_sublink": False
            })
        else:
            description_anchors = cells[1].find_all('a', href=True)
            if description_anchors:
                cell_html = str(cells[1])
                for anchor in description_anchors:
                    sub_link_text = anchor.get_text().strip()
                    href = anchor['href'].strip()

                    anchor_html = str(anchor)
                    anchor_index = cell_html.find(anchor_html)
                    text_before_link = cell_html[:anchor_index] if anchor_index != -1 else ""

                    category = ""
                    hs_idx = max(text_before_link.rfind("हाईस्कूल"), text_before_link.rfind("High School"))
                    inter_idx = max(text_before_link.rfind("इण्टरमीडिएट"), text_before_link.rfind("Intermediate"))

                    if hs_idx > inter_idx:
                        category = "[हाईस्कूल] "
                    elif inter_idx > hs_idx:
                        category = "[इण्टरमीडिएट] "

                    main_title_cleaned = cleaned_title.split('।')[0].strip()
                    specific_title = f"{main_title_cleaned} - {category}{sub_link_text}"

                    links_to_process.append({
                        "title": specific_title,
                        "url": href,
                        "is_sublink": True
                    })
            else:
                continue 

        for link_data in links_to_process:
            href_link = link_data["url"]
            final_title = link_data["title"]
            is_sublink = link_data.get("is_sublink", False)

            link_parts = href_link.split('?')[0].split('/')[-1].split('.')
            link_extension = link_parts[-1].lower() if len(link_parts) > 1 else ""

            known_file_extensions = ['pdf', 'jpeg', 'jpg', 'png', 'gif', 'docx', 'doc', 'xlsx', 'xls', 'zip', 'rar']
            is_webpage_link = ".aspx" in href_link.lower() or link_extension not in known_file_extensions

            if href_link.startswith('http://') or href_link.startswith('https://'):
                target_url = href_link
            else:
                clean_path = href_link.lstrip('./')
                target_url = "https://upmsp.edu.in/" + clean_path

            file_name = target_url.split('/')[-1] if not is_webpage_link else "portal_link.pdf"
            
            if is_webpage_link:
                if is_sublink:
                    unique_str = f"{target_url}_{final_title}"
                    doc_id = clean_document_id(hashlib.md5(unique_str.encode()).hexdigest()[:12])
                else:
                    doc_id = clean_document_id(hashlib.md5(target_url.encode()).hexdigest()[:12])
            else:
                doc_id = clean_document_id(file_name)

            try:
                doc_ref = firestore_collection.document(doc_id)
                doc_snapshot = doc_ref.get()
                if doc_snapshot.exists:
                    skip_count += 1
                    continue
            except Exception as err:
                print(f"⚠️ Registry read error for [{doc_id}]: {err}")
                continue

            live_entry_date = datetime.now().strftime("%d-%m-%Y")

            print("-" * 50)
            print(f"📋 New Entry Match: {final_title[:50]}...")
            print(f"📅 Website Date: {original_website_date} | ⚡ Entry Date: {live_entry_date}")
            print(f"🔗 Target Link Type: {'WEBPORTAL' if is_webpage_link else f'FILE ({link_extension.upper()})'}")
            
            cloudflare_permanent_url = target_url
            
            ai_extracted = {
                "summary": "Portal link notice - please visit the portal for full details.",
                "englishSummary": "",
                "search_keywords": [],
                "fullText": ""
            }

            if not is_webpage_link:
                print(f"📥 Streaming file bytes from UPMSP for [{file_name}]...")
                try:
                    file_response = requests.get(target_url, headers=headers, timeout=60)
                    if file_response.status_code != 200:
                        print(f"⚠️ File Stream failed ({file_response.status_code}). Skipping...")
                        continue
                        
                    bytes_payload = file_response.content
                    content_type_header = get_smart_content_type(link_extension)

                    print("🧠 Running Full AI Document Extraction (OCR + Summaries)...")
                    ai_result = generate_ai_data(bytes_payload, content_type_header, final_title)
                    if ai_result:
                        ai_extracted.update(ai_result) 

                    print(f"☁️ Pushing binary data to Cloudflare R2 [Mime: {content_type_header}]...")
                    r2_client.put_object(
                        Bucket=CLOUDFLARE_BUCKET_NAME,
                        Key=f"notices/{file_name}",
                        Body=bytes_payload,
                        ContentType=content_type_header
                    )
                    cloudflare_permanent_url = f"{CLOUDFLARE_PUBLIC_BASE_URL.rstrip('/')}/notices/{file_name}"
                    print(f"✅ R2 Permanent Backup URL: {cloudflare_permanent_url}")

                    time.sleep(5)

                except NoCredentialsError:
                    print("❌ Invalid Cloudflare API Credentials! Stopping pipeline execution.")
                    return
                except Exception as e:
                    print(f"❌ R2 Upload execution error: {e}")
                    continue
            else:
                print("🌐 [Webportal Detected] Cloudflare upload & AI Summary skipped. Directing link to Firestore...")

            print("⚡ Synchronizing Firestore Realtime Nodes...")
            try:
                is_pdf_file = (link_extension == 'pdf')
                
                doc_ref.set({
                    "id": doc_id,
                    "title": final_title,
                    "fileName": file_name if not is_webpage_link else "",
                    "date": live_entry_date,  
                    "originalWebsiteDate": original_website_date,  
                    "ts_epoch": int(current_ist_time.timestamp()), 
                    "timestamp": firestore.SERVER_TIMESTAMP,     
                    "targetClass": "General", 
                    "department": "UPMSP Board Office",
                    "isTrade": False,
                    "isPdf": is_pdf_file,
                    "isWebpage": is_webpage_link,
                    "serverFileUrl": cloudflare_permanent_url,
                    "viewCount": 0,
                    "fullText": ai_extracted.get("fullText", ""),
                    "summary": ai_extracted.get("summary", ""),
                    "englishSummary": ai_extracted.get("englishSummary", ""),
                    "search_keywords": ai_extracted.get("search_keywords", []),
                    "status": "published"
                })
                print(f"✅ SUCCESS: Complete Sync Saved for [{doc_id}]")
                send_fcm_push_notification(final_title, is_webpage_link)
                success_count += 1
            except Exception as e:
                print(f"❌ Database Transaction Crash: {e}")

    print("\n" + "=" * 50)
    print(f"🏁 CYCLE COMPLETE | New Pushed: {success_count} | Duplicates Bypassed: {skip_count}")
    print("=" * 50)

if __name__ == "__main__":
    run_upmsp_pipeline()
