import os
from dotenv import load_dotenv
load_dotenv()
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SECRET_KEY = os.getenv('SECRET_KEY','dev-secret')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME','admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD','admin123')  # plain for dummy app
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads', 'permission_letters')
GENERATED_FOLDER = os.path.join(BASE_DIR, 'generated_letters')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GENERATED_FOLDER, exist_ok=True)
WKHTMLTOPDF_PATH = r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe"
