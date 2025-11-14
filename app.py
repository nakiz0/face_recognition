import os, io, json, base64
from datetime import datetime, date
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer
from flask_socketio import SocketIO, emit
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename
from PIL import Image
import numpy as np
import face_recognition

# ---------------- config ----------------
BASE = os.path.dirname(os.path.abspath(__file__))
FACE_DIR = os.path.join(BASE, 'face_data')
MODEL_DIR = os.path.join(BASE, 'models')
ENC_FILE = os.path.join(MODEL_DIR, 'encodings.json')
os.makedirs(FACE_DIR, exist_ok=True)
os.makedirs(MODEL_DIR, exist_ok=True)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, '.env'))

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'devsecret')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE, 'db.sqlite3')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Mail
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT','587'))
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])
# Threshold
MATCH_THRESHOLD = float(os.getenv('MATCH_THRESHOLD','0.52'))

db = SQLAlchemy(app)
mail = Mail(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# token serializer for password reset
serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])

def send_verification_email(user):
    """Send email verification link (24 hour expiry)"""
    if not user.email:
        print(f'❌ User {user.username} has no email address')
        return False
    try:
        token = serializer.dumps(user.email, salt='email-verify-salt')
        link = url_for('verify_email', token=token, _external=True)
        msg = Message(subject='Verify your email address', recipients=[user.email])
        msg.html = render_template('verify_email_email.html', username=user.username, verify_link=link)
        mail.send(msg)
        print(f'✅ Verification email sent to {user.email}')
        return True
    except Exception as e:
        print(f'❌ Verification email send failed: {e}')
        app.logger.exception('verify mail failed: %s', e)
        return False

def send_reset_email(user):
    if not user.email:
        print(f'❌ User {user.username} has no email address')
        return False
    try:
        token = serializer.dumps(user.email, salt='password-reset-salt')
        link = url_for('reset_password', token=token, _external=True)
        msg = Message(subject='Password reset request', recipients=[user.email])
        msg.html = render_template('password_reset_email.html', username=user.username, reset_link=link)
        mail.send(msg)
        print(f'✅ Reset email sent to {user.email}')
        return True
    except Exception as e:
        print(f'❌ Email send failed: {e}')
        app.logger.exception('reset mail failed: %s', e)
        return False

# ---------------- models ----------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)  # use hashing in prod
    email = db.Column(db.String(200))
    email_verified = db.Column(db.Boolean, default=False)  # True only after email confirmation
    role = db.Column(db.String(20), default='student')  # admin | teacher | student
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    subject = db.Column(db.String(150))
    date = db.Column(db.String(20))  # yyyy-mm-dd
    time = db.Column(db.String(8))
    status = db.Column(db.String(20), default='Present')

class Timetable(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.String(10))   # Monday
    start = db.Column(db.String(5))  # HH:MM
    end = db.Column(db.String(5))
    subject = db.Column(db.String(120))

# ---------------- encodings helpers ----------------
def load_encodings():
    if not os.path.exists(ENC_FILE):
        return {"names": [], "encodings": []}
    with open(ENC_FILE,'r') as f:
        data = json.load(f)
    encs = [np.array(e) for e in data.get('encodings',[])]
    return {"names": data.get('names',[]), "encodings": encs}

def save_encodings(names, encodings):
    data = {"names": names, "encodings":[e.tolist() for e in encodings]}
    with open(ENC_FILE,'w') as f:
        json.dump(data,f)

def build_encodings_from_images():
    names=[]; encs=[]
    for username in os.listdir(FACE_DIR):
        folder = os.path.join(FACE_DIR, username)
        if not os.path.isdir(folder): continue
        for fname in os.listdir(folder):
            if fname.lower().endswith(('.jpg','.jpeg','.png')):
                path = os.path.join(folder,fname)
                try:
                    img = face_recognition.load_image_file(path)
                    d = face_recognition.face_encodings(img)
                    if d:
                        encs.append(d[0]); names.append(username)
                except Exception as e:
                    app.logger.warning('skip %s: %s', path, e)
    save_encodings(names, encs)
    return names, encs

# pre-load encodings
ENC = load_encodings()

# ---------------- email helper ----------------
def send_attendance_email_to_user(user:User, att_date:str, subject_name:str):
    if not user.email: return False
    try:
        msg = Message(subject=f'Attendance marked: {att_date}',
                      recipients=[user.email])
        msg.html = render_template('email_template.html',
                                   username=user.username,
                                   date=att_date,
                                   subject=subject_name,
                                   organization='Your Institute')
        mail.send(msg)
        return True
    except Exception as e:
        app.logger.exception('Mail send failed: %s', e)
        return False

