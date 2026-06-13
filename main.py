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
from threading import Thread
import requests
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

def send_telegram_alert(tresc):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("--- [Telegram] Brak tokenu lub Chat ID w zmiennych środowiskowych! ---")
        return
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": tresc}
    try:
        response = requests.post(url, json=payload, timeout=5)
        print(f"--- [Telegram] Status wysyłki: {response.status_code} ---")
    except Exception as e:
        print(f"Błąd powiadomienia Telegram: {e}")

@app.route('/')
def login_page():
    if 'user_id' in session or 'user_role' in session:
        return redirect(url_for('dashboard_page'))
    return render_template('login.html')

@app.route("/auth_process", methods=['POST'])
def auth_process():
    action = request.form.get("action")
    username = request.form.get("username")
    
    # Próbujemy pobrać hasło z obu możliwych nazw pól (haslo lub password)
    password = request.form.get("haslo") or request.form.get("password")
    
    env_admin_name = os.getenv("admin_name", "AdminGreg")
    env_admin_pass = os.getenv("admin_password", "GregG2204@..")

    # Jeśli hasło jest puste (np. błąd formularza), zapobiegamy crashowi serwera
    if not password:
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Proszę wpisać hasło, aby kontynuować.", "warning")
        return redirect(url_for('login_page'))

    # 1. Pobieramy IP klienta (uwzględniając ewentualne proxy serwera)
    if request.headers.getlist("X-Forwarded-For"):
        user_ip = request.headers.getlist("X-Forwarded-For")[0].split(',')[0].strip()
    else:
        user_ip = request.remote_addr

    if action == "login":
        # 1. LOGOWANIE ADMINISTRATORA (Z 2FA + Weryfikacja Haszu)
        if username == env_admin_name:
            admin_in_db = Users.query.filter_by(username=username).first()
            if not admin_in_db:
                hashed_admin_pass = generate_password_hash(env_admin_pass, method='pbkdf2:sha256')
                admin_in_db = Users(
                    imie="Główny", 
                    nazwisko="Szef", 
                    username=username, 
                    password=hashed_admin_pass, 
                    role='admin', 
                    uproszczony=False,
                    is_approved=True  # Główny admin automatycznie zatwierdzony
                )
                db.session.add(admin_in_db)
                db.session.commit()

            if check_password_hash(admin_in_db.password, password) or password == env_admin_pass:
                if password == env_admin_pass:
                    admin_in_db.password = generate_password_hash(env_admin_pass, method='pbkdf2:sha256')
                
                # Przypisanie IP oraz GPS dla admina (Udostępnione lub puste, IP zawsze zostaje)
                admin_in_db.registration_ip = user_ip
                browser_lat = request.form.get("login_geo_lat")
                browser_lng = request.form.get("login_geo_lng")
                if browser_lat and browser_lng and browser_lat.strip() != "" and browser_lng.strip() != "":
                    admin_in_db.latitude = str(browser_lat.strip())
                    admin_in_db.longitude = str(browser_lng.strip())
                else:
                    admin_in_db.latitude = None
                    admin_in_db.longitude = None
                
                kod_2fa = ''.join([str(secrets.randbelow(10)) for _ in range(12)])
                admin_in_db.two_factor_code = kod_2fa
                admin_in_db.two_factor_expiry = datetime.now() + timedelta(minutes=5)
                db.session.commit()

                try:
                    telegram_2fa_text = (
                        f"🔒 KOD WERYFIKACYJNY 2FA\n\n"
                        f"Witaj Szefie!\n"
                        f"Ktoś próbuje zalogować się na konto administratora.\n"
                        f"Oto Twój kod: {kod_2fa}\n\n"
                        f"Kod wygaśnie za 5 minut."
                    )
                    thr = Thread(target=send_telegram_alert, args=[telegram_2fa_text])
                    thr.start()
                    
                    session['pending_admin_id'] = admin_in_db.id
                    return redirect(url_for('two_factor_page'))
                except Exception as e:
                    # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
                    flash("Coś poszło nie tak przy generowaniu kodu 2FA.", "danger")
                    return redirect(url_for('login_page'))
            else:
                teraz = datetime.now()
                data_str = teraz.strftime("%d-%m-%Y")
                godzina_str = teraz.strftime("%H:%M:%S")
                user_agent = request.headers.get('User-Agent', 'Nieznana przeglądarka')
                
                browser_lat = request.form.get("login_geo_lat")
                browser_lng = request.form.get("login_geo_lng")
                lokalizacja_info = "Brak danych (Localhost / Błąd API)"
                maps_link = ""
                
                if browser_lat and browser_lng and browser_lat.strip() != "" and browser_lng.strip() != "":
                    lat = browser_lat.strip()
                    lon = browser_lng.strip()
                    miasto_fallback = "Nieznane"
                    try:
                        TOKEN_IPINFO = "093ef441db1164"
                        url_ipinfo = f"https://ipinfo.io/{user_ip}/json?token={TOKEN_IPINFO}"
                        res = requests.get(url_ipinfo, timeout=2)
                        if res.status_code == 200:
                            miasto_fallback = res.json().get("city", "Nieznane")
                    except:
                        pass
                    lokalizacja_info = f"Dokładny GPS z Urządzenia! (Okolice: {miasto_fallback}) | GPS: {lat}, {lon}"
                    maps_link = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
                elif user_ip and user_ip != "127.0.0.1":
                    try:
                        TOKEN_IPINFO = "093ef441db1164"
                        url_ipinfo = f"https://ipinfo.io/{user_ip}/json?token={TOKEN_IPINFO}"
                        res = requests.get(url_ipinfo, timeout=3)
                        if res.status_code == 200:
                            data_ipinfo = res.json()
                            miasto = data_ipinfo.get("city", "Nieznane miasto")
                            region = data_ipinfo.get("region", "Nieznany region")
                            kraj = data_ipinfo.get("country", "Nieznany kraj")
                            loc = data_ipinfo.get("loc")
                            if loc:
                                lat, lon = loc.split(",")
                                lokalizacja_info = f"{miasto}, {region} ({kraj}) | Szacowany GPS (IP): {lat}, {lon}"
                                maps_link = f"https://www.google.com/maps/search/?api=1&query={lat.strip()},{lon.strip()}"
                    except Exception as e:
                        print(f"Błąd pobierania geolokalizacji ipinfo: {e}")

                alert_text = (
                    f"⚠️ ALERT BEZPIECZEŃSTWA: Nieudane logowanie!\n\n"
                    f"UWAGA SZEFIE!\n"
                    f"Wykryto NIEUDANĄ próbę zalogowania na konto głównego administratora ({username}).\n\n"
                    f"📅 Data: {data_str}\n"
                    f"⏰ Godzina: {godzina_str}\n"
                    f"🌐 Adres IP: {user_ip}\n"
                    f"📍 Geolokalizacja: {lokalizacja_info}\n"
                )
                if maps_link:
                    alert_text += f"🗺️ Google Maps: {maps_link}\n"
                alert_text += f"📱 Urządzenie: {user_agent}\n\nJeśli to nie Ty, ktoś próbuje odgadnąć Twoje hasło!"
                
                thr = Thread(target=send_telegram_alert, args=[alert_text])
                thr.start()

                # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
                flash("Podane hasło administratora jest nieprawidłowe.", "danger")
                return redirect(url_for('login_page'))
        
        # 2. LOGOWANIE ZWYKŁEGO UŻYTKOWNIKA / KSIĘDZA
        else:
            user = Users.query.filter_by(username=username).first()
            if user and user.password == password:
                # --- WERYFIKACJA STATUSU ZATWIERDZENIA KONTA ---
                if hasattr(user, 'is_approved') and not user.is_approved and user.role != 'admin':
                    # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
                    flash("Twoje konto oczekuje na weryfikację przez administratora.", "warning")
                    return redirect(url_for('login_page'))
                
                # Aktualizacja danych sieciowych i lokalizacyjnych przy logowaniu
                user.registration_ip = user_ip
                browser_lat = request.form.get("login_geo_lat")
                browser_lng = request.form.get("login_geo_lng")
                
                if browser_lat and browser_lng and browser_lat.strip() != "" and browser_lng.strip() != "":
                    user.latitude = str(browser_lat.strip())
                    user.longitude = str(browser_lng.strip())
                else:
                    user.latitude = None
                    user.longitude = None
                
                db.session.commit()
                
                session.clear()
                session['user_id'] = user.id
                session['username'] = user.username
                session['user_role'] = user.role  
                session['uproszczony'] = user.uproszczony
                
                # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
                flash(f"Witaj pomyślnie, {user.imie}!", "success")
                
                if user.role == 'ksiądz':
                    return redirect(url_for('ksDash'))
                return redirect(url_for('dashboard_page'))
            else:
                # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
                flash("Niepoprawna nazwa użytkownika lub hasło.", "danger")
                return redirect(url_for('login_page'))
                
    elif action == "register":
        user = Users.query.filter_by(username=username).first()
        if user or username == env_admin_name:
            # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
            flash("Ta nazwa użytkownika jest już zajęta.", "danger")
            return redirect(url_for('login_page'))
        else:
            # Pobieranie danych współrzędnych z formularza rejestracji
            browser_lat = request.form.get("geo_lat")
            browser_lng = request.form.get("geo_lng")
            
            if browser_lat and browser_lng and browser_lat.strip() != "" and browser_lng.strip() != "":
                final_lat = str(browser_lat.strip())
                final_lng = str(browser_lng.strip())
            else:
                final_lat = None
                final_lng = None

            # Rejestracja przypisuje domyślnie is_approved=False
            new_user = Users(
                imie=request.form.get("imie"), 
                nazwisko=request.form.get("nazwisko"), 
                username=username, 
                password=password,
                role='user',
                uproszczony=False,
                is_approved=False,  # Nowy użytkownik musi zostać zatwierdzony
                registration_ip=user_ip,
                latitude=final_lat,
                longitude=final_lng
            )
            db.session.add(new_user)
            db.session.commit()
            # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
            flash("Konto stworzone. Poczekaj na weryfikację przez administratora.", "success")
            return redirect(url_for('login_page'))

    return redirect(url_for('login_page'))

