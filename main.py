from flask import Flask, render_template, request, flash, redirect, url_for, session, send_from_directory, send_file
from models import Users, Attendance, Announcement, Schedule, db
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta, date
import os
import secrets  # Do generowania bezpiecznych kodów 2FA i haseł
from dotenv import load_dotenv
import pandas as pd
import io
from werkzeug.security import generate_password_hash, check_password_hash
import urllib.request
import json
from flask_mail import Mail, Message
from threading import Thread

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("TAJNE_HASLO")
db_url = os.getenv("DATABASE_URL", "sqlite:///ministranci.db")

if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.permanent_session_lifetime = timedelta(minutes=15)

app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 280,
    "pool_pre_ping": True
}


app.config['MAIL_SERVER'] = 'smtp.sendgrid.net'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USE_SSL'] = False
app.config['MAIL_USERNAME'] = 'apikey'  # To pole MUSI mieć dokładnie taki tekst: 'apikey'
app.config['MAIL_PASSWORD'] = os.getenv("EMAIL_PASSWORD")  # Tutaj Render wstawi Twój klucz SG....
app.config['MAIL_DEFAULT_SENDER'] = os.getenv("EMAIL_USER")   # Twój zweryfikowany mail
app.config['MAIL_SUPPRESS_SEND'] = False
app.config['FAIL_SILENTLY'] = True



mail = Mail(app)
db.init_app(app)

@app.template_filter('datetimeformat')
def datetimeformat(value, format='%Y-%m-%d %H:%M'):
    if value is None:
        return ""
    return value.strftime(format)

@app.template_filter('dateformat')
def dateformat(value, format='%Y-%m-%d'):
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            return value
    return value.strftime(format)

@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

def format_datetime_pl(dt):
    if not dt: return ""
    dni = ["pon.", "wt.", "śr.", "czw.", "pt.", "sob.", "nd."]
    godz_min = dt.strftime("%H:%M")
    return f"{dni[dt.weekday()]} {dt.day}.{dt.month} o {godz_min}"

app.jinja_env.filters['datetime_pl'] = format_datetime_pl

def send_async_email(app, msg):
    with app.app_context():
        try:
            mail.send(msg)
        except Exception as e:
            print(f"--- [BŁĄD SMTP W TLE] Nie udało się wysłać maila: {e} ---")

@app.route('/')
def login_page():
    if 'user_id' in session or 'user_role' in session:
        return redirect(url_for('dashboard_page'))
    return render_template('login.html')

