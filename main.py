from flask import Flask, render_template, request, flash, redirect, url_for, session, send_from_directory, send_file
from models import Users, Attendance, Announcement, Schedule, db
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta, date
import os
from dotenv import load_dotenv
import pandas as pd
import io

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("TAJNE_HASLO")
db_url = os.getenv("DATABASE_URL", "sqlite:///ministranci.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.permanent_session_lifetime = timedelta(minutes=15)

db.init_app(app)

# --- WYMUSZANIE ŚWIEŻYCH DANYCH (Brak opóźnień w wyświetlaniu) ---
@app.after_request
def add_header(response):
    # Wymuszamy na przeglądarce pobranie świeżego HTML-a za każdym razem
    if 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '-1'
    return response

# --- TRASY WIDOKU ---

@app.route('/')
def login_page():
    if 'user_id' in session or 'user_role' in session:
        return redirect(url_for('dashboard_page'))
    return render_template('login.html')

# --- LOGIKA PROCESOWA (AUTH & CRUD) ---

@app.route("/auth_process", methods=['POST'])
def auth_process():
    action = request.form.get("action")
    username = request.form.get("username")
    password = request.form.get("haslo")
    
    # Odczyt danych Szefa prosto z pliku .env
    env_admin_name = os.getenv("admin_name")
    env_admin_pass = os.getenv("admin_password")

    if action == "login":
        # 1. PRIORYTET: Logowanie Szefa z pliku .env
        if username == env_admin_name and password == env_admin_pass:
            session.clear()
            
            # Zabezpieczenie: Tworzymy awaryjne konto w bazie dla Szefa .env, 
            # żeby miał swoje poprawne ID przy dodawaniu własnych służb!
            admin_in_db = Users.query.filter_by(username=username).first()
            if not admin_in_db:
                admin_in_db = Users(imie="Grześ", nazwisko="Gładysz", username=username, password=password, role='admin')
                db.session.add(admin_in_db)
                db.session.commit()
                
            session['user_id'] = admin_in_db.id
            session['username'] = admin_in_db.username
            session['user_role'] = 'admin'
            flash("Witaj Szefie! System gotowy.", "success")
            return redirect(url_for('admin_page'))

        # 2. Zwykłe logowanie z Bazy Danych
        user = Users.query.filter_by(username=username).first()
        if user and user.password == password:
            session.clear()
            session['user_id'] = user.id
            session['username'] = user.username
            session['user_role'] = user.role
            
            if user.role == 'admin':
                flash("Witaj Szefie! System gotowy.", "success")
                return redirect(url_for('admin_page'))
            elif user.role == 'ksiądz':
                flash("Szczęść Boże! Panel gotowy.", "success")
                return redirect(url_for('ksDash'))
            else:
                flash(f"Cześć {user.imie}! Zaraz Cię wpuścimy...", "success")
                return redirect(url_for('dashboard_page'))
        
        flash("Błędna nazwa użytkownika lub hasło.", "danger")
        return redirect(url_for('login_page'))

    elif action == "register":
        user = Users.query.filter_by(username=username).first()
        # Zabroniona rejestracja na login szefa z env
        if user or username == env_admin_name:
            flash("Ta nazwa jest zajęta!", "danger")
        else:
            new_user = Users(
                imie=request.form.get("imie"), 
                nazwisko=request.form.get("nazwisko"), 
                username=username, 
                password=password,
                role='user' 
            )
            db.session.add(new_user)
            db.session.commit()
            flash("Konto stworzone! Możesz się zalogować.", "success")
        return redirect(url_for('login_page'))

@app.route('/add_attendance', methods=['POST'])
def add_attendance():
    if 'user_id' not in session: return redirect(url_for('login_page'))

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
        flash(f"Wystąpił błąd: {e}", "danger")

    return redirect(url_for('dashboard_page'))

# --- ADMIN: Zarządzanie Użytkownikami ---

@app.route('/admin/delete_user/<int:id>')
def delete_user(id):
    if session.get('user_role') != 'admin': return redirect(url_for('login_page'))
    user_to_del = Users.query.get_or_404(id)
    Attendance.query.filter_by(user_id=id).delete()
    Schedule.query.filter_by(user_id=id).delete() # Usunięcie z planu służb
    db.session.delete(user_to_del)
    db.session.commit()
    flash(f"Użytkownik {user_to_del.username} usunięty.", "success")
    return redirect(url_for('admin_page'))

@app.route('/admin/edit_user/<int:id>', methods=['POST'])
def edit_user(id):
    if session.get('user_role') != 'admin': return redirect(url_for('login_page'))
    u = Users.query.get_or_404(id)
    
    u.imie = request.form.get('imie')
    u.nazwisko = request.form.get('nazwisko')
    u.username = request.form.get('username')
    u.password = request.form.get('password')
    u.role = request.form.get('role') 
    
    try:
        db.session.commit()
        flash("Dane użytkownika zaktualizowane!", "success")
    except:
        db.session.rollback()
        flash("Błąd podczas edycji użytkownika.", "danger")
    return redirect(url_for('admin_page'))

# --- ADMIN: Zarządzanie Służbami ---

@app.route('/admin/delete/<int:id>')
def delete_entry(id):
    if session.get('user_role') != 'admin': return redirect(url_for('login_page'))
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
    if session.get('user_role') != 'admin': return redirect(url_for('login_page'))
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
    if session.get('user_role') != 'admin': return redirect(url_for('login_page'))

    user_id = request.form.get("user_id")
    data_str = request.form.get("date")
    typ_mszy = request.form.get("typ_mszy")
    nazwa_inna = request.form.get("nazwa_inna")
    godzina = request.form.get("godzina")

    try:
        wybrana_data = date.fromisoformat(data_str)
        nowa_sluzba = Attendance(
            user_id=user_id, data_sluzby=wybrana_data, typ_mszy=typ_mszy,
            nazwa_inna=nazwa_inna if typ_mszy == 'inna' else None, godzina=godzina
        )
        db.session.add(nowa_sluzba)
        db.session.commit()
        flash("Służba została dodana przez Szefa!", "success")
    except Exception as e:
        db.session.rollback()
        flash("Wystąpił błąd podczas dodawania służby.", "danger")

    return redirect(url_for('admin_page'))

# --- ADMIN: Zarządzanie Ogłoszeniami ---

@app.route('/admin/add_announcement', methods=['POST'])
def add_announcement():
    if session.get('user_role') not in ['admin', 'ksiądz']: return redirect(url_for('login_page'))
    nowe = Announcement(tresc=request.form.get('tresc'))
    db.session.add(nowe)
    db.session.commit()
    flash("Ogłoszenie dodane!", "success")
    if session.get('user_role') == 'ksiądz':
        return redirect(url_for('ksDash'))
    return redirect(url_for('admin_page'))

@app.route('/admin/edit_announcement/<int:id>', methods=['POST'])
def edit_announcement(id):
    if session.get('user_role') != 'admin': return redirect(url_for('login_page'))
    ogloszenie = Announcement.query.get_or_404(id)
    ogloszenie.tresc = request.form.get('tresc')
    db.session.commit()
    return redirect(url_for('admin_page'))

@app.route('/admin/delete_announcement/<int:id>')
def delete_announcement(id):
    if session.get('user_role') != 'admin': return redirect(url_for('login_page'))
    ogloszenie = Announcement.query.get_or_404(id)
    db.session.delete(ogloszenie)
    db.session.commit()
    return redirect(url_for('admin_page'))

# --- PLAN SŁUŻB (DYŻURY) ---

@app.route('/admin/add_schedule', methods=['POST'])
def add_schedule():
    if session.get('user_role') not in ['admin', 'ksiądz']: return redirect(url_for('login_page'))
    nowy_dyzur = Schedule(
        user_id=request.form.get("user_id"),
        dzien_tygodnia=request.form.get("dzien"),
        godzina=request.form.get("godzina")
    )
    db.session.add(nowy_dyzur)
    db.session.commit()
    flash("Dodano dyżur do planu!", "success")
    return redirect(request.referrer)

@app.route('/admin/delete_schedule/<int:id>')
def delete_schedule(id):
    if session.get('user_role') not in ['admin', 'ksiądz']: return redirect(url_for('login_page'))
    dyzur = Schedule.query.get(id)
    if dyzur:
        db.session.delete(dyzur)
        db.session.commit()
        flash("Usunięto dyżur z planu.", "success")
    return redirect(request.referrer)

# --- AKTUALIZACJA WIDOKÓW ---

@app.route('/admin')
def admin_page():
    if session.get('user_role') != 'admin': return redirect(url_for('login_page'))
    
    # Sortujemy by najnowsze i najpóźniejsze w danym dniu były wyżej
    all_attendance = db.session.query(Attendance, Users).join(Users).order_by(
        Attendance.data_sluzby.desc(), Attendance.godzina.desc()
    ).all()
    all_users = Users.query.all()
    all_announcements = Announcement.query.order_by(Announcement.data_wystawienia.desc()).all()
    schedules = db.session.query(Schedule, Users).join(Users, Schedule.user_id == Users.id).all()
    
    plan_tygodnia = {'Poniedziałek': [], 'Wtorek': [], 'Środa': [], 'Czwartek': [], 'Piątek': [], 'Sobota': [], 'Niedziela': []}
    for sch, u in schedules:
        plan_tygodnia[sch.dzien_tygodnia].append({'id': sch.id, 'user': u, 'godzina': sch.godzina})
    for dzien in plan_tygodnia:
        plan_tygodnia[dzien] = sorted(plan_tygodnia[dzien], key=lambda x: x['godzina'])
    
    # ZOPTYMALIZOWANE ZLICZANIE (Eliminuje zacięcia serwera przy dużej liczbie wpisów)
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
            'username': u.username, 'full_name': f"{u.imie} {u.nazwisko}",
            'total': total, 'morning': morning, 'evening': evening, 'other': other
        })
    
    return render_template("admin.html", attendances=all_attendance, users=all_users, announcements=all_announcements, stats=user_stats, plan=plan_tygodnia)

