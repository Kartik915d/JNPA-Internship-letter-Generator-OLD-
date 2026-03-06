@echo off
echo Starting JNPA Internship Portal...

:: Activate venv
call venv\Scripts\activate

:: Set Firebase Credentials
set FIREBASE_CREDENTIALS=C:\Users\karti\Downloads\internship_portal_dummy\internship_portal_dummy\internship-portal-a37d4-firebase-adminsdk-fbsvc-2825b1b4ce.json

:: Run Flask App
python app.py

pause
