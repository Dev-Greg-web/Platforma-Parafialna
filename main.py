from flask import Flask, render_template, request, flash, redirect, url_for, session, send_from_directory, send_file
from models import Users, Attendance, Announcement, Schedule, db
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta, date
import os
import secrets
from dotenv import load_dotenv
import pandas as pd
import io
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.chart import PieChart, Reference
from openpyxl.utils import get_column_letter
from werkzeug.security import generate_password_hash, check_password_hash
import urllib.request
import json
from threading import Thread
import requests
from sqlalchemy.orm import joinedload
from collections import defaultdict

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("TAJNE_HASLO") or secrets.token_hex(32)
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

# Formats datetime values for templates
@app.template_filter('datetimeformat')
def datetimeformat(value, format='%Y-%m-%d %H:%M'):
    if value is None:
        return ""
    return value.strftime(format)

# Formats date objects for templates
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

# Adds security headers to prevent browser caching
@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# Formats datetime into Polish text format
def format_datetime_pl(dt):
    if not dt: return ""
    dni = ["pon.", "wt.", "śr.", "czw.", "pt.", "sob.", "nd."]
    godz_min = dt.strftime("%H:%M")
    return f"{dni[dt.weekday()]} {dt.day}.{dt.month} o {godz_min}"

# Resolves IP address to geolocation data
def resolve_ip_and_coords(ip_addr):
    result = {
        "display": ip_addr,
        "lat": None,
        "lng": None
    }
    
    if not ip_addr or ip_addr == "127.0.0.1":
        result["display"] = "127.0.0.1 (Lokalny komputer testowy)"
        result["lat"] = "52.2297"
        result["lng"] = "21.0118"
        return result
        
    try:
        token = os.getenv("IPINFO_TOKEN") or "093ef441db1164"
        url = f"https://ipinfo.io/{ip_addr}/json?token={token}"
        res = requests.get(url, timeout=2)
        if res.status_code == 200:
            data = res.json()
            city = data.get("city", "Nieznane")
            country = data.get("country", "PL")
            loc = data.get("loc", "")
            
            if loc:
                result["display"] = f"{ip_addr} ({city}, {country}) | Szacowany GPS: {loc}"
                parts = loc.split(",")
                if len(parts) == 2:
                    result["lat"] = parts[0].strip()
                    result["lng"] = parts[1].strip()
            else:
                result["display"] = f"{ip_addr} ({city}, {country})"
    except Exception as e:
        print(f"Błąd pobierania fallback IPinfo: {e}")
    return result

app.jinja_env.filters['datetime_pl'] = format_datetime_pl

# Sends security alerts via Telegram bot
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
    except Exception as e:
        print(f"Błąd powiadomienia Telegram: {e}")

# Displays the user login page
@app.route('/')
def login_page():
    if 'user_id' in session or 'user_role' in session:
        return redirect(url_for('dashboard_page'))
    return render_template('login.html')

