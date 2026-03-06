# app.py
import os
import logging
import base64
from datetime import datetime
from types import SimpleNamespace
from functools import wraps
from pathlib import Path
from io import BytesIO

from flask import (
    Flask, render_template, request, redirect, url_for, flash, session,
    send_from_directory, send_file, abort, get_flashed_messages, jsonify, current_app
)
from werkzeug.utils import secure_filename

import pdfkit           # pip install pdfkit
# Optional fallback (pure-Python)
try:
    from weasyprint import HTML as WeasyHTML  # pip install WeasyPrint
    WEASY_AVAILABLE = True
except Exception:
    WEASY_AVAILABLE = False

import firebase_admin
from firebase_admin import credentials, firestore

import config
import dotenv

# load .env
dotenv.load_dotenv()

# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)
app.config.from_object(config)
app.secret_key = app.config.get('SECRET_KEY', os.environ.get('FLASK_SECRET', 'dev-secret'))
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# Defaults for folders
UPLOAD_FOLDER = app.config.get('UPLOAD_FOLDER', 'uploads')
GENERATED_FOLDER = app.config.get('GENERATED_FOLDER', 'generated_letters')

# Ensure folders exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GENERATED_FOLDER, exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, 'permission_letters'), exist_ok=True)

# Firestore init
FIRE_CRED_PATH = os.getenv("FIREBASE_CREDENTIALS", None)
if not FIRE_CRED_PATH or not os.path.isfile(FIRE_CRED_PATH):
    raise RuntimeError("Please set FIREBASE_CREDENTIALS env var and point to the serviceAccount JSON file.")

cred = credentials.Certificate(FIRE_CRED_PATH)
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)
db = firestore.client()

# constants
ALLOWED_EXT = {'pdf'}
COLLECTION = "internship_requests"

# helpers
def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