@app.route("/auth_process", methods=['POST'])
def auth_process():
    action = request.form.get("action")
    username = request.form.get("username")
    password = request.form.get("haslo")
    
    env_admin_name = os.getenv("admin_name")
    env_admin_pass = os.getenv("admin_password") # Zostawiamy w .env jako surowe zabezpieczenie pierwszego startu
    admin_target_email = os.getenv("ADMIN_TARGET_EMAIL") # Docelowy mail admina na kody i resety

    if action == "login":
        # 1. LOGOWANIE GLOWNEGO ADMINA
        if username == env_admin_name:
            admin_in_db = Users.query.filter_by(username=username).first()
            
            # Pobieranie IP z uwzględnieniem proxy Rendera
            if request.headers.getlist("X-Forwarded-For"):
                user_ip = request.headers.getlist("X-Forwarded-For")[0].split(',')[0].strip()
            else:
                user_ip = request.remote_addr

            if not admin_in_db:
                hashed_admin_pass = generate_password_hash(env_admin_pass, method='pbkdf2:sha256')
                admin_in_db = Users(
                    imie="Główny", 
                    nazwisko="Szef", 
                    username=username, 
                    password=hashed_admin_pass, 
                    role='admin', 
                    uproszczony=False
                )
                db.session.add(admin_in_db)
                db.session.commit()

            # SPRAWDZAMY HASŁO
            if check_password_hash(admin_in_db.password, password):
                # Kod 2FA (jeśli hasło jest poprawne)
                kod_2fa = ''.join([str(secrets.randbelow(10)) for _ in range(12)])
                admin_in_db.two_factor_code = kod_2fa
                admin_in_db.two_factor_expiry = datetime.now() + timedelta(minutes=5)
                db.session.commit()

                try:
                    msg = Message("Twój 12-cyfrowy kod weryfikacyjny 2FA", recipients=[admin_target_email])
                    msg.body = f"Witaj Szefie!\n\nKtoś próbuje zalogować się na konto administratora.\nOto Twój kod weryfikacyjny: {kod_2fa}\n\nKod wygaśnie za 5 minut."
                    thr = Thread(target=send_async_email, args=[app, msg])
                    thr.start()
                    
                    session['pending_admin_id'] = admin_in_db.id
                    return redirect(url_for('two_factor_page'))
                except Exception as e:
                    flash("Błąd wysyłania maila z kodem 2FA. Sprawdź konfigurację.", "danger")
                    return redirect(url_for('login_page'))
            else:
                # !!! NIEUDANA PRÓBA LOGOWANIA NA KONTO ADMINA !!!
                teraz = datetime.now()
                data_str = teraz.strftime("%d-%m-%Y")
                godzina_str = teraz.strftime("%H:%M:%S")
                user_agent = request.headers.get('User-Agent', 'Nieznana przeglądarka')
                
                lokalizacja_info = "Brak danych (Localhost / Błąd API)"
                if user_ip and user_ip != "127.0.0.1":
                    try:
                        with urllib.request.urlopen(f"http://ip-api.com/json/{user_ip}?fields=status,country,regionName,city,lat,lon", timeout=2) as url:
                            geo_data = json.loads(url.read().decode())
                            if geo_data.get("status") == "success":
                                lokalizacja_info = f"{geo_data.get('city')}, {geo_data.get('regionName')} ({geo_data.get('country')}) | Współrzędne GPS: {geo_data.get('lat')}, {geo_data.get('lon')}"
                    except Exception as e:
                        print(f"Błąd pobierania geolokalizacji IP: {e}")

                # TWORZYMY WIADOMOŚĆ
                alert_msg = Message("⚠️ ALERT BEZPIECZEŃSTWA: Nieudane logowanie na Admina!", recipients=[admin_target_email])
                alert_msg.body = (
                    f"UWAGA SZEFIE!\n\n"
                    f"Wykryto NIEUDANĄ próbę zalogowania na konto głównego administratora ({username}).\n\n"
                    f"📅 Data: {data_str}\n"
                    f"⏰ Godzina: {godzina_str}\n"
                    f"🌐 Adres IP: {user_ip}\n"
                    f"📍 Geolokalizacja IP: {lokalizacja_info}\n"
                    f"📱 Urządzenie/Przeglądarka: {user_agent}\n\n"
                    f"Jeśli to nie Ty, ktoś próbuje odgadnąć Twoje hasło!"
                )
                
                # URUCHAMIAMY WYSYŁANIE W TLE - STRONA NIE BĘDZIE CZEKAĆ NA SERWER GMAILA!
                thr = Thread(target=send_async_email, args=[app, alert_msg])
                thr.start()

                flash("Błędne hasło administratora.", "danger")
                return redirect(url_for('login_page'))

    elif action == "register":
        user = Users.query.filter_by(username=username).first()
        if user or username == env_admin_name:
            flash("Ta nazwa jest zajęta!", "danger")
        else:
            # Haszujemy hasło nowego użytkownika przed zapisem do bazy!
            hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
            
            new_user = Users(
                imie=request.form.get("imie"), 
                nazwisko=request.form.get("nazwisko"), 
                username=username, 
                password=hashed_password, # Zapisujemy bezpieczny nieodwracalny hasz
                role='user',
                uproszczony=False
            )
            db.session.add(new_user)
            db.session.commit()
            flash("Konto stworzone! Możesz się zalogować.", "success")
        return redirect(url_for('login_page'))
    