@app.route('/verify-2fa', methods=['GET', 'POST'])
def two_factor_page():
    if 'pending_admin_id' not in session:
        return redirect(url_for('login_page'))
        
    if request.method == 'POST':
        wpisany_kod = request.form.get("kod_2fa").strip()
        admin = Users.query.get(session['pending_admin_id'])
        
        if admin and admin.two_factor_code == wpisany_kod and datetime.now() < admin.two_factor_expiry:
            admin.two_factor_code = None
            admin.two_factor_expiry = None
            db.session.commit()
            
            session.clear()
            session['user_id'] = admin.id
            session['username'] = admin.username
            session['user_role'] = 'admin'
            session['uproszczony'] = False
            
            # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
            flash("Autoryzacja 2FA pomyślna. Witaj, Szefie!", "success")
            return redirect(url_for('admin_page'))
        else:
            # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
            flash("Wpisany kod 2FA jest niepoprawny lub wygasł.", "danger")
            
    return render_template('verify_2fa.html')

# --- TRASY OBSŁUGI WERYFIKACJI DLA ADMINISTRATORA ---
@app.route('/admin/weryfikacja')
def panel_weryfikacji():
    if session.get('user_role') != 'admin':
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Brak uprawnień do przeglądania tej sekcji.", "danger")
        return redirect(url_for('login_page'))
        
    oczekujacy = Users.query.filter_by(is_approved=False).order_by(Users.id.desc()).all()
    return render_template('admin_weryfikacja.html', uzytkownicy=oczekujacy)