# Processes user login and registration requests
@app.route("/auth_process", methods=['POST'])
def auth_process():
    action = request.form.get("action")
    username = request.form.get("username")
    password = request.form.get("haslo") or request.form.get("password")
    
    env_admin_name = os.getenv("ADMIN_NAME") or os.getenv("admin_name") or "AdminGreg"
    env_admin_pass = os.getenv("ADMIN_PASSWORD") or os.getenv("admin_password") or "Lego2012"

    if not password:
        flash("Proszę wpisać hasło, aby kontynuować.", "warning")
        return redirect(url_for('login_page'))

    if request.headers.getlist("X-Forwarded-For"):
        user_ip = request.headers.getlist("X-Forwarded-For")[0].split(',')[0].strip()
    else:
        user_ip = request.remote_addr

    ip_data = resolve_ip_and_coords(user_ip)
    resolved_ip_str = ip_data["display"]

    if action == "login":
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
                    is_approved=True
                )
                db.session.add(admin_in_db)
                db.session.commit()

            if check_password_hash(admin_in_db.password, password) or password == env_admin_pass:
                if password == env_admin_pass:
                    admin_in_db.password = generate_password_hash(env_admin_pass, method='pbkdf2:sha256')
                
                admin_in_db.registration_ip = resolved_ip_str
                
                browser_lat = request.form.get("geo_lat")
                browser_lng = request.form.get("geo_lng")
                
                if browser_lat and browser_lng and browser_lat.strip() != "" and browser_lng.strip() != "":
                    admin_in_db.latitude = str(browser_lat.strip())
                    admin_in_db.longitude = str(browser_lng.strip())
                else:
                    admin_in_db.latitude = ip_data["lat"]
                    admin_in_db.longitude = ip_data["lng"]
                
                bloki = ["".join([str(secrets.randbelow(10)) for _ in range(3)]) for _ in range(4)]
                kod_2fa = "-".join(bloki)
                
                admin_in_db.two_factor_code = kod_2fa
                admin_in_db.two_factor_expiry = datetime.now() + timedelta(minutes=5)
                db.session.commit()

                try:
                    telegram_2fa_text = (
                        f"🔒 KOD WERYFIKACYJNY 2FA\n\n"
                        f"Witaj Szefie!\n"
                        f"Ktoś próbuje zalogować się na konto administratora.\n"
                        f"Oto Twój kod: `{kod_2fa}`\n\n"
                        f"Kod wygaśnie za 5 minut."
                    )
                    thr = Thread(target=send_telegram_alert, args=[telegram_2fa_text])
                    thr.start()
                    
                    session['pending_admin_id'] = admin_in_db.id
                    return redirect(url_for('two_factor_page'))
                except Exception as e:
                    flash("Coś poszło nie tak przy generowaniu kodu 2FA.", "danger")
                    return redirect(url_for('login_page'))
            else:
                teraz = datetime.now()
                data_str = teraz.strftime("%d-%m-%Y")
                godzina_str = teraz.strftime("%H:%M:%S")
                user_agent = request.headers.get('User-Agent', 'Nieznana przeglądarka')
                
                browser_lat = request.form.get("geo_lat")
                browser_lng = request.form.get("geo_lng")
                lokalizacja_info = "Brak danych (Localhost / Błąd API)"
                maps_link = ""
                
                if browser_lat and browser_lng and browser_lat.strip() != "" and browser_lng.strip() != "":
                    lat = browser_lat.strip()
                    lon = browser_lng.strip()
                    lokalizacja_info = f"Dokładny GPS z Urządzenia! | GPS: {lat}, {lon}"
                    maps_link = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
                elif ip_data["lat"] and ip_data["lng"]:
                    lokalizacja_info = resolved_ip_str
                    maps_link = f"https://www.google.com/maps/search/?api=1&query={ip_data['lat']},{ip_data['lng']}"
                else:
                    lokalizacja_info = resolved_ip_str

                alert_text = (
                    f"⚠️ ALERT BEZPIECZEŃSTWA: Nieudane logowanie!\n\n"
                    f"Wykryto NIEUDANĄ próbę zalogowania na konto administratora ({username}).\n\n"
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

                flash("Podane hasło administratora jest nieprawidłowe.", "danger")
                return redirect(url_for('login_page'))
        else:
            user = Users.query.filter_by(username=username).first()
            if user and user.password == password:
                if hasattr(user, 'is_approved') and not user.is_approved and user.role != 'admin':
                    flash("Twoje konto oczekuje na weryfikację przez administratora.", "warning")
                    return redirect(url_for('login_page'))
                
                user.registration_ip = resolved_ip_str
                
                browser_lat = request.form.get("geo_lat")
                browser_lng = request.form.get("geo_lng")
                
                if browser_lat and browser_lng and browser_lat.strip() != "" and browser_lng.strip() != "":
                    user.latitude = str(browser_lat.strip())
                    user.longitude = str(browser_lng.strip())
                else:
                    user.latitude = ip_data["lat"]
                    user.longitude = ip_data["lng"]
                
                db.session.commit()
                session.clear()
                session['user_id'] = user.id
                session['username'] = user.username
                session['user_role'] = user.role  
                session['uproszczony'] = user.uproszczony
                
                flash(f"Witaj pomyślnie, {user.imie}!", "success")
                
                if user.role == 'ksiądz':
                    return redirect(url_for('ksDash'))
                return redirect(url_for('dashboard_page'))
            else:
                flash("Niepoprawna nazwa użytkownika lub hasło.", "danger")
                return redirect(url_for('login_page'))
                
    elif action == "register":
        if not request.form.get('regulamin'):
            flash('Musisz zaakceptować regulamin, aby się zarejestrować!', 'danger')
            return redirect(url_for('login_page'))

        user = Users.query.filter_by(username=username).first()
        if user or username == env_admin_name:
            flash("Ta nazwa użytkownika jest już zajęta.", "danger")
            return redirect(url_for('login_page'))
        else:
            browser_lat = request.form.get("geo_lat")
            browser_lng = request.form.get("geo_lng")
            
            if browser_lat and browser_lng and browser_lat.strip() != "" and browser_lng.strip() != "":
                final_lat = str(browser_lat.strip())
                final_lng = str(browser_lng.strip())
            else:
                final_lat = ip_data["lat"]
                final_lng = ip_data["lng"]

            new_user = Users(
                imie=request.form.get("imie"), 
                nazwisko=request.form.get("nazwisko"), 
                username=username, 
                password=password,
                role='user',
                uproszczony=False,
                is_approved=False,
                registration_ip=resolved_ip_str,
                latitude=final_lat,
                longitude=final_lng
            )
            db.session.add(new_user)
            db.session.commit()
            
            flash("Konto stworzone. Poczekaj na weryfikację przez administratora.", "success")
            return redirect(url_for('login_page'))

    return redirect(url_for('login_page'))

# Updates user details from admin dashboard
@app.route('/admin/edit_user/<int:user_id>', methods=['POST'])
def admin_edit_user(user_id):
    if session.get('role') != 'admin' and session.get('user_role') != 'admin':
        flash('Brak uprawnień bazy szefa!', 'danger')
        return redirect(url_for('login_page'))
        
    user = Users.query.get_or_404(user_id)
    user.imie = request.form.get('imie')
    user.nazwisko = request.form.get('nazwisko')
    user.username = request.form.get('username')
    user.role = request.form.get('role')
    user.is_approved = request.form.get('is_approved') == 'true'
    user.uproszczony = request.form.get('uproszczony') == 'true'
    
    try:
        db.session.commit()
        flash(f'Dane użytkownika {user.imie} {user.nazwisko} zostały zaktualizowane!', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Wystąpił błąd podczas zapisu danych (prawdopodobnie login jest zajęty).', 'danger')
        
    return redirect(url_for('admin_page'))

# Handles two-factor authentication verification
@app.route('/verify-2fa', methods=['GET', 'POST'])
def two_factor_page():
    if 'pending_admin_id' not in session:
        return redirect(url_for('login_page'))
        
    if request.method == 'POST':
        wpisany_kod = request.form.get("kod_2fa").strip()
        admin = db.session.get(Users, session['pending_admin_id'])
        
        if admin and admin.two_factor_code == wpisany_kod and datetime.now() < admin.two_factor_expiry:
            admin.two_factor_code = None
            admin.two_factor_expiry = None
            db.session.commit()
            
            session.clear()
            session['user_id'] = admin.id
            session['username'] = admin.username
            session['user_role'] = 'admin'
            session['uproszczony'] = False
            
            flash("Autoryzacja 2FA pomyślna. Witaj, Szefie!", "success")
            return redirect(url_for('admin_page'))
        else:
            flash("Wpisany kod 2FA jest niepoprawny lub wygasł.", "danger")
            
    return render_template('verify_2fa.html')

# Displays user verification page
@app.route('/admin/weryfikacja')
def panel_weryfikacji():
    if session.get('user_role') != 'admin':
        flash("Brak uprawnień do przeglądania tej sekcji.", "danger")
        return redirect(url_for('login_page'))
        
    oczekujacy = Users.query.filter_by(is_approved=False).order_by(Users.id.desc()).all()
    return render_template('admin_weryfikacja.html', uzytkownicy=oczekujacy)

# Approves or rejects user verification requests
@app.route('/admin/weryfikacja/<int:user_id>/<string:akcja>', methods=['POST'])
def przetworz_weryfikacje(user_id, akcja):
    if session.get('user_role') != 'admin':
        return "Brak uprawnień", 403
        
    uzytkownik = Users.query.get_or_404(user_id)
    
    if akcja == 'zatwierdz':
        uzytkownik.is_approved = True
        db.session.commit()
        flash(f"Konto użytkownika {uzytkownik.username} zostało zatwierdzone.", "success")
    elif akcja == 'odrzuc':
        Attendance.query.filter_by(user_id=user_id).delete()
        Schedule.query.filter_by(user_id=user_id).delete()
        db.session.delete(uzytkownik)
        db.session.commit()
        flash(f"Rejestracja użytkownika {uzytkownik.username} została odrzucona.", "warning")
        
    return redirect(url_for('panel_weryfikacji'))

# Resets administrator password and sends alert
@app.route('/reset-admin-password', methods=['POST'])
def reset_admin_password():
    username = request.form.get("username")
    env_admin_name = os.getenv("admin_name") or os.getenv("ADMIN_NAME") or "AdminGreg"
    
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
                flash("Nowe hasło administratora wysłane na Telegram.", "success")
            except Exception as e:
                flash("Coś poszło nie tak przy wysyłaniu powiadomienia.", "danger")
        else:
            flash("Admin nie został jeszcze w pełni zainicjalizowany.", "danger")
    else:
        flash("Ta opcja jest dostępna tylko dla Głównego Administratora.", "danger")
        
    return redirect(url_for('login_page'))

# Adds a new attendance entry for logged user
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
            flash("Wybrana data służby jest nieprawidłowa.", "danger")
            return redirect(url_for('dashboard_page'))

        istniejaca = Attendance.query.filter_by(
            user_id=session['user_id'], 
            data_sluzby=wybrana_data, 
            godzina=godzina
        ).first()

        if istniejaca:
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
        flash("Obecność na służbie zapisana pomyślnie.", "success")
    except Exception as e:
        db.session.rollback()
        flash("Coś poszło nie tak przy zapisywaniu służby.", "danger")

    return redirect(url_for('dashboard_page'))

# Deletes a user by ID
@app.route('/admin/delete_user/<int:id>')
def delete_user(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    user_to_del = Users.query.get_or_404(id)
    Attendance.query.filter_by(user_id=id).delete()
    Schedule.query.filter_by(user_id=id).delete()
    db.session.delete(user_to_del)
    db.session.commit()
    flash(f"Użytkownik {user_to_del.username} został trwale usunięty.", "success")
    return redirect(url_for('admin_page'))

# Edits user details
@app.route('/edit_user/<int:user_id>', methods=['POST'])
def edit_user(user_id):
    if session.get('user_role') != 'admin':
        flash("Brak uprawnień do edycji użytkowników.", "danger")
        return redirect(url_for('login_page'))
        
    user = Users.query.get_or_404(user_id)
    user.imie = request.form.get('imie')
    user.nazwisko = request.form.get('nazwisko')
    user.username = request.form.get('username')
    
    new_role = request.form.get('role')
    user.role = new_role
    user.is_approved = True if request.form.get('is_approved') == 'true' else False
    
    new_password = request.form.get('password')
    if new_role == 'admin' or user.username == os.getenv("ADMIN_NAME", "AdminGreg"):
        if new_password != user.password and not new_password.startswith('pbkdf2:'):
            user.password = generate_password_hash(new_password, method='pbkdf2:sha256')
    else:
        user.password = new_password

    db.session.commit()
    flash(f"Pomyślnie zaktualizowano dane użytkownika {user.username}!", "success")
    return redirect(url_for('admin_page'))

# Deletes an attendance record
@app.route('/admin/delete/<int:id>')
def delete_attendance(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    entry = Attendance.query.get_or_404(id)
    try:
        db.session.delete(entry)
        db.session.commit()
        flash("Wpis o służbie został usunięty.", "success")
    except:
        db.session.rollback()
        flash("Coś poszło nie tak przy usuwaniu wpisu.", "danger")
    return redirect(url_for('admin_page'))

# Edits an attendance record
@app.route('/admin/edit/<int:id>', methods=['POST'])
@app.route('/edit_attendance/<int:id>', methods=['POST'])
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
        flash("Wpis o służbie został zaktualizowany.", "success")
    except:
        db.session.rollback()
        flash("Coś poszło nie tak przy aktualizacji wpisu.", "danger")
    return redirect(url_for('admin_page'))

# Adds attendance directly from admin panel
@app.route('/admin/add_attendance_admin', methods=['POST'])
def add_attendance_admin():
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    user_id = request.form.get("user_id")
    data_str = request.form.get("data_sluzby")
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
        flash("Służba dodana pomyślnie przez Administratora.", "success")
    except Exception as e:
        db.session.rollback()
        flash("Coś poszło nie tak przy dodawaniu służby.", "danger")

    return redirect(url_for('admin_page'))

# Adds a new announcement
@app.route('/admin/add_announcement', methods=['POST'])
def add_announcement():
    if session.get('user_role') not in ['admin', 'ksiądz']:
        return redirect(url_for('login_page'))
    tytul_val = request.form.get('tytul') or "Ogłoszenie"
    nowe = Announcement(tytul=tytul_val, tresc=request.form.get('tresc'))
    db.session.add(nowe)
    db.session.commit()
    flash("Nowe ogłoszenie dodane pomyślnie.", "success")
    if session.get('user_role') == 'ksiądz':
        return redirect(url_for('ksDash'))
    return redirect(url_for('admin_page'))

# Edits an existing announcement
@app.route('/admin/edit_announcement/<int:id>', methods=['POST'])
def edit_announcement(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    ogloszenie = Announcement.query.get_or_404(id)
    ogloszenie.tytul = request.form.get('tytul') or ogloszenie.tytul
    ogloszenie.tresc = request.form.get('tresc')
    db.session.commit()
    flash("Ogłoszenie zaktualizowane.", "success")
    return redirect(url_for('admin_page'))

# Deletes an announcement
@app.route('/admin/delete_announcement/<int:id>')
def delete_announcement(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    ogloszenie = Announcement.query.get_or_404(id)
    db.session.delete(ogloszenie)
    db.session.commit()
    flash("Ogłoszenie usunięte.", "warning")
    return redirect(url_for('admin_page'))

# Adds a schedule entry
@app.route('/admin/add_schedule', methods=['POST'])
def add_schedule():
    if 'user_id' not in session:
        flash("Brak dostępu! Zaloguj się ponownie.", "danger")
        return redirect(url_for('login_page'))
        
    aktualny_uzytkownik = db.session.get(Users, session['user_id'])
    
    if not aktualny_uzytkownik or aktualny_uzytkownik.role not in ['admin', 'ksiądz']:
        flash("Brak dostępu! Nie masz uprawnień do zarządzania dyżurami.", "danger")
        return redirect(url_for('dashboard_page'))
        
    user_id = request.form.get('user_id')
    dzien = request.form.get('dzien')
    godzina = request.form.get('godzina')
    
    if not user_id or not dzien or not godzina:
        flash("Nie udało się zapisać. Wypełnij wszystkie pola formularza!", "warning")
        return redirect(url_for('admin_page'))
        
    try:
        nowy_dyzur = Schedule(
            user_id=int(user_id),
            dzien_tygodnia=dzien,
            godzina=godzina
        )
        db.session.add(nowy_dyzur)
        db.session.commit()
        flash("Służba dodana pomyślnie przez Administratora.", "success")
        return redirect(url_for('admin_page'))
    except Exception as e:
        db.session.rollback()
        flash("Coś poszło nie tak przy dodawaniu służby.", "danger")
        return redirect(url_for('admin_page'))

# Deletes a schedule entry
@app.route('/admin/delete_schedule/<int:id>', methods=['GET', 'POST'])
def delete_schedule(id):
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
        
    aktualny_uzytkownik = db.session.get(Users, session['user_id'])
    if not aktualny_uzytkownik or aktualny_uzytkownik.role not in ['admin', 'ksiądz']:
        flash("Brak uprawnień!", "danger")
        return redirect(url_for('dashboard_page'))
        
    dyzur = db.session.get(Schedule, id)
    if dyzur:
        try:
            db.session.delete(dyzur)
            db.session.commit()
            flash("Służba została usunięta z grafiku.", "success")
        except Exception as e:
            db.session.rollback()
            flash("Błąd podczas usuwania służby.", "danger")
            
    return redirect(url_for('admin_page'))

# Main admin panel view
@app.route('/admin')
def admin_page():
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    
    all_attendance = db.session.query(Attendance, Users).join(Users).order_by(
        Attendance.data_sluzby.desc(), Attendance.godzina.desc()
    ).all()
    
    all_users = Users.query.filter_by(is_approved=True).all()
    pending_users = Users.query.filter_by(is_approved=False).order_by(Users.created_at.desc()).all()
    all_announcements = Announcement.query.order_by(Announcement.data_dodania.desc()).all()
    schedules = db.session.query(Schedule, Users).join(Users, Schedule.user_id == Users.id).all()

    wszystkie_dyzury = Schedule.query.all()
    aktualny_uzytkownik = db.session.get(Users, session['user_id'])
    
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

    all_atts = Attendance.query.all()
    atts_by_user = defaultdict(list)
    for att in all_atts:
        atts_by_user[att.user_id].append(att)

    user_stats = []
    for u in all_users:
        his_atts = atts_by_user[u.id]
        total = len(his_atts)
        morning = sum(1 for a in his_atts if a.typ_mszy == 'poranna')
        evening = sum(1 for a in his_atts if a.typ_mszy == 'wieczorna')
        user_stats.append({
            'username': u.username, 'imie': u.imie, 'nazwisko': u.nazwisko,
            'full_name': f"{u.imie} {u.nazwisko}", 'uproszczony': u.uproszczony,
            'total': total, 'morning': morning, 'evening': evening, 'other': total - (morning + evening)
        })

    unapproved_users = Users.query.filter_by(is_approved=False).all()
    unapproved_count = len(unapproved_users)
    all_users = Users.query.order_by(Users.created_at.desc()).all()
    
    return render_template(
        "admin.html", 
        attendances=all_attendance, 
        users=all_users, 
        pending_users=pending_users,
        announcements=all_announcements, 
        stats=user_stats, 
        plan=plan_tygodnia, 
        liczniki=plan_liczniki,
        unapproved_users=unapproved_users,
        unapproved_count=unapproved_count,
        dyzury=wszystkie_dyzury,
        dyzury_count=len(wszystkie_dyzury),
        user=aktualny_uzytkownik.username
    )

# Inline password change for admin
@app.route('/admin/change_password_inline/<int:user_id>', methods=['POST'])
def admin_change_password_inline(user_id):
    if session.get('role') != 'admin' and session.get('user_role') != 'admin':
        flash('Brak uprawnień bazy szefa!', 'danger')
        return redirect(url_for('login_page'))
        
    user = Users.query.get_or_404(user_id)
    nowe_haslo = request.form.get('new_password')
    
    if nowe_haslo and nowe_haslo.strip() != "":
        user.password = generate_password_hash(nowe_haslo, method='pbkdf2:sha256')
        db.session.commit()
        flash(f'Hasło użytkownika {user.imie} {user.nazwisko} zostało zaktualizowane! 🔑', 'success')
    else:
        flash('Hasło nie może być puste!', 'danger')
        
    return redirect(url_for('admin_page'))

# Approves a single user account
@app.route('/admin/approve_user/<int:user_id>')
def approve_user(user_id):
    if session.get('user_role') != 'admin':
        flash('Brak uprawnień!', 'danger')
        return redirect(url_for('login_page'))
    
    user = Users.query.get_or_404(user_id)
    user.is_approved = True
    db.session.commit()
    flash(f'Konto użytkownika {user.username} zostało zatwierdzone!', 'success')
    return redirect(url_for('admin_page'))

# Rejects and deletes a single user account
@app.route('/admin/reject_user/<int:user_id>')
def reject_user(user_id):
    if session.get('user_role') != 'admin':
        flash('Brak uprawnień!', 'danger')
        return redirect(url_for('login_page'))
    
    user = Users.query.get_or_404(user_id)
    db.session.delete(user)
    db.session.commit()
    flash(f'Konto użytkownika {user.username} zostało odrzucone i usunięte.', 'warning')
    return redirect(url_for('admin_page'))

# Performs action on pending user
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

# Bulk deletion of selected users
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
        flash("Wybrani użytkownicy zostali trwale usunięci.", "success")
    else:
        flash("Proszę zaznaczyć użytkowników do usunięcia.", "warning")
    return redirect(url_for('admin_page'))

# Bulk deletion of attendance entries
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
        flash("Wybrane wpisy o służbach zostały usunięte.", "success")
    else:
        flash("Proszę zaznaczyć służby do usunięcia.", "warning")
    return redirect(url_for('admin_page'))

# Priest dashboard view
@app.route('/ksDash')
def ksDash():
    if session.get('user_role') not in ['admin', 'ksiądz']: 
        flash("Brak uprawnień do panelu duszpasterskiego.", "danger")
        return redirect(url_for('dashboard_page'))
    
    all_attendance = db.session.query(Attendance, Users).join(Users).order_by(
        Attendance.data_sluzby.desc(), Attendance.godzina.desc()
    ).all()
    all_users = Users.query.all()
    all_announcements = Announcement.query.order_by(Announcement.data_dodania.desc()).all()
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

# Regular user dashboard view
@app.route('/dashboard', methods=['GET', 'POST'])
def dashboard_page():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    
    user_id = session['user_id']
    aktualny_uzytkownik = db.session.get(Users, user_id)
    if not aktualny_uzytkownik:
        return redirect(url_for('logout'))

    if request.method == 'POST':
        data_sluzby_str = request.form.get('data_sluzby')
        godzina = request.form.get('godzina')
        typ_mszy = request.form.get('typ')
        nazwa_inna = request.form.get('inna') if typ_mszy == 'inna' else None
        
        try:
            data_sluzby = datetime.strptime(data_sluzby_str, '%Y-%m-%d').date()
            dzisiaj = date.today()
            wczoraj = dzisiaj - timedelta(days=1)
            hard_limit = date(2026, 4, 12)

            if data_sluzby > dzisiaj or data_sluzby < max(wczoraj, hard_limit):
                flash("Wybrana data służby jest nieprawidłowa.", "danger")
                return redirect(url_for('dashboard_page'))

            istniejaca = Attendance.query.filter_by(
                user_id=user_id, 
                data_sluzby=data_sluzby, 
                godzina=godzina
            ).first()

            if istniejaca:
                flash("Masz już zgłoszoną służbę o tej godzinie.", "danger")
                return redirect(url_for('dashboard_page'))

            new_attendance = Attendance(
                user_id=user_id,
                data_sluzby=data_sluzby,
                typ_mszy=typ_mszy,
                nazwa_inna=nazwa_inna,
                godzina=godzina
            )
            db.session.add(new_attendance)
            db.session.commit()
            flash('Obecność na służbie zapisana pomyślnie.', 'success')
        except Exception as e:
            db.session.rollback()
            flash("Coś poszło nie tak przy zapisywaniu służby.", "danger")
        return redirect(url_for('dashboard_page'))
        
    announcements = Announcement.query.order_by(Announcement.id.desc()).all()
    attendances = Attendance.query.filter_by(user_id=user_id).order_by(Attendance.data_sluzby.desc()).all()
    
    moje_dyzury = Schedule.query.filter_by(user_id=user_id).order_by(Schedule.dzien_tygodnia, Schedule.godzina).all()
    schedules = Schedule.query.options(joinedload(Schedule.user)).all()
    
    dni_kolejnosc = ['Poniedziałek', 'Wtorek', 'Środa', 'Czwartek', 'Piątek', 'Sobota', 'Niedziela']
    plan_tygodnia = {dzien: {} for dzien in dni_kolejnosc}
    plan_liczniki = {dzien: 0 for dzien in dni_kolejnosc}
    
    for sch in schedules:
        dzien = sch.dzien_tygodnia
        godzina = sch.godzina
        if dzien in plan_tygodnia:
            if godzina not in plan_tygodnia[dzien]:
                plan_tygodnia[dzien][godzina] = []
            plan_tygodnia[dzien][godzina].append(sch.user)
            plan_liczniki[dzien] += 1
            
    for dzien in plan_tygodnia:
        plan_tygodnia[dzien] = dict(sorted(plan_tygodnia[dzien].items()))
    
    if aktualny_uzytkownik.uproszczony:
        return render_template('dash_uproszczony.html', user=aktualny_uzytkownik.username, announcements=announcements, attendances=attendances)
    return render_template('dashboard.html', user=aktualny_uzytkownik.username, announcements=announcements, attendances=attendances, moje_dyzury=moje_dyzury, plan=plan_tygodnia, liczniki=plan_liczniki)

# Deletes personal attendance entry
@app.route('/delete_my_attendance/<int:id>')
def delete_my_attendance(id):
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    entry = Attendance.query.get_or_404(id)
    if entry.user_id != session['user_id']:
        flash("Coś poszło nie tak. Nie można usunąć wpisu.", "danger")
        return redirect(url_for('dashboard_page'))
    try:
        db.session.delete(entry)
        db.session.commit()
        flash("Wpis o Twojej służbie został usunięty.", "success")
    except:
        db.session.rollback()
        flash("Coś poszło nie tak przy usuwaniu służby.", "danger")
    return redirect(url_for('dashboard_page'))

# Edits personal attendance entry
@app.route('/edit_my_attendance/<int:id>', methods=['POST'])
def edit_my_attendance(id):
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    
    att = Attendance.query.get_or_404(id)
    if att.user_id != session['user_id']:
        flash('Brak uprawnień do edycji tej służby.', 'danger')
        return redirect(url_for('dashboard_page'))
        
    att.data_sluzby = datetime.strptime(request.form.get('data_sluzby'), '%Y-%m-%d').date()
    att.godzina = request.form.get('godzina')
    att.typ_mszy = request.form.get('typ')
    att.nazwa_inna = request.form.get('inna') if att.typ_mszy == 'inna' else None
    
    db.session.commit()
    flash('Zgłoszenie zostało pomyślnie zaktualizowane.', 'success')
    return redirect(url_for('dashboard_page'))

# Clears user session and logs out
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

# Renders password recovery page
@app.route('/forget-password')
def forget_password():
    return render_template('forget-password.html')

# Serves static files from root
@app.route('/robots.txt')
def static_from_root():
    return send_from_directory(app.static_folder, request.path[1:])

# Serves sitemap XML
@app.route('/sitemap.xml')
def sitemap_from_root():
    return send_from_directory(app.static_folder, request.path[1:])

# Exports full attendance list to formatted Excel with pie chart
@app.route('/admin/export_attendances')
def export_attendances():
    if session.get('user_role') not in ['admin', 'ksiądz']:
        flash("Brak uprawnień do eksportu danych.", "danger")
        return redirect(url_for('login_page'))

    attendances = db.session.query(Attendance, Users).join(Users).order_by(
        Attendance.data_sluzby.desc(), Attendance.godzina.desc()
    ).all()

    wb = openpyxl.Workbook()
    
    # --- ARKUSZ 1: LISTA SŁUŻB ---
    ws = wb.active
    ws.title = "Lista Służb"
    ws.views.sheetView[0].showGridLines = True

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="5E3BEE", end_color="5E3BEE", fill_type="solid")
    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")
    thin_border = Border(
        left=Side(style='thin', color='D1D5DB'),
        right=Side(style='thin', color='D1D5DB'),
        top=Side(style='thin', color='D1D5DB'),
        bottom=Side(style='thin', color='D1D5DB')
    )

    headers = ["ID", "Imię i Nazwisko", "Login", "Data Służby", "Godzina", "Typ Liturgii", "Nazwa Inna / Uwagi"]
    ws.append(headers)

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align_center
        cell.border = thin_border

    typ_counts = defaultdict(int)

    for idx, (att, u) in enumerate(attendances, start=2):
        typ_label = att.typ_mszy.capitalize() if att.typ_mszy else "Nieokreślony"
        typ_counts[typ_label] += 1

        row = [
            att.id,
            f"{u.imie} {u.nazwisko}",
            u.username,
            att.data_sluzby.strftime("%Y-%m-%d") if att.data_sluzby else "",
            att.godzina,
            typ_label,
            att.nazwa_inna or ""
        ]
        ws.append(row)

        for col_num in range(1, len(headers) + 1):
            c = ws.cell(row=idx, column=col_num)
            c.border = thin_border
            c.alignment = align_center if col_num in [1, 4, 5, 6] else align_left

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    # --- ARKUSZ 2: STATYSTYKI I WYKRES ---
    ws_chart = wb.create_sheet(title="Statystyki i Wykres")
    ws_chart.views.sheetView[0].showGridLines = True

    ws_chart.append(["Typ Liturgii", "Liczba Służb"])
    ws_chart.cell(row=1, column=1).font = header_font
    ws_chart.cell(row=1, column=1).fill = header_fill
    ws_chart.cell(row=1, column=2).font = header_font
    ws_chart.cell(row=1, column=2).fill = header_fill

    r_idx = 2
    for t_name, count in typ_counts.items():
        ws_chart.append([t_name, count])
        ws_chart.cell(row=r_idx, column=1).border = thin_border
        ws_chart.cell(row=r_idx, column=2).border = thin_border
        r_idx += 1

    chart = PieChart()
    chart.title = "Rozkład Służb według Typu Liturgii"
    labels = Reference(ws_chart, min_col=1, min_row=2, max_row=r_idx - 1)
    data = Reference(ws_chart, min_col=2, min_row=1, max_row=r_idx - 1)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(labels)
    chart.width = 16
    chart.height = 10
    ws_chart.add_chart(chart, "D2")

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"ewidencja_sluzb_{date.today()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

# Exports user rankings to an Excel sheet
@app.route("/admin/export_ranking")
def export_ranking():
    users = Users.query.filter(Users.role == 'user').all()
    attendances = Attendance.query.all()
    
    morning_count = defaultdict(int)
    evening_count = defaultdict(int)
    other_count = defaultdict(int)
    total_count = defaultdict(int)
    
    for att in attendances:
        total_count[att.user_id] += 1
        time_str = att.godzina.replace(":", "")
        try:
            hour_val = int(time_str[:2])
            if hour_val < 12:
                morning_count[att.user_id] += 1
            elif hour_val >= 17:
                evening_count[att.user_id] += 1
            else:
                other_count[att.user_id] += 1
        except ValueError:
            other_count[att.user_id] += 1

    data = []
    for u in users:
        tot = total_count[u.id]
        level = "LIDER" if tot >= 20 else ("AKTYWNY" if tot >= 5 else "POCZĄTKUJĄCY")
        data.append({
            "Login": u.username,
            "Imię i Nazwisko": f"{u.imie} {u.nazwisko}",
            "Suma Służb (Punkty)": tot,
            "Służby Poranne": morning_count[u.id],
            "Służby Wieczorne": evening_count[u.id],
            "Inne / Dodatkowe": other_count[u.id],
            "Status Aktywności": level
        })
        
    df = pd.DataFrame(data).sort_values(by="Suma Służb (Punkty)", ascending=False)
    buffer = io.BytesIO()
    df.to_excel(buffer, index=False, sheet_name="Cyfrowy Ranking")
    buffer.seek(0)
    
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"cyfrowy_ranking_{date.today()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

# Exports weekly schedule to Excel
@app.route('/export_schedule')
def export_schedule():
    if 'user_id' not in session: 
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

# Toggles user simplified view mode
@app.route('/admin/toggle_uproszczony/<int:id>', methods=['GET', 'POST'])
def toggle_uproszczony(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('login_page'))
    u = Users.query.get_or_404(id)
    u.uproszczony = not u.uproszczony
    db.session.commit()
    flash(f"Zmieniono tryb wyświetlania dla użytkownika {u.username}.", "success")
    return redirect(url_for('admin_page'))

# Downloads terms of service PDF
@app.route('/download/regulamin.pdf')
def pobierz_regulamin():
    katalog = os.path.join(app.root_path, 'static', 'docs')
    return send_from_directory(
        katalog, 
        'regulamin.pdf', 
        as_attachment=True, 
        download_name='regulamin.pdf'
    )

# Renders help page
@app.route('/pomoc')
def pomoc_page():
    return render_template('pomoc.html')

with app.app_context(): 
    db.create_all()
    env_admin_name = os.getenv("ADMIN_NAME") or os.getenv("admin_name") or "AdminGreg"
    env_admin_pass = os.getenv("ADMIN_PASSWORD") or os.getenv("admin_password") or "Lego2012"
    
    admin_user = Users.query.filter_by(username=env_admin_name).first()
    if not admin_user:
        hashed_admin_pass = generate_password_hash(env_admin_pass, method='pbkdf2:sha256')
        admin_user = Users(
            imie="Główny", 
            nazwisko="Szef", 
            username=env_admin_name, 
            password=hashed_admin_pass, 
            role='admin', 
            uproszczony=False,
            is_approved=True
        )
        db.session.add(admin_user)
        db.session.commit()
    elif not admin_user.password.startswith('pbkdf2:'):
        admin_user.password = generate_password_hash(env_admin_pass, method='pbkdf2:sha256')
        admin_user.is_approved = True
        db.session.commit()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)