@app.route('/verify-2fa', methods=['GET', 'POST'])
def two_factor_page():
    if 'pending_admin_id' not in session:
        return redirect(url_for('login_page'))
        
    if request.method == 'POST':
        wpisany_kod = request.form.get("kod_2fa").strip()
        admin = Users.query.get(session['pending_admin_id'])
        
        if admin and admin.two_factor_code == wpisany_kod and datetime.now() < admin.two_factor_expiry:
            # Czyszczenie danych 2FA po sukcesie
            admin.two_factor_code = None
            admin.two_factor_expiry = None
            db.session.commit()
            
            # Pełne zalogowanie sesji
            session.clear()
            session['user_id'] = admin.id
            session['username'] = admin.username
            session['user_role'] = 'admin'
            session['uproszczony'] = False
            
            flash("Autoryzacja 2FA pomyślna. Witaj Szefie!", "success")
            return redirect(url_for('admin_page'))
        else:
            flash("Niepoprawny lub wygasły kod 2FA!", "danger")
            
    return render_template('verify_2fa.html')

@app.route('/reset-admin-password', methods=['POST'])
def reset_admin_password():
    username = request.form.get("username")
    env_admin_name = os.getenv("admin_name")
    admin_target_email = os.getenv("ADMIN_TARGET_EMAIL")
    
    if username == env_admin_name:
        admin = Users.query.filter_by(username=username).first()
        if admin:
            # Generujemy nowe losowe bezpieczne hasło tekstowe
            nowe_losowe_haslo = secrets.token_hex(6) # Wygeneruje 12-znakowe losowe hasło tekstowe
            
            # Zapisujemy bezpieczny HASZ nowego hasła w bazie
            admin.password = generate_password_hash(nowe_losowe_haslo, method='pbkdf2:sha256')
            db.session.commit()
            
            # Wysyłamy SUROWE nowe hasło tylko na wskazany e-mail admina
            try:
                msg = Message("Zresetowane Hasło Administratora", recipients=[admin_target_email])
                msg.body = f"Szefie, oto Twoje nowe, wygenerowane hasło do systemu: {nowe_losowe_haslo}\n\nZaloguj się nim, a stare hasło z pliku .env przestało działać w bazie."
                thr = Thread(target=send_async_email, args=[app, msg])
                thr.start()
                flash("Nowe hasło zostało wysłane na tajny e-mail administratora!", "success")
            except Exception as e:
                flash("Błąd podczas wysyłania wiadomości e-mail.", "danger")
        else:
            flash("Admin nie został jeszcze zainicjalizowany w bazie danych.", "danger")
    else:
        flash("Ta opcja szybkiego resetu e-mail jest dostępna wyłącznie dla Konta Głównego Admina.", "danger")
        
    return redirect(url_for('login_page'))

@app.route('/add_attendance', methods=['POST'])
def add_attendance():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))

    data_str = request.form.get("date")
    typ_mszy = request.form.get("typ_mszy")
    nazwa_inna = request.form.get("nazwa_inna")
    godzina = request.form.get("godzina")
    
    try:
        wybrana_data = date.fromisoformat(data_str)
        dzisiaj = date.today()
        wczoraj = dzisiaj - timedelta(days=1)
        hard_limit = date(2026, 4, 12)

        if wybrana_data > dzisiaj or wybrana_data < max(wczoraj, hard_limit):
            flash("Nieprawidłowa data!", "danger")
            return redirect(url_for('dashboard_page'))

        istniejaca = Attendance.query.filter_by(
            user_id=session['user_id'], 
            data_sluzby=wybrana_data, 
            godzina=godzina
        ).first()

        if istniejaca:
            flash("Nie możesz służyć w dwóch miejscach naraz! Masz już zgłoszoną służbę w ten dzień o tej samej godzinie.", "danger")
            return redirect(url_for('dashboard_page'))

        nowa = Attendance(
            user_id=session['user_id'],
            data_sluzby=wybrana_data,
            typ_mszy=typ_mszy,
            nazwa_inna=nazwa_inna if typ_mszy == 'inna' else None,
            godzina=godzina
        )
        db.session.add(nowa)
        db.session.commit()
        flash("Obecność zapisana!", "success")
    except Exception as e:
        db.session.rollback()
        flash("Wystąpił błąd podczas dodawania wpisu.", "danger")

    return redirect(url_for('dashboard_page'))