# ---------------- views ----------------
@app.route('/')
def index():
    if 'user_id' in session:
        u = User.query.get(session['user_id'])
        if u.role=='admin': return redirect(url_for('admin_dashboard'))
        if u.role=='teacher': return redirect(url_for('teacher_take_attendance'))
        return redirect(url_for('student_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    error=None
    if request.method=='POST':
        u = request.form['username']; p = request.form['password']
        user = User.query.filter_by(username=u, password=p).first()
        if user:
            # Check if email is verified
            if user.email and not user.email_verified:
                error = '⚠️ Please verify your email before logging in. Check your inbox.'
            else:
                session['user_id']=user.id
                return redirect(url_for('index'))
        else:
            error='Invalid credentials'
    return render_template('login.html', error=error)


# Password reset - request
@app.route('/password_reset', methods=['GET','POST'])
def password_reset_request():
    message=None
    if request.method=='POST':
        email = request.form['email']
        user = User.query.filter_by(email=email).first()
        if user:
            send_reset_email(user)
        # Do not reveal whether email exists
        message = 'If your email is in our system, a reset link has been sent.'
        return render_template('password_reset_request.html', message=message)
    return render_template('password_reset_request.html')


# Email verification - token link
@app.route('/verify_email/<token>')
def verify_email(token):
    try:
        email = serializer.loads(token, salt='email-verify-salt', max_age=86400)  # 24 hour expiry
    except Exception as e:
        return render_template('verify_email_confirm.html', error='Invalid or expired verification link.')
    
    user = User.query.filter_by(email=email).first()
    if not user:
        return render_template('verify_email_confirm.html', error='User not found.')
    
    if user.email_verified:
        return render_template('verify_email_confirm.html', message='✅ Your email is already verified!')
    
    # Mark email as verified
    user.email_verified = True
    db.session.commit()
    return render_template('verify_email_confirm.html', message='✅ Email verified successfully! You can now login.')


# Password reset - token link
@app.route('/reset_password/<token>', methods=['GET','POST'])
def reset_password(token):
    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=3600)
    except Exception as e:
        return render_template('password_reset_form.html', error='Invalid or expired token.')

    user = User.query.filter_by(email=email).first()
    if not user:
        return render_template('password_reset_form.html', error='User not found.')

    if request.method=='POST':
        pwd = request.form['password']
        conf = request.form['confirm']
        if pwd != conf:
            return render_template('password_reset_form.html', error='Passwords do not match.')
        # Update password (note: passwords are stored plaintext in this app)
        user.password = pwd
        db.session.commit()
        return render_template('password_reset_form.html', error='Password updated. You can now login.')

    return render_template('password_reset_form.html')


# Test email route (admin only)
@app.route('/admin/test_email/<username>')
def test_email(username):
    uid = session.get('user_id')
    admin = User.query.get(uid)
    if not admin or admin.role != 'admin':
        return jsonify({'ok': False, 'error': 'Admin only'})
    
    user = User.query.filter_by(username=username).first()
    if not user or not user.email:
        return jsonify({'ok': False, 'error': 'User not found or no email'})
    
    success = send_reset_email(user)
    if success:
        return jsonify({'ok': True, 'message': f'Test email sent to {user.email}'})
    else:
        return jsonify({'ok': False, 'error': 'Email send failed. Check terminal logs.'})


# Admin reset user password
@app.route('/admin/reset_user_password/<int:user_id>', methods=['POST'])
def admin_reset_password(user_id):
    uid = session.get('user_id')
    admin = User.query.get(uid)
    if not admin or admin.role != 'admin':
        return jsonify({'ok': False, 'error': 'Unauthorized'})
    
    user = User.query.get(user_id)
    if not user:
        return jsonify({'ok': False, 'error': 'User not found'})
    
    new_pwd = request.form.get('password')
    if not new_pwd or len(new_pwd) < 4:
        return jsonify({'ok': False, 'error': 'Password must be at least 4 characters'})
    
    user.password = new_pwd
    db.session.commit()
    return jsonify({'ok': True, 'message': f'Password for {user.username} reset to: {new_pwd}'})


# Admin dashboard (similar sections to screenshot)
@app.route('/admin')
def admin_dashboard():
    uid = session.get('user_id')
    if not uid: return redirect(url_for('login'))
    u = User.query.get(uid)
    if not u or u.role!='admin': return redirect(url_for('login'))
    students = User.query.filter_by(role='student').all()
    attendance = Attendance.query.order_by(Attendance.date.desc()).limit(200).all()
    timetable = Timetable.query.order_by(Timetable.day, Timetable.start).all()
    today = date.today().isoformat()
    return render_template('admin_dashboard.html', students=students, attendance=attendance, timetable=timetable, today=today)

# Add timetable entry
@app.route('/admin/timetable/add', methods=['POST'])
def admin_add_timetable():
    uid = session.get('user_id')
    u = User.query.get(uid)
    if not u or u.role != 'admin':
        return redirect(url_for('login'))
    day = request.form['day']
    start = request.form['start']
    end = request.form['end']
    subj = request.form['subject']
    t = Timetable(day=day, start=start, end=end, subject=subj)
    db.session.add(t)
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

# Upload multiple images for a student (admin)
@app.route('/admin/upload_images', methods=['POST'])
def admin_upload_images():
    uid = session.get('user_id')
    u = User.query.get(uid)
    if not u or u.role != 'admin':
        return redirect(url_for('login'))
    username = request.form['username']
    files = request.files.getlist('images')
    folder = os.path.join(FACE_DIR, username)
    os.makedirs(folder, exist_ok=True)
    for f in files:
        fname = secure_filename(f.filename)
        f.save(os.path.join(folder, fname))
    # rebuild encodings
    build_encodings_from_images()
    global ENC
    ENC = load_encodings()
    return redirect(url_for('admin_dashboard'))

# Admin manual mark attendance
@app.route('/admin/mark', methods=['POST'])
def admin_mark():
    uid = session.get('user_id')
    u = User.query.get(uid)
    if not u or u.role != 'admin':
        return redirect(url_for('login'))
    username = request.form['username']
    subj = request.form['subject']
    dt = request.form.get('date') or date.today().isoformat()
    # Validate: Only allow marking for today and past dates, not future dates
    try:
        selected_date = datetime.strptime(dt, '%Y-%m-%d').date()
        if selected_date > date.today():
            students = User.query.filter_by(role='student').all()
            attendance = Attendance.query.order_by(Attendance.date.desc()).limit(200).all()
            timetable = Timetable.query.order_by(Timetable.day, Timetable.start).all()
            return render_template('admin_dashboard.html', students=students, attendance=attendance, timetable=timetable, error='❌ Cannot mark attendance for future dates. Only today and past dates are allowed.')
    except ValueError:
        # Invalid date format
        students = User.query.filter_by(role='student').all()
        attendance = Attendance.query.order_by(Attendance.date.desc()).limit(200).all()
        timetable = Timetable.query.order_by(Timetable.day, Timetable.start).all()
        return render_template('admin_dashboard.html', students=students, attendance=attendance, timetable=timetable, error='❌ Invalid date format. Please use YYYY-MM-DD.')

    user = User.query.filter_by(username=username).first()
    if user:
        att = Attendance(user_id=user.id, subject=subj, date=dt, time=datetime.now().strftime('%H:%M:%S'), status='Present')
        db.session.add(att)
        db.session.commit()
    return redirect(url_for('admin_dashboard'))

# Teacher - take attendance page
@app.route('/teacher/take')
def teacher_take_attendance():
    uid = session.get('user_id')
    u = User.query.get(uid)
    if not u or u.role not in ('teacher', 'admin'):
        return redirect(url_for('login'))
    # find current subject by timetable
    todayname = datetime.today().strftime('%A')
    nowt = datetime.now().time()
    todays = Timetable.query.filter_by(day=todayname).all()
    current_subject = None
    current_subject_time = ''
    for t in todays:
        s = datetime.strptime(t.start, '%H:%M').time()
        e = datetime.strptime(t.end, '%H:%M').time()
        if s <= nowt <= e:
            current_subject = t.subject
            current_subject_time = f"{t.start} - {t.end}"
            break

    return render_template('teacher_take_attendance.html', subject=current_subject or '', subject_time=current_subject_time, timetable=todays)

# API recognize: receives base64 frame, marks attendance if matches
@app.route('/api/recognize', methods=['POST'])
def api_recognize():
    payload = request.json
    frame_b64 = payload.get('frame')
    subject = payload.get('subject') or 'General'
    if not frame_b64:
        return jsonify({'ok': False, 'error': 'no_frame'})
    header, data = frame_b64.split(',', 1) if ',' in frame_b64 else ('', frame_b64)
    img_bytes = base64.b64decode(data)
    img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
    rgb = np.array(img)  # RGB
    face_locations = face_recognition.face_locations(rgb)
    face_encodings = face_recognition.face_encodings(rgb, face_locations)
    global ENC
    if not ENC['encodings']:
        return jsonify({'ok': False, 'error': 'no_known_faces'})
    for enc in face_encodings:
        dists = face_recognition.face_distance(ENC['encodings'], enc)
        if len(dists) == 0:
            continue
        best = int(np.argmin(dists))
        if dists[best] <= MATCH_THRESHOLD:
            username = ENC['names'][best]
            user = User.query.filter_by(username=username).first()
            if not user:
                continue
            today = date.today().isoformat()
            exists = Attendance.query.filter_by(user_id=user.id, date=today, subject=subject, status='Present').first()
            if exists:
                return jsonify({'ok': True, 'marked': False, 'reason': 'already_marked', 'username': username})
            nowt = datetime.now().strftime('%H:%M:%S')
            att = Attendance(user_id=user.id, subject=subject, date=today, time=nowt, status='Present')
            db.session.add(att)
            db.session.commit()
            # emit socket event so teacher/admin/student dashboards can update in real time
            socketio.emit('attendance_marked', {'username': username, 'subject': subject, 'date': today, 'time': nowt})
            # send email
            send_attendance_email_to_user(user, today, subject)
            return jsonify({'ok': True, 'marked': True, 'username': username})
    return jsonify({'ok': True, 'marked': False, 'reason': 'no_match'})

# API train: accepts frames for a username, saves images and rebuilds encodings
@app.route('/api/train', methods=['POST'])
def api_train():
    payload = request.json
    username = payload.get('username')
    frames = payload.get('frames', [])
    if not username or not frames:
        return jsonify({'ok': False, 'error': 'need_username_frames'})
    folder = os.path.join(FACE_DIR, username)
    os.makedirs(folder, exist_ok=True)
    saved = 0
    for idx, b64 in enumerate(frames):
        h, d = b64.split(',', 1) if ',' in b64 else ('', b64)
        data = base64.b64decode(d)
        fname = f'{int(datetime.utcnow().timestamp()*1000)}_{idx}.jpg'
        with open(os.path.join(folder, fname), 'wb') as f:
            f.write(data)
        saved += 1
    build_encodings_from_images()
    global ENC
    ENC = load_encodings()
    return jsonify({'ok':True,'saved':saved})

# list student attendance (student dashboard)
@app.route('/student')
def student_dashboard():
    uid = session.get('user_id')
    if not uid:
        return redirect(url_for('login'))
    user = User.query.get(uid)
    if user.role != 'student':
        return redirect(url_for('login'))
    atts = Attendance.query.filter_by(user_id=user.id).order_by(Attendance.date.desc()).all()
    return render_template('student_dashboard.html', user=user, attendance=atts)

# serve face images
@app.route('/face_data/<path:filename>')
def face_file(filename):
    return send_from_directory(FACE_DIR, filename)

# Delete user (admin only)
@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    uid = session.get('user_id')
    admin = User.query.get(uid)
    if not admin or admin.role != 'admin':
        return jsonify({'ok': False, 'error': 'Unauthorized'})
    
    user = User.query.get(user_id)
    if user:
        # Don't allow deleting admin accounts
        if user.role == 'admin':
            return jsonify({'ok': False, 'error': 'Cannot delete admin users'})
        
        # Delete all attendance records for this user
        Attendance.query.filter_by(user_id=user_id).delete()
        
        # Delete user face data from filesystem
        user_folder = os.path.join(FACE_DIR, user.username)
        if os.path.exists(user_folder):
            import shutil
            shutil.rmtree(user_folder)
        
        # Delete user from database
        db.session.delete(user)
        db.session.commit()
        
        # Rebuild encodings
        build_encodings_from_images()
        global ENC
        ENC = load_encodings()
        
        return jsonify({'ok': True, 'message': f'User {user.username} deleted successfully'})
    
    return jsonify({'ok': False, 'error': 'User not found'})

# ---------- init & run -------------
with app.app_context():
    db.create_all()
    # initial build encodings if not exist
    enc = load_encodings()
    if not enc['encodings']:
        build_encodings_from_images()
        ENC = load_encodings()

if __name__ == '__main__':
    # use socketio server (eventlet)
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
