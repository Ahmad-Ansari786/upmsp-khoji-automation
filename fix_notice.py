import os
import sys
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from google import genai
from google.genai import types
import json
from json_repair import repair_json
import time
import io
from pypdf import PdfReader, PdfWriter

# =====================================================================
# ⚙️ CONFIGURATION & INITIALIZATION
# =====================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
client = None
if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)

FIREBASE_SERVICE_ACCOUNT_JSON = "serviceAccountKey.json"

if not os.path.exists(FIREBASE_SERVICE_ACCOUNT_JSON):
    print(f"❌ Error: '{FIREBASE_SERVICE_ACCOUNT_JSON}' file nahi mili!")
    sys.exit(1)

cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_JSON)
firebase_admin.initialize_app(cred)
db = firestore.client()

# =====================================================================
# 🛠️ HELPER FUNCTION (Same as updater.py)
# =====================================================================
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

# =====================================================================
# 🤖 GOOGLE AI SUMMARY GENERATOR (100% Identical to updater.py)
# =====================================================================
def generate_ai_data(bytes_payload, mime_type, title, max_retries=3):
    if not GEMINI_API_KEY or not client:
        return None
    
    if len(bytes_payload) > 18 * 1024 * 1024:
        if mime_type == 'application/pdf':
            print("⚠️ PDF is > 18MB. Extracting the first 3 pages to reduce size for AI...")
            try:
                reader = PdfReader(io.BytesIO(bytes_payload))
                writer = PdfWriter()
                
                pages_to_extract = min(3, len(reader.pages))
                for page_num in range(pages_to_extract):
                    writer.add_page(reader.pages[page_num])
                
                reduced_pdf_stream = io.BytesIO()
                writer.write(reduced_pdf_stream)
                bytes_payload = reduced_pdf_stream.getvalue()
                
                if len(bytes_payload) > 18 * 1024 * 1024:
                    print("⚠️ Reduced PDF is still too large. Skipping AI extraction.")
                    return None
            except Exception as pdf_err:
                print(f"⚠️ PDF size reduction failed: {pdf_err}")
                return None
        else:
            print("⚠️ Image/File is too large (>18MB). Skipping AI extraction.")
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
        
    for attempt in range(max_retries):
        try:
            prompt_input = [
                f"Notice Title Context: {title}", 
                types.Part.from_bytes(data=bytes_payload, mime_type=mime_type),
                OPTIMIZED_PROMPT
            ]

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
            error_msg = str(e)
            if "503" in error_msg or "UNAVAILABLE" in error_msg:
                wait_time = (attempt + 1) * 5
                print(f"⚠️ Server Busy (503). Retrying {attempt + 1}/{max_retries} in {wait_time} seconds...")
                time.sleep(wait_time)
                continue 
            else:
                print(f"⚠️ Google AI Extraction Error: {e}")
                return None
                
    print(f"❌ Failed to extract AI data after {max_retries} attempts.")
    return None

# =====================================================================
# 🎯 MANUAL FIXER ENGINE
# =====================================================================
def fix_document():
    doc_id = os.environ.get("TARGET_DOC_ID", "").strip()
    if not doc_id:
        print("❌ No Document ID provided! Process aborted.")
        sys.exit(1)

    print(f"🔍 Locating Document [{doc_id}] in Firestore 'live_notices'...")
    doc_ref = db.collection("live_notices").document(doc_id)
    doc = doc_ref.get()

    if not doc.exists:
        print(f"❌ Document [{doc_id}] not found in database.")
        sys.exit(1)

    data = doc.to_dict()
    file_url = data.get("serverFileUrl")
    title = data.get("title", "")
    
    if not file_url:
        print("❌ No valid 'serverFileUrl' found in this document.")
        sys.exit(1)

    ext = file_url.split('?')[0].split('/')[-1].split('.')[-1].lower()
    mime_type = get_smart_content_type(ext)

    print(f"📥 Downloading file from Cloudflare R2: {file_url}")
    try:
        response = requests.get(file_url, timeout=30)
        if response.status_code != 200:
            print(f"❌ Failed to download file. HTTP Status: {response.status_code}")
            sys.exit(1)
        bytes_payload = response.content
    except Exception as e:
        print(f"❌ Download error: {e}")
        sys.exit(1)

    print("🧠 Running AI Extraction...")
    ai_data = generate_ai_data(bytes_payload, mime_type, title)

    if ai_data:
        print("⚡ Updating Firestore fields directly...")
        doc_ref.update({
            "summary": ai_data.get("summary", ""),
            "englishSummary": ai_data.get("englishSummary", ""),
            "search_keywords": ai_data.get("search_keywords", []),
            "fullText": ai_data.get("fullText", "")
        })
        print(f"✅ Document [{doc_id}] successfully fixed in production!")
    else:
        print("❌ AI Extraction failed. Document was not modified.")

if __name__ == "__main__":
    fix_document()