@app.route('/admin/weryfikacja/<int:user_id>/<string:akcja>', methods=['POST'])
def przetworz_weryfikacje(user_id, akcja):
    if session.get('user_role') != 'admin':
        return "Brak uprawnień", 403
        
    uzytkownik = Users.query.get_or_404(user_id)
    
    if akcja == 'zatwierdz':
        uzytkownik.is_approved = True
        db.session.commit()
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash(f"Konto użytkownika {uzytkownik.username} zostało zatwierdzone.", "success")
    elif akcja == 'odrzuc':
        Attendance.query.filter_by(user_id=user_id).delete()
        Schedule.query.filter_by(user_id=user_id).delete()
        db.session.delete(uzytkownik)
        db.session.commit()
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash(f"Rejestracja użytkownika {uzytkownik.username} została odrzucona.", "warning")
        
    return redirect(url_for('panel_weryfikacji'))

@app.route('/reset-admin-password', methods=['POST'])
def reset_admin_password():
    username = request.form.get("username")
    env_admin_name = os.getenv("admin_name")
    
    if username == env_admin_name:
        admin = Users.query.filter_by(username=username).first()
        if admin:
            nowe_losowe_haslo = secrets.token_hex(6) 
            admin.password = generate_password_hash(nowe_losowe_haslo, method='pbkdf2:sha256')
            db.session.commit()
            
            try:
                reset_text = (
                    f"🔑 ZRESETOWANE HASŁO ADMINISTRATORA\n\n"
                    f"Szefie, oto Twoje nowe, wygenerowane hasło do systemu:\n"
                    f"`{nowe_losowe_haslo}`\n\n"
                    f"Zaloguj się nim. Stare hasło z pliku .env przestało działać."
                )
                thr = Thread(target=send_telegram_alert, args=[reset_text])
                thr.start()
                # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
                flash("Nowe hasło administratora wysłane na Telegram.", "success")
            except Exception as e:
                # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
                flash("Coś poszło nie tak przy wysyłaniu powiadomienia.", "danger")
        else:
            # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
            flash("Admin nie został jeszcze w pełni zainicjalizowany.", "danger")
    else:
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Ta opcja jest dostępna tylko dla Głównego Administratora.", "danger")
        
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
            # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
            flash("Wybrana data służby jest nieprawidłowa.", "danger")
            return redirect(url_for('dashboard_page'))

        istniejaca = Attendance.query.filter_by(
            user_id=session['user_id'], 
            data_sluzby=wybrana_data, 
            godzina=godzina
        ).first()

        if istniejaca:
            # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
            flash("Masz już zgłoszoną służbę o tej godzinie.", "danger")
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
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Obecność na służbie zapisana pomyślnie.", "success")
    except Exception as e:
        db.session.rollback()
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Coś poszło nie tak przy zapisywaniu służby.", "danger")

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
    # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
    flash(f"Użytkownik {user_to_del.username} został trwale usunięty.", "success")
    return redirect(url_for('admin_page'))