def to_obj(d: dict):
    return SimpleNamespace(**d)

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def inner(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login', next=request.url))
        return f(*args, **kwargs)
    return inner

def image_to_data_uri(path: str) -> str:
    """
    Read an image from disk and return a data URI (base64). Raises FileNotFoundError if missing.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Image not found: {path}")
    with p.open("rb") as f:
        data = f.read()
    ext = p.suffix.lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(data).decode()

# -----------------------
# Public form routes
# -----------------------
@app.route('/', methods=['GET'])
def index():
    # Consume login flashes so admin-login messages don't show on the public form.
    all_msgs = get_flashed_messages(with_categories=True)
    for cat, msg in all_msgs:
        if cat == 'login':
            continue
        else:
            flash(msg, cat)
    return render_template('form.html')

@app.route('/submit', methods=['POST'])
def submit():
    try:
        name = request.form.get('full_name', '').strip()
        college = request.form.get('college_name', '').strip()
        email = request.form.get('email', '').strip()
        start_date = request.form.get('start_date', '').strip()
        end_date = request.form.get('end_date', '').strip()
        duration = request.form.get('duration', '').strip()

        student_year = request.form.get('student_year', '').strip()
        branch = request.form.get('branch', '').strip()
        other_branch = request.form.get('other_branch', '').strip()
        submission_date = request.form.get('submission_date', '').strip()
        if not submission_date:
            submission_date = datetime.utcnow().strftime('%Y-%m-%d')

        # minimal validation
        if not (name and college and email and start_date and end_date and duration):
            flash('All fields are required (including email and dates).', 'danger')
            return redirect(url_for('index'))

        # file
        file = request.files.get('permission_letter')
        if not file or file.filename == '':
            flash('Permission letter (PDF) is required.', 'danger')
            return redirect(url_for('index'))
        if not allowed_file(file.filename):
            flash('Only PDF files are allowed for permission letter.', 'danger')
            return redirect(url_for('index'))

        # save file locally under UPLOAD_FOLDER/permission_letters/
        filename = secure_filename(file.filename)
        ts = datetime.utcnow().strftime('%Y%m%d%H%M%S%f')
        saved_filename = f"{ts}_{filename}"
        upload_base = UPLOAD_FOLDER
        subdir = os.path.join(upload_base, 'permission_letters')
        os.makedirs(subdir, exist_ok=True)
        save_path = os.path.join(subdir, saved_filename)
        file.save(save_path)

        # stored paths (two forms for compatibility)
        permission_pdf_path = os.path.join(upload_base, 'permission_letters', saved_filename)
        permission_path = os.path.join('permission_letters', saved_filename)

        # final branch
        if branch == "Other" and other_branch:
            branch_final = f"Other ({other_branch})"
        else:
            branch_final = branch or other_branch or ""

        # create Firestore doc
        doc_ref = db.collection(COLLECTION).document()
        doc_id = doc_ref.id
        payload = {
            "doc_id": doc_id,
            "student_name": name,
            "college_name": college,
            "email": email,
            "start_date": start_date,
            "end_date": end_date,
            "duration": duration,
            "student_year": student_year,
            "branch": branch_final,
            "other_branch": other_branch,
            "permission_pdf": permission_pdf_path,
            "permission_path": permission_path,
            "status": "pending",
            "submission_date": submission_date,
            "created_at": datetime.utcnow().isoformat()
        }
        doc_ref.set(payload)
        logger.info("Saved application %s for %s", doc_id, name)
        flash('Application submitted. Wait for admin approval.', 'success')
        return redirect(url_for('index'))

    except Exception as e:
        logger.exception("Error submitting application")
        flash(f'Error submitting application: {str(e)}', 'danger')
        return redirect(url_for('index'))

# -----------------------
# Admin auth
# -----------------------
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'GET':
        return render_template('login.html')
    username = request.form.get('username', '')
    password = request.form.get('password', '')
    if username == app.config.get('ADMIN_USERNAME') and password == app.config.get('ADMIN_PASSWORD'):
        session['admin_logged_in'] = True
        session['admin_user'] = username
        flash('Logged in successfully.', 'login')  # login-specific category
        return redirect(url_for('admin_dashboard'))
    else:
        flash('Invalid credentials.', 'danger')
        return redirect(url_for('admin_login'))

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('admin_login'))

# -----------------------
# Admin dashboard & view
# -----------------------
@app.route('/admin', methods=['GET'])
@admin_required
def admin_dashboard():
    try:
        docs = db.collection(COLLECTION).order_by('created_at', direction=firestore.Query.DESCENDING).stream()
        rows = []
        for d in docs:
            data = d.to_dict() or {}
            rows.append({
                'doc_ref_id': d.id,
                'doc_id': data.get('doc_id', d.id),
                'student_name': data.get('student_name', ''),
                'college_name': data.get('college_name', ''),
                'email': data.get('email', ''),
                'start_date': data.get('start_date', ''),
                'end_date': data.get('end_date', ''),
                'duration': data.get('duration', ''),
                'student_year': data.get('student_year', ''),
                'branch': data.get('branch', ''),
                'permission_pdf': data.get('permission_pdf', ''),
                'permission_path': data.get('permission_path', ''),
                'status': data.get('status', 'pending'),
                'submission_date': data.get('submission_date', ''),
                'created_at': data.get('created_at', '')
            })
        objs = [to_obj(r) for r in rows]
        return render_template('admin.html', requests=objs)
    except Exception as e:
        logger.exception("Error loading admin dashboard")
        flash(f"Error loading requests: {str(e)}", "danger")
        return render_template('admin.html', requests=[])

@app.route('/admin/view/<string:req_id>', methods=['GET'])
@admin_required
def admin_view(req_id):
    try:
        # Fetch doc by reference id, fallback to doc_id field
        doc_ref = db.collection(COLLECTION).document(req_id)
        doc = doc_ref.get()
        if not doc.exists:
            query = db.collection(COLLECTION).where('doc_id', '==', req_id).limit(1).stream()
            found = None
            for d in query:
                found = d
                break
            if not found:
                flash('Request not found.', 'danger')
                return redirect(url_for('admin_dashboard'))
            doc_ref = db.collection(COLLECTION).document(found.id)
            doc = doc_ref.get()

        data = doc.to_dict() or {}

        # Normalize permission path for URLs (convert backslashes to forward slashes)
        perm = data.get("permission_path") or data.get("permission_pdf") or ""
        if perm:
            perm = perm.replace("\\", "/").lstrip("uploads/").lstrip("/")

        generated = data.get("generated_letter_filename") or None

        # Prepare variables expected by the template
        return render_template(
            "view_request.html",
            req_id = req_id,
            student_name = data.get("student_name"),
            email = data.get("email"),
            college = data.get("college_name"),
            year = data.get("student_year"),
            branch = data.get("branch"),
            start_date = data.get("start_date"),
            end_date = data.get("end_date"),
            duration = data.get("duration"),
            submission = data.get("submission_date"),
            status = data.get("status"),
            permission_filename = perm or None,
            pdf_url = data.get("pdf_url"),
            generated_filename = generated,
            audit_log = data.get("audit_log", [])
        )

    except Exception as e:
        logger.exception("Error loading request")
        flash('Error loading request. Check server logs.', 'danger')
        return redirect(url_for('admin_dashboard'))

# -----------------------
# Serve uploaded files (robust)
# -----------------------
@app.route('/uploads/<path:filename>')
@admin_required
def uploaded_file(filename):
    try:
        upload_base = Path(UPLOAD_FOLDER).resolve()

        if filename.startswith("uploads/"):
            filename = filename[len("uploads/"):]
        if filename.startswith("/"):
            filename = filename.lstrip("/")

        candidates = [
            upload_base / filename,
            upload_base / Path(filename).name,
            upload_base / "permission_letters" / Path(filename).name
        ]

        for cand in candidates:
            try:
                cand_resolved = cand.resolve()
            except Exception:
                continue
            if str(cand_resolved).startswith(str(upload_base)) and cand_resolved.is_file():
                return send_file(str(cand_resolved), as_attachment=False)

        logger.warning("Uploaded file not found. Tried: %s", [str(c) for c in candidates])
        abort(404)
    except Exception as e:
        logger.exception("Error serving uploaded file %s", filename)
        flash('Unable to open requested file.', 'danger')
        return redirect(url_for('admin_dashboard'))

# -----------------------
# Debug: list all files under uploads (admin-only). Remove when happy.
# -----------------------
@app.route('/debug/list_uploads')
@admin_required
def debug_list_uploads():
    base = Path(UPLOAD_FOLDER).resolve()
    files = []
    for p in base.rglob('*'):
        if p.is_file():
            rel = p.relative_to(base)
            files.append(str(rel))
    return render_template('debug_list.html', files=files)

# -----------------------
# Approve / Reject routes
# -----------------------
@app.route('/admin/approve/<string:req_id>', methods=['POST'])
@admin_required
def admin_approve(req_id):
    try:
        # --- 1) fetch document (by doc ref id, fallback to doc_id field) ---
        doc_ref = db.collection(COLLECTION).document(req_id)
        doc = doc_ref.get()
        if not doc.exists:
            query = db.collection(COLLECTION).where('doc_id', '==', req_id).limit(1).stream()
            found = None
            for d in query:
                found = d
                break
            if not found:
                msg = "Request not found."
                logger.warning("admin_approve: %s %s", req_id, msg)
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({"error": msg}), 404
                flash(msg, 'danger')
                return redirect(url_for('admin_dashboard'))
            doc_ref = db.collection(COLLECTION).document(found.id)
            doc = doc_ref.get()

        data = doc.to_dict() or {}
        logger.info("admin_approve: preparing to approve doc %s (data keys: %s)", doc_ref.id, list(data.keys()))

        # If already approved - return download URL
        if (data.get('status') or '').lower() == 'approved':
            download_url = url_for('download_letter', req_id=doc_ref.id, _external=True)
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"message": "Already approved", "download_url": download_url}), 200
            flash('Request already approved.', 'info')
            return redirect(url_for('admin_view', req_id=doc_ref.id))

        # --- 2) Prepare variables for the letter template ---
        student_name = data.get('student_name') or data.get('name') or ""
        student_id = data.get('student_id') or data.get('roll_no') or data.get('admission_no') or data.get('reg_no') or data.get('email') or ""
        college_name = data.get('college_name') or data.get('college') or ""
        college_address = data.get('college_address') or data.get('college_add') or data.get('college_address_line') or ""
        college_city = data.get('college_city') or data.get('college_city_name') or ""
        reference_date = data.get('reference_date') or data.get('ref_date') or data.get('submission_date') or ""
        duration = data.get('duration') or ""
        start_date = data.get('start_date') or ""
        end_date = data.get('end_date') or ""
        branch = data.get('branch') or ""
        student_year = data.get('student_year') or ""
        letter_date = data.get('letter_date') or data.get('issued_date') or None
        college_phone = data.get('college_phone') or data.get('college_contact') or ""
        supervisor = data.get('supervisor') or ""
        extra_notes = data.get('notes') or data.get('remarks') or ""

        # --- 3) Prepare output paths & header image data-uri ---
        gen_folder = Path(app.config.get('GENERATED_FOLDER', 'generated_letters')).resolve()
        gen_folder.mkdir(parents=True, exist_ok=True)
        pdf_filename = f"offer_{doc_ref.id}.pdf"
        pdf_path = gen_folder / pdf_filename

        # prepare header image as base64 data URI (preferred)
        header_image = None
        # check common static file names - prefer the exact Fjnpa_logo.png the template references
        candidates = [
            Path(app.static_folder) / 'img' / 'Fjnpa_logo.png',
            Path(app.static_folder) / 'img' / 'jnpa_letterhead.jpeg',
            Path(app.static_folder) / 'img' / 'jnpa_letterhead.jpg',
            Path(app.static_folder) / 'img' / 'letter_head.png',
            Path(app.static_folder) / 'img' / 'letterhead.png'
        ]
        for cand in candidates:
            try:
                if cand.exists() and cand.is_file():
                    try:
                        header_image = image_to_data_uri(str(cand))
                        break
                    except Exception as e:
                        logger.warning("Failed to convert header image to data-uri for %s: %s", cand, e)
            except Exception:
                continue

        # issued_date for top-right & dtd
        issued = datetime.utcnow().strftime('%d-%m-%Y')

        # ensure latest template is used (clear Jinja template cache)
        try:
            app.jinja_env.cache.clear()
        except Exception:
            pass

        # --- 4) Render the letter HTML with all fields available (pass header_image & issued_date) ---
        html = render_template(
            'internship_letter.html',
            letter_year = datetime.utcnow().year,
            issued_date = issued,
            header_image = header_image,   # may be None -> template falls back to url_for static
            college_name = college_name,
            college_address = college_address,
            college_city = college_city,
            college_phone = college_phone,
            reference_date = reference_date,
            student_name = student_name,
            student_roll_or_id = student_id,
            branch = branch,
            student_year = student_year,
            duration = duration,
            start_date = start_date,
            end_date = end_date,
            supervisor = supervisor,
            notes = extra_notes
        )

        # --- 5) Generate PDF with pdfkit (wkhtmltopdf) or fallback to WeasyPrint ---
        pdf_generated = False
        wk_path = app.config.get('WKHTMLTOPDF_PATH')  # optional config in config.py

        try:
            pdf_conf = None
            if wk_path:
                pdf_conf = pdfkit.configuration(wkhtmltopdf=wk_path)
            options = {
                'page-size': 'A4',
                'encoding': "UTF-8",
                # local file access not needed when embedding base64, but keep it for safety if static file path used:
                'enable-local-file-access': None,
                'quiet': None
            }
            # Overwrite existing file if present
            pdfkit.from_string(html, str(pdf_path), configuration=pdf_conf, options=options)
            pdf_generated = True
            logger.info("admin_approve: PDF generated via wkhtmltopdf at %s", str(pdf_path))
        except Exception as e:
            logger.warning("pdfkit/wkhtmltopdf generation failed: %s", e)
            # Try WeasyPrint fallback if available
            if WEASY_AVAILABLE:
                try:
                    weasy_html = WeasyHTML(string=html, base_url=request.host_url)
                    weasy_html.write_pdf(str(pdf_path))
                    pdf_generated = True
                    logger.info("admin_approve: PDF generated via WeasyPrint at %s", str(pdf_path))
                except Exception as e2:
                    logger.exception("WeasyPrint fallback also failed: %s", e2)
                    raise
            else:
                # No fallback available -> re-raise original exception
                raise

        if not pdf_generated:
            raise RuntimeError("Failed to generate PDF with both wkhtmltopdf and WeasyPrint.")

        # --- 6) Update Firestore doc with metadata and status ---
        update_payload = {
            'status': 'approved',
            'generated_letter_filename': pdf_filename,
            'generated_letter_path': str(pdf_path),
            'approved_at': datetime.utcnow().isoformat(),
            'issued_date': issued
        }
        doc_ref.update(update_payload)
        logger.info("admin_approve: Firestore updated for %s", doc_ref.id)

        download_url = url_for('download_letter', req_id=doc_ref.id, _external=True)

        # --- 7) Respond appropriately (AJAX or normal form) ---
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"message": "Approved and letter generated.", "download_url": download_url}), 200

        flash('Request approved and letter generated. Use "Download Letter" to save the PDF.', 'success')
        return redirect(url_for('admin_view', req_id=doc_ref.id))

    except Exception as e:
        logger.exception("Error approving request: %s", str(e))
        # revert to pending if partial
        try:
            # doc_ref may not be defined if failure happened early; guard it
            if 'doc_ref' in locals():
                doc_ref.update({'status': 'pending'})
        except Exception:
            pass
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"error": str(e)}), 500
        flash(f"Error approving request: {str(e)}", 'danger')
        # if possible redirect back to admin view for the same req_id
        try:
            return redirect(url_for('admin_view', req_id=req_id))
        except Exception:
            return redirect(url_for('admin_dashboard'))


@app.route('/admin/reject/<string:req_id>', methods=['POST'])
@admin_required
def admin_reject(req_id):
    try:
        doc_ref = db.collection(COLLECTION).document(req_id)
        doc = doc_ref.get()
        if not doc.exists:
            query = db.collection(COLLECTION).where('doc_id', '==', req_id).limit(1).stream()
            found = None
            for d in query:
                found = d
                break
            if not found:
                flash('Request not found.', 'danger')
                return redirect(url_for('admin_dashboard'))
            doc_ref = db.collection(COLLECTION).document(found.id)
            doc = doc_ref.get()

        data = doc.to_dict() or {}
        # If there is a generated file, remove it
        gen_filename = data.get('generated_letter_filename')
        if gen_filename:
            gen_folder = Path(app.config.get('GENERATED_FOLDER', 'generated_letters')).resolve()
            gen_path = gen_folder / gen_filename
            try:
                if gen_path.exists():
                    gen_path.unlink()
                    logger.info("Deleted generated file %s", str(gen_path))
            except Exception as e:
                logger.warning("Failed to delete generated file %s: %s", str(gen_path), e)

        # Update Firestore status and clear generated references
        doc_ref.update({
            'status': 'rejected',
            'generated_letter_filename': None,
            'generated_letter_path': None,
            'approved_at': None,
            'issued_date': None
        })
        flash('Request rejected. Generated letter (if any) removed.', 'info')
        return redirect(url_for('admin_view', req_id=doc_ref.id))
    except Exception as e:
        logger.exception("Error rejecting request")
        flash(f"Error rejecting request: {str(e)}", 'danger')
        return redirect(url_for('admin_dashboard'))

# -----------------------
# Serve generated letters
# -----------------------
@app.route('/generated_letters/<path:filename>')
@admin_required
def serve_generated(filename):
    gen_folder = Path(GENERATED_FOLDER).resolve()
    # Security - ensure filename is safe and inside generated folder
    safe = secure_filename(filename)
    file_path = gen_folder / safe
    try:
        file_res = file_path.resolve()
    except Exception:
        abort(404)
    if not str(file_res).startswith(str(gen_folder)) or not file_res.is_file():
        abort(404)
    return send_file(str(file_res), as_attachment=False)

# ---- Download letter route (admin protected) ----
@app.route('/download_letter/<req_id>')
@admin_required
def download_letter(req_id):
    gen_folder = Path(GENERATED_FOLDER).resolve()

    candidates = [
        gen_folder / f"internship_{req_id}.pdf",
        gen_folder / f"{req_id}.pdf",
        gen_folder / f"offer_{req_id}.pdf",
        gen_folder / f"letter_{req_id}.pdf",
        gen_folder / f"offer_{req_id}.pdf"
    ]

    for p in candidates:
        try:
            p_res = p.resolve()
        except Exception:
            continue
        if str(p_res).startswith(str(gen_folder)) and p_res.is_file():
            return send_file(str(p_res), as_attachment=True, download_name=p_res.name)

    # fallback: check Firestore doc for 'pdf_url' or generated_letter_filename
    try:
        doc = db.collection(COLLECTION).document(req_id).get()
        if doc.exists:
            pdf_url = doc.get('pdf_url')
            gen_name = doc.get('generated_letter_filename')
            if gen_name:
                local = gen_folder / gen_name
                if local.is_file():
                    return send_file(str(local.resolve()), as_attachment=True, download_name=local.name)
            if pdf_url:
                return redirect(pdf_url)
    except Exception:
        logger.exception("Error checking Firestore for pdf_url fallback")

    abort(404)

# Error handler
@app.errorhandler(413)
def request_entity_too_large(error):
    flash('Uploaded file is too large.', 'danger')
    return redirect(url_for('index'))

if __name__ == '__main__':
    logger.info("Starting Flask + Firestore JNPA app")
    # When debugging locally keep debug=True, in production set to False
    app.run(debug=True)