# --- ADMIN: Zbiorcze usuwanie ---

@app.route('/admin/delete_bulk_users', methods=['POST'])
def delete_bulk_users():
    if session.get('user_role') != 'admin': return redirect(url_for('login_page'))
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
    if session.get('user_role') != 'admin': return redirect(url_for('login_page'))
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
    
    plan_tygodnia = {'Poniedziałek': [], 'Wtorek': [], 'Środa': [], 'Czwartek': [], 'Piątek': [], 'Sobota': [], 'Niedziela': []}
    for sch, u in schedules:
        plan_tygodnia[sch.dzien_tygodnia].append({'id': sch.id, 'user': u, 'godzina': sch.godzina})
    for dzien in plan_tygodnia:
        plan_tygodnia[dzien] = sorted(plan_tygodnia[dzien], key=lambda x: x['godzina'])
    
    # ZOPTYMALIZOWANE ZLICZANIE
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
            'username': u.username, 'full_name': f"{u.imie} {u.nazwisko}",
            'total': total, 'morning': morning, 'evening': evening, 'other': other
        })

    return render_template('ks.html', attendances=all_attendance, users=all_users, announcements=all_announcements, stats=user_stats, plan=plan_tygodnia)

@app.route('/dashboard_view')
def dashboard_page():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    announcements = Announcement.query.order_by(Announcement.data_wystawienia.desc()).all()
    dzisiaj = date.today()
    min_date = max(dzisiaj - timedelta(days=1), date(2026, 4, 12))
    user_attendances = Attendance.query.filter_by(user_id=session['user_id']).order_by(Attendance.data_sluzby.desc()).all()

    return render_template('dashboard.html', 
                           user=session.get('username'), 
                           announcements=announcements,
                           today=dzisiaj.strftime('%Y-%m-%d'), 
                           min_date=min_date.strftime('%Y-%m-%d'),
                           attendances=user_attendances)

# --- MINISTRANT: Zarządzanie swoimi służbami ---

@app.route('/delete_my_attendance/<int:id>')
def delete_my_attendance(id):
    if 'user_id' not in session: return redirect(url_for('login_page'))
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
    if 'user_id' not in session: return redirect(url_for('login_page'))
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
    
    # ZOPTYMALIZOWANE ZLICZANIE DLA RAPORTU
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

with app.app_context(): 
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True)