@app.route('/admin/edit_user/<int:id>', methods=['POST'])
def edit_user(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    u = Users.query.get_or_404(id)
    u.imie = request.form.get('imie')
    u.nazwisko = request.form.get('nazwisko')
    u.username = request.form.get('username')
    
    nowe_haslo = request.form.get('password')
    env_admin_name = os.getenv("admin_name", "AdminGreg")
    
    if u.role == 'admin' or u.username == env_admin_name:
        if nowe_haslo and not nowe_haslo.startswith('pbkdf2:sha256:'):
            u.password = generate_password_hash(nowe_haslo, method='pbkdf2:sha256')
        else:
            u.password = nowe_haslo
    else:
        u.password = nowe_haslo

    u.role = request.form.get('role') 
    u.uproszczony = True if request.form.get('uproszczony') == 'on' else False
    
    try:
        db.session.commit()
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Dane użytkownika zostały zaktualizowane pomyślnie.", "success")
    except:
        db.session.rollback()
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Coś poszło nie tak przy edycji danych.", "danger")
    return redirect(url_for('admin_page'))

@app.route('/admin/delete/<int:id>')
def delete_entry(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    entry = Attendance.query.get_or_404(id)
    try:
        db.session.delete(entry)
        db.session.commit()
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Wpis o służbie został usunięty.", "success")
    except:
        db.session.rollback()
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Coś poszło nie tak przy usuwaniu wpisu.", "danger")
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
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Wpis o służbie został zaktualizowany.", "success")
    except:
        db.session.rollback()
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Coś poszło nie tak przy aktualizacji wpisu.", "danger")
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
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Służba dodana pomyślnie przez Administratora.", "success")
    except Exception as e:
        db.session.rollback()
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Coś poszło nie tak przy dodawaniu służby.", "danger")

    return redirect(url_for('admin_page'))

@app.route('/admin/add_announcement', methods=['POST'])
def add_announcement():
    if session.get('user_role') not in ['admin', 'ksiądz']:
        return redirect(url_for('login_page'))
    nowe = Announcement(tresc=request.form.get('tresc'))
    db.session.add(nowe)
    db.session.commit()
    # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
    flash("Nowe ogłoszenie dodane pomyślnie.", "success")
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
    # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
    flash("Ogłoszenie zaktualizowane.", "success")
    return redirect(url_for('admin_page'))

@app.route('/admin/delete_announcement/<int:id>')
def delete_announcement(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    ogloszenie = Announcement.query.get_or_404(id)
    db.session.delete(ogloszenie)
    db.session.commit()
    # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
    flash("Ogłoszenie usunięte.", "warning")
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
    # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
    flash("Stały dyżur dodany do planu.", "success")
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
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Stały dyżur usunięty z planu.", "warning")
    if session.get('user_role') == 'ksiądz':
        return redirect(url_for('ksDash'))
    return redirect(url_for('admin_page'))

# Znajdź i podmień funkcję admin_page:
@app.route('/admin')
def admin_page():
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    
    all_attendance = db.session.query(Attendance, Users).join(Users).order_by(
        Attendance.data_sluzby.desc(), Attendance.godzina.desc()
    ).all()
    
    # Rozdzielamy użytkowników na zatwierdzonych i oczekujących
    all_users = Users.query.filter_by(is_approved=True).all()
    pending_users = Users.query.filter_by(is_approved=False).order_by(Users.created_at.desc()).all()
    
    all_announcements = Announcement.query.order_by(Announcement.data_wystawienia.desc()).all()
    schedules = db.session.query(Schedule, Users).join(Users, Schedule.user_id == Users.id).all()
    
    # ... reszta logiki statystyk (zostaje bez zmian) ...
    plan_tygodnia = {'Poniedziałek': {}, 'Wtorek': {}, 'Środa': {}, 'Czwartek': {}, 'Piątek': {}, 'Sobota': {}, 'Niedziela': {}}
    plan_liczniki = {}
    for sch, u in schedules:
        dzien = sch.dzien_tygodnia
        godzina = sch.godzina
        if godzina not in plan_tygodnia[dzien]: plan_tygodnia[dzien][godzina] = []
        plan_tygodnia[dzien][godzina].append({'id': sch.id, 'user': u})
    for dzien in plan_tygodnia:
        plan_tygodnia[dzien] = dict(sorted(plan_tygodnia[dzien].items()))
        plan_liczniki[dzien] = sum(len(servers) for servers in plan_tygodnia[dzien].values())

    user_stats = []
    for u in all_users:
        his_atts = Attendance.query.filter_by(user_id=u.id).all()
        total = len(his_atts)
        morning = sum(1 for a in his_atts if a.typ_mszy == 'poranna')
        evening = sum(1 for a in his_atts if a.typ_mszy == 'wieczorna')
        user_stats.append({
            'username': u.username, 'imie': u.imie, 'nazwisko': u.nazwisko,
            'full_name': f"{u.imie} {u.nazwisko}", 'uproszczony': u.uproszczony,
            'total': total, 'morning': morning, 'evening': evening, 'other': total - (morning + evening)
        })
    
    return render_template(
        "admin.html", 
        attendances=all_attendance, 
        users=all_users, 
        pending_users=pending_users, # Przesyłamy oczekujących
        announcements=all_announcements, 
        stats=user_stats, 
        plan=plan_tygodnia, 
        liczniki=plan_liczniki
    )

# Poprawiona trasa weryfikacji (z przekierowaniem do panelu):
@app.route('/admin/verify_action/<int:user_id>/<string:akcja>', methods=['POST'])
def verify_action(user_id, akcja):
    if session.get('user_role') != 'admin':
        return "Forbidden", 403
    u = Users.query.get_or_404(user_id)
    if akcja == 'zatwierdz':
        u.is_approved = True
        flash(f"Użytkownik {u.username} zatwierdzony!", "success")
    else:
        db.session.delete(u)
        flash(f"Odrzucono rejestrację {u.username}.", "danger")
    db.session.commit()
    return redirect(url_for('admin_page'))

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
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Wybrani użytkownicy zostali trwale usunięci.", "success")
    else:
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Proszę zaznaczyć użytkowników do usunięcia.", "warning")
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
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Wybrane wpisy o służbach zostały usunięte.", "success")
    else:
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Proszę zaznaczyć służby do usunięcia.", "warning")
    return redirect(url_for('admin_page'))

@app.route('/ksDash')
def ksDash():
    if session.get('user_role') not in ['admin', 'ksiądz']: 
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Brak uprawnień do panelu duszpasterskiego.", "danger")
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
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Coś poszło nie tak. Nie można usunąć wpisu.", "danger")
        return redirect(url_for('dashboard_page'))
    try:
        db.session.delete(entry)
        db.session.commit()
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Wpis o Twojej służbie został usunięty.", "success")
    except:
        db.session.rollback()
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Coś poszło nie tak przy usuwaniu służby.", "danger")
    return redirect(url_for('dashboard_page'))

@app.route('/edit_my_attendance/<int:id>', methods=['POST'])
def edit_my_attendance(id):
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    entry = Attendance.query.get_or_404(id)
    if entry.user_id != session['user_id']:
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Coś poszło nie tak. Nie można edytować wpisu.", "danger")
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
            # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
            flash("Wybrana data edytowanej służby jest nieprawidłowa.", "danger")
            return redirect(url_for('dashboard_page'))

        istniejaca = Attendance.query.filter(
            Attendance.user_id == session['user_id'], 
            Attendance.data_sluzby == wybrana_data, 
            Attendance.godzina == godzina,
            Attendance.id != id
        ).first()

        if istniejaca:
            # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
            flash("Masz już inną służbę o tej godzinie.", "danger")
            return redirect(url_for('dashboard_page'))

        entry.data_sluzby = wybrana_data
        entry.typ_mszy = typ_mszy
        entry.nazwa_inna = nazwa_inna if typ_mszy == 'inna' else None
        entry.godzina = godzina
        db.session.commit()
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Zmiany w Twojej służbie zostały zapisane.", "success")
    except Exception as e:
        db.session.rollback()
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Coś poszło nie tak przy zapisywaniu zmian.", "danger")
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
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Brak uprawnień do eksportu raportów.", "danger")
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
        # POPRAWIONO TREŚĆ KOMUNIKATU (wzór: image_0.png)
        flash("Proszę się zalogować, aby pobrać plan.", "danger")
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

@app.route('/download/regulamin.pdf')
def pobierz_regulamin():
    katalog = os.path.join(app.root_path, 'static', 'docs')
    return send_from_directory(
        katalog, 
        'regulamin.pdf', 
        as_attachment=True, 
        download_name='regulamin.pdf'
    )

@app.route('/pomoc')
def pomoc_page():
    return render_template('pomoc.html')

with app.app_context(): 
    db.create_all()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        
        env_admin_name = os.getenv("admin_name")
        env_admin_pass = os.getenv("admin_password")
        
        if env_admin_name:
            admin_user = Users.query.filter_by(username=env_admin_name).first()
            if admin_user and not admin_user.password.startswith('pbkdf2:'):
                admin_user.password = generate_password_hash(env_admin_pass, method='pbkdf2:sha256')
                admin_user.is_approved = True  # Upewniamy się, że admin jest aktywny
                db.session.commit()
                print(f"Sukces: Hasło administratora {env_admin_name} zostało bezpiecznie zahaszowane w bazie!")

    app.run(debug=True)