@app.route('/admin/delete_user/<int:id>')
def delete_user(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    user_to_del = Users.query.get_or_404(id)
    Attendance.query.filter_by(user_id=id).delete()
    Schedule.query.filter_by(user_id=id).delete()
    db.session.delete(user_to_del)
    db.session.commit()
    flash(f"Użytkownik {user_to_del.username} usunięty.", "success")
    return redirect(url_for('admin_page'))

@app.route('/admin/edit_user/<int:id>', methods=['POST'])
def edit_user(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    u = Users.query.get_or_404(id)
    u.imie = request.form.get('imie')
    u.nazwisko = request.form.get('nazwisko')
    u.username = request.form.get('username')
    u.password = request.form.get('password')
    u.role = request.form.get('role') 
    u.uproszczony = True if request.form.get('uproszczony') == 'on' else False
    
    try:
        db.session.commit()
        flash("Dane użytkownika zaktualizowane!", "success")
    except:
        db.session.rollback()
        flash("Błąd podczas edycji użytkownika.", "danger")
    return redirect(url_for('admin_page'))

@app.route('/admin/delete/<int:id>')
def delete_entry(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    entry = Attendance.query.get_or_404(id)
    try:
        db.session.delete(entry)
        db.session.commit()
        flash("Wpis usunięty pomyślnie.", "success")
    except:
        db.session.rollback()
        flash("Nie udało się usunąć wpisu.", "danger")
    return redirect(url_for('admin_page'))

@app.route('/admin/edit/<int:id>', methods=['POST'])
def edit_entry(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    entry = Attendance.query.get_or_404(id)
    try:
        entry.data_sluzby = date.fromisoformat(request.form.get('date'))
        entry.godzina = request.form.get('godzina')
        entry.typ_mszy = request.form.get('typ_mszy')
        entry.nazwa_inna = request.form.get('nazwa_inna') if entry.typ_mszy == 'inna' else None
        db.session.commit()
        flash("Dane zostały zaktualizowane.", "success")
    except:
        db.session.rollback()
        flash("Błąd podczas zapisywania zmian.", "danger")
    return redirect(url_for('admin_page'))

@app.route('/admin/add_attendance_admin', methods=['POST'])
def add_attendance_admin():
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    user_id = request.form.get("user_id")
    data_str = request.form.get("date")
    typ_mszy = request.form.get("typ_mszy")
    nazwa_inna = request.form.get("nazwa_inna")
    godzina = request.form.get("godzina")

    try:
        wybrana_data = date.fromisoformat(data_str)
        nowa_sluzba = Attendance(
            user_id=user_id, 
            data_sluzby=wybrana_data, 
            typ_mszy=typ_mszy,
            nazwa_inna=nazwa_inna if typ_mszy == 'inna' else None, 
            godzina=godzina
        )
        db.session.add(nowa_sluzba)
        db.session.commit()
        flash("Służba została dodana przez Szefa!", "success")
    except Exception as e:
        db.session.rollback()
        flash("Wystąpił błąd podczas dodawania służby.", "danger")

    return redirect(url_for('admin_page'))

@app.route('/admin/add_announcement', methods=['POST'])
def add_announcement():
    if session.get('user_role') not in ['admin', 'ksiądz']:
        return redirect(url_for('login_page'))
    nowe = Announcement(tresc=request.form.get('tresc'))
    db.session.add(nowe)
    db.session.commit()
    flash("Ogłoszenie dodane!", "success")
    if session.get('user_role') == 'ksiądz':
        return redirect(url_for('ksDash'))
    return redirect(url_for('admin_page'))

@app.route('/admin/edit_announcement/<int:id>', methods=['POST'])
def edit_announcement(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    ogloszenie = Announcement.query.get_or_404(id)
    ogloszenie.tresc = request.form.get('tresc')
    db.session.commit()
    return redirect(url_for('admin_page'))

@app.route('/admin/delete_announcement/<int:id>')
def delete_announcement(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    ogloszenie = Announcement.query.get_or_404(id)
    db.session.delete(ogloszenie)
    db.session.commit()
    return redirect(url_for('admin_page'))

@app.route('/admin/add_schedule', methods=['POST'])
def add_schedule():
    if session.get('user_role') not in ['admin', 'ksiądz']:
        return redirect(url_for('login_page'))
    nowy_dyzur = Schedule(
        user_id=request.form.get("user_id"),
        dzien_tygodnia=request.form.get("dzien"),
        godzina=request.form.get("godzina")
    )
    db.session.add(nowy_dyzur)
    db.session.commit()
    flash("Dodano dyżur do planu!", "success")
    if session.get('user_role') == 'ksiądz':
        return redirect(url_for('ksDash'))
    return redirect(url_for('admin_page'))

@app.route('/admin/delete_schedule/<int:id>')
def delete_schedule(id):
    if session.get('user_role') not in ['admin', 'ksiądz']:
        return redirect(url_for('login_page'))
    dyzur = Schedule.query.get(id)
    if dyzur:
        db.session.delete(dyzur)
        db.session.commit()
        flash("Usunięto dyżur z planu.", "success")
    if session.get('user_role') == 'ksiądz':
        return redirect(url_for('ksDash'))
    return redirect(url_for('admin_page'))

@app.route('/admin')
def admin_page():
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    
    all_attendance = db.session.query(Attendance, Users).join(Users).order_by(
        Attendance.data_sluzby.desc(), Attendance.godzina.desc()
    ).all()
    all_users = Users.query.all()
    all_announcements = Announcement.query.order_by(Announcement.data_wystawienia.desc()).all()
    schedules = db.session.query(Schedule, Users).join(Users, Schedule.user_id == Users.id).all()
    
    plan_tygodnia = {'Poniedziałek': {}, 'Wtorek': {}, 'Środa': {}, 'Czwartek': {}, 'Piątek': {}, 'Sobota': {}, 'Niedziela': {}}
    plan_liczniki = {}

    for sch, u in schedules:
        dzien = sch.dzien_tygodnia
        godzina = sch.godzina
        if godzina not in plan_tygodnia[dzien]:
            plan_tygodnia[dzien][godzina] = []
        plan_tygodnia[dzien][godzina].append({'id': sch.id, 'user': u})
        
    for dzien in plan_tygodnia:
        plan_tygodnia[dzien] = dict(sorted(plan_tygodnia[dzien].items()))
        count = sum(len(servers) for servers in plan_tygodnia[dzien].values())
        plan_liczniki[dzien] = count
    
    user_atts_map = {u.id: [] for u in all_users}
    for att, usr in all_attendance:
        if usr.id in user_atts_map:
            user_atts_map[usr.id].append(att)

    user_stats = []
    for u in all_users:
        his_atts = user_atts_map.get(u.id, [])
        total = len(his_atts)
        morning = sum(1 for a in his_atts if a.typ_mszy == 'poranna')
        evening = sum(1 for a in his_atts if a.typ_mszy == 'wieczorna')
        other = total - (morning + evening)
        
        user_stats.append({
            'username': u.username,
            'imie': u.imie,
            'nazwisko': u.nazwisko,
            'full_name': f"{u.imie} {u.nazwisko}",
            'uproszczony': u.uproszczony,
            'total': total, 
            'morning': morning, 
            'evening': evening, 
            'other': other
        })
    
    return render_template(
        "admin.html", 
        attendances=all_attendance, 
        users=all_users, 
        announcements=all_announcements, 
        stats=user_stats, 
        plan=plan_tygodnia, 
        liczniki=plan_liczniki
    )

@app.route('/admin/delete_bulk_users', methods=['POST'])
def delete_bulk_users():
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    user_ids = request.form.getlist('user_ids')
    if user_ids:
        for uid in user_ids:
            user_to_del = Users.query.get(uid)
            if user_to_del:
                Attendance.query.filter_by(user_id=uid).delete()
                Schedule.query.filter_by(user_id=uid).delete()
                db.session.delete(user_to_del)
        db.session.commit()
        flash("Usunięto zaznaczonych użytkowników i ich służby.", "success")
    else:
        flash("Najpierw zaznacz kogoś do usunięcia!", "danger")
    return redirect(url_for('admin_page'))

@app.route('/admin/delete_bulk_attendances', methods=['POST'])
def delete_bulk_attendances():
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    att_ids = request.form.getlist('att_ids')
    if att_ids:
        for aid in att_ids:
            entry = Attendance.query.get(aid)
            if entry:
                db.session.delete(entry)
        db.session.commit()
        flash("Wybrane służby zostały usunięte.", "success")
    else:
        flash("Najpierw zaznacz służby do usunięcia!", "danger")
    return redirect(url_for('admin_page'))

@app.route('/ksDash')
def ksDash():
    if session.get('user_role') not in ['admin', 'ksiądz']: 
        flash("Nie masz uprawnień do wejścia na ten panel!", "danger")
        return redirect(url_for('dashboard_page'))
    
    all_attendance = db.session.query(Attendance, Users).join(Users).order_by(
        Attendance.data_sluzby.desc(), Attendance.godzina.desc()
    ).all()
    all_users = Users.query.all()
    all_announcements = Announcement.query.order_by(Announcement.data_wystawienia.desc()).all()
    schedules = db.session.query(Schedule, Users).join(Users, Schedule.user_id == Users.id).all()
    
    plan_tygodnia = {'Poniedziałek': {}, 'Wtorek': {}, 'Środa': {}, 'Czwartek': {}, 'Piątek': {}, 'Sobota': {}, 'Niedziela': {}}
    plan_liczniki = {}

    for sch, u in schedules:
        dzien = sch.dzien_tygodnia
        godzina = sch.godzina
        if godzina not in plan_tygodnia[dzien]:
            plan_tygodnia[dzien][godzina] = []
        plan_tygodnia[dzien][godzina].append({'id': sch.id, 'user': u})
        
    for dzien in plan_tygodnia:
        plan_tygodnia[dzien] = dict(sorted(plan_tygodnia[dzien].items()))
        count = sum(len(servers) for servers in plan_tygodnia[dzien].values())
        plan_liczniki[dzien] = count
    
    user_atts_map = {u.id: [] for u in all_users}
    for att, usr in all_attendance:
        if usr.id in user_atts_map:
            user_atts_map[usr.id].append(att)

    user_stats = []
    for u in all_users:
        his_atts = user_atts_map.get(u.id, [])
        total = len(his_atts)
        morning = sum(1 for a in his_atts if a.typ_mszy == 'poranna')
        evening = sum(1 for a in his_atts if a.typ_mszy == 'wieczorna')
        other = total - (morning + evening)
        
        user_stats.append({
            'username': u.username, 
            'imie': u.imie,
            'nazwisko': u.nazwisko,
            'full_name': f"{u.imie} {u.nazwisko}",
            'total': total, 
            'morning': morning, 
            'evening': evening, 
            'other': other
        })

    return render_template(
        'ks.html', 
        attendances=all_attendance, 
        users=all_users, 
        announcements=all_announcements, 
        stats=user_stats, 
        plan=plan_tygodnia, 
        liczniki=plan_liczniki
    )

@app.route('/dashboard_view')
def dashboard_page():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
        
    announcements = Announcement.query.order_by(Announcement.data_wystawienia.desc()).all()
    dzisiaj = date.today()
    min_date = max(dzisiaj - timedelta(days=1), date(2026, 4, 12))
    user_attendances = Attendance.query.filter_by(user_id=session['user_id']).order_by(Attendance.data_sluzby.desc()).all()

    schedules = db.session.query(Schedule, Users).join(Users, Schedule.user_id == Users.id).all()
    
    plan_tygodnia = {'Poniedziałek': {}, 'Wtorek': {}, 'Środa': {}, 'Czwartek': {}, 'Piątek': {}, 'Sobota': {}, 'Niedziela': {}}
    plan_liczniki = {}
    moje_dyzury = []
    
    for sch, u in schedules:
        dzien = sch.dzien_tygodnia
        godzina = sch.godzina
        
        if godzina not in plan_tygodnia[dzien]:
            plan_tygodnia[dzien][godzina] = []
        
        plan_tygodnia[dzien][godzina].append({'id': sch.id, 'user': u})
        
        if u.id == session['user_id']:
            moje_dyzury.append({'dzien': sch.dzien_tygodnia, 'godzina': sch.godzina})
            
    for dzien in plan_tygodnia:
        plan_tygodnia[dzien] = dict(sorted(plan_tygodnia[dzien].items()))
        count = sum(len(servers) for servers in plan_tygodnia[dzien].values())
        plan_liczniki[dzien] = count
        
    dni_tygodnia_kolejnosc = {'Poniedziałek': 1, 'Wtorek': 2, 'Środa': 3, 'Czwartek': 4, 'Piątek': 5, 'Sobota': 6, 'Niedziela': 7}
    moje_dyzury = sorted(moje_dyzury, key=lambda x: (dni_tygodnia_kolejnosc.get(x['dzien'], 8), x['godzina']))

    jest_uproszczony = session.get('uproszczony', False)

    if jest_uproszczony:
        return render_template(
            'dash_uproszczony.html', 
            user=session.get('username'), 
            announcements=announcements,
            min_date=min_date.strftime('%Y-%m-%d'),
            today=dzisiaj.strftime('%Y-%m-%d'),
            attendances=user_attendances,
            plan=plan_tygodnia,
            liczniki=plan_liczniki,
            moje_dyzury=moje_dyzury
        )
    else:
        return render_template(
            'dashboard.html', 
            user=session.get('username'), 
            announcements=announcements,
            today=dzisiaj.strftime('%Y-%m-%d'), 
            min_date=min_date.strftime('%Y-%m-%d'),
            attendances=user_attendances,
            plan=plan_tygodnia,
            liczniki=plan_liczniki,
            moje_dyzury=moje_dyzury
        )

@app.route('/delete_my_attendance/<int:id>')
def delete_my_attendance(id):
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    entry = Attendance.query.get_or_404(id)
    if entry.user_id != session['user_id']:
        flash("Nie możesz usunąć służby kogoś innego!", "danger")
        return redirect(url_for('dashboard_page'))
    try:
        db.session.delete(entry)
        db.session.commit()
        flash("Twój wpis został usunięty.", "success")
    except:
        db.session.rollback()
        flash("Nie udało się usunąć wpisu.", "danger")
    return redirect(url_for('dashboard_page'))

@app.route('/edit_my_attendance/<int:id>', methods=['POST'])
def edit_my_attendance(id):
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    entry = Attendance.query.get_or_404(id)
    if entry.user_id != session['user_id']:
        flash("Nie możesz edytować służby kogoś innego!", "danger")
        return redirect(url_for('dashboard_page'))
        
    data_str = request.form.get("date")
    typ_mszy = request.form.get("typ_mszy")
    nazwa_inna = request.form.get("nazwa_inna")
    godzina = request.form.get("godzina")
    
    try:
        wybrana_data = date.fromisoformat(data_str)
        dzisiaj = date.today()
        wczoraj = dzisiaj - timedelta(days=1)
        hard_limit = date(2026, 4, 12)

        if wybrana_data > dzisiaj or wybrana_data < max(wczoraj, hard_limit):
            flash("Nieprawidłowa data!", "danger")
            return redirect(url_for('dashboard_page'))

        istniejaca = Attendance.query.filter(
            Attendance.user_id == session['user_id'], 
            Attendance.data_sluzby == wybrana_data, 
            Attendance.godzina == godzina,
            Attendance.id != id
        ).first()

        if istniejaca:
            flash("Masz już zgłoszoną inną służbę o tej godzinie!", "danger")
            return redirect(url_for('dashboard_page'))

        entry.data_sluzby = wybrana_data
        entry.typ_mszy = typ_mszy
        entry.nazwa_inna = nazwa_inna if typ_mszy == 'inna' else None
        entry.godzina = godzina
        db.session.commit()
        flash("Twoja służba została zaktualizowana!", "success")
    except Exception as e:
        db.session.rollback()
        flash("Wystąpił błąd podczas edycji wpisu.", "danger")
    return redirect(url_for('dashboard_page'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

@app.route('/forget-password')
def forget_password():
    return render_template('forget-password.html')

@app.route('/robots.txt')
def static_from_root():
    return send_from_directory(app.static_folder, request.path[1:])

@app.route('/sitemap.xml')
def sitemap_from_root():
    return send_from_directory(app.static_folder, request.path[1:])

@app.route('/export_raport')
def export_raport():
    if session.get('user_role') not in ['admin', 'ksiądz']: 
        flash("Brak uprawnień do pobierania raportów.", "danger")
        return redirect(url_for('dashboard_page'))

    all_attendance = db.session.query(Attendance, Users).join(Users).all()
    all_users = Users.query.all()
    
    user_atts_map = {u.id: [] for u in all_users}
    for att, usr in all_attendance:
        if usr.id in user_atts_map:
            user_atts_map[usr.id].append(att)

    data = []
    for u in all_users:
        his_atts = user_atts_map.get(u.id, [])
        total = len(his_atts)
        morning = sum(1 for a in his_atts if a.typ_mszy == 'poranna')
        evening = sum(1 for a in his_atts if a.typ_mszy == 'wieczorna')
        other = total - (morning + evening)
        
        data.append({
            'Imię i Nazwisko': f"{u.imie} {u.nazwisko}",
            'Pseudonim (Login)': u.username,
            'Suma Służb': total,
            'Poranne': morning,
            'Wieczorne': evening,
            'Inne': other
        })

    df = pd.DataFrame(data)
    df = df.sort_values(by='Suma Służb', ascending=False)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Ranking_Ministrantow')
    
    output.seek(0)
    nazwa_pliku = f"Raport_Ministranci_{date.today().strftime('%Y-%m-%d')}.xlsx"
    return send_file(output, download_name=nazwa_pliku, as_attachment=True)


@app.route('/export_schedule')
def export_schedule():
    if 'user_id' not in session: 
        flash("Musisz być zalogowany, aby pobrać plan.", "danger")
        return redirect(url_for('login_page'))

    schedules = db.session.query(Schedule, Users).join(Users, Schedule.user_id == Users.id).all()
    
    data = []
    dni_kolejnosc = {'Poniedziałek': 1, 'Wtorek': 2, 'Środa': 3, 'Czwartek': 4, 'Piątek': 5, 'Sobota': 6, 'Niedziela': 7}
    
    for sch, u in schedules:
        data.append({
            'Dzień Tygodnia': sch.dzien_tygodnia,
            'Kolejność': dni_kolejnosc.get(sch.dzien_tygodnia, 8),
            'Godzina': sch.godzina,
            'Imię i Nazwisko': f"{u.imie} {u.nazwisko}",
            'Pseudonim (Login)': u.username
        })

    df = pd.DataFrame(data)
    if not df.empty:
        df = df.sort_values(by=['Kolejność', 'Godzina'])
        df = df.drop(columns=['Kolejność'])
    else:
        df = pd.DataFrame(columns=['Dzień Tygodnia', 'Godzina', 'Imię i Nazwisko', 'Pseudonim (Login)'])

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Plan_Sluzb')
    
    output.seek(0)
    nazwa_pliku = f"Plan_Sluzb_{date.today().strftime('%Y-%m-%d')}.xlsx"
    return send_file(output, download_name=nazwa_pliku, as_attachment=True)

with app.app_context(): 
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True)
