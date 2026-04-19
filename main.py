from flask import Flask, render_template, request, flash, redirect, url_for, session, send_from_directory, send_file
from models import Users, Attendance, Announcement, db
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
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///ministranci.db"
app.permanent_session_lifetime = timedelta(minutes=15)

admin_username = os.getenv("admin_name")
admin_pass = os.getenv("admin_password")

db.init_app(app)

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
    user = Users.query.filter_by(username=username).first()

    if action == "login":
        if user and user.password == password:
            session.clear()
            session['user_id'] = user.id
            session['username'] = user.username
            
            # SPRAWDZAMY CZY TO SZEF Z PLIKU .ENV
            is_env_admin = (username == os.getenv("admin_name") and password == os.getenv("admin_password"))
            
            if user.role == 'admin' or is_env_admin:
                session['user_role'] = 'admin' # WYMUSZAMY RANGĘ ADMINA W SESJI
                flash("Witaj Szefie! System gotowy.", "success")
                return redirect(url_for('admin_page'))
            elif user.role == 'ksiądz':
                session['user_role'] = 'ksiądz'
                flash("Szczęść Boże! Panel gotowy.", "success")
                return redirect(url_for('ksDash'))
            else:
                session['user_role'] = 'user'
                flash(f"Cześć {user.imie}! Zaraz Cię wpuścimy...", "success")
                return redirect(url_for('dashboard_page'))
        
        flash("Błędna nazwa użytkownika lub hasło.", "danger")
        return redirect(url_for('login_page'))

    elif action == "register":
        if user:
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
    # ZAPIS NOWEJ RANGI
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

# --- ADMIN: Zarządzanie Ogłoszeniami ---

@app.route('/admin/add_announcement', methods=['POST'])
def add_announcement():
    if session.get('user_role') not in ['admin', 'ksiądz']: return redirect(url_for('login_page'))
    nowe = Announcement(tresc=request.form.get('tresc'))
    db.session.add(nowe)
    db.session.commit()
    flash("Ogłoszenie dodane!", "success")
    # Redirect do odpowiedniego panelu w zależności kto dodał
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

# --- AKTUALIZACJA WIDOKÓW ---

@app.route('/admin')
def admin_page():
    if session.get('user_role') != 'admin': return redirect(url_for('login_page'))
    
    all_attendance = db.session.query(Attendance, Users).join(Users).order_by(Attendance.data_sluzby.desc()).all()
    all_users = Users.query.all()
    all_announcements = Announcement.query.order_by(Announcement.data_wystawienia.desc()).all()
    
    user_stats = []
    for u in all_users:
        his_atts = [att for att, usr in all_attendance if usr.id == u.id]
        total = len(his_atts)
        morning = len([a for a in his_atts if a.typ_mszy == 'poranna'])
        evening = len([a for a in his_atts if a.typ_mszy == 'wieczorna'])
        other = total - (morning + evening)
        
        user_stats.append({
            'username': u.username,
            'full_name': f"{u.imie} {u.nazwisko}",
            'total': total,
            'morning': morning,
            'evening': evening,
            'other': other
        })
    
    return render_template("admin.html", attendances=all_attendance, users=all_users, announcements=all_announcements, stats=user_stats)

@app.route('/ksDash')
def ksDash():
    # KULOODPORNY BRAMKARZ
    if session.get('user_role') not in ['admin', 'ksiądz']: 
        flash("Nie masz uprawnień do wejścia na ten panel!", "danger")
        return redirect(url_for('dashboard_page'))
    
    all_attendance = db.session.query(Attendance, Users).join(Users).order_by(Attendance.data_sluzby.desc()).all()
    all_users = Users.query.all()
    all_announcements = Announcement.query.order_by(Announcement.data_wystawienia.desc()).all()
    
    user_stats = []
    for u in all_users:
        his_atts = [att for att, usr in all_attendance if usr.id == u.id]
        total = len(his_atts)
        morning = len([a for a in his_atts if a.typ_mszy == 'poranna'])
        evening = len([a for a in his_atts if a.typ_mszy == 'wieczorna'])
        other = total - (morning + evening)
        
        user_stats.append({
            'username': u.username,
            'full_name': f"{u.imie} {u.nazwisko}",
            'total': total,
            'morning': morning,
            'evening': evening,
            'other': other
        })

    return render_template('ks.html', attendances=all_attendance, users=all_users, announcements=all_announcements, stats=user_stats)

@app.route('/dashboard_view')
def dashboard_page():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    announcements = Announcement.query.order_by(Announcement.data_wystawienia.desc()).all()
    
    dzisiaj = date.today()
    min_date = max(dzisiaj - timedelta(days=1), date(2026, 4, 12))
    return render_template('dashboard.html', 
                           user=session.get('username'), 
                           announcements=announcements,
                           today=dzisiaj.strftime('%Y-%m-%d'), 
                           min_date=min_date.strftime('%Y-%m-%d'))

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
    # 1. BRAMKARZ: Tylko Szef i Ksiądz mogą pobierać raporty
    if session.get('user_role') not in ['admin', 'ksiądz']: 
        flash("Brak uprawnień do pobierania raportów.", "danger")
        return redirect(url_for('dashboard_page'))

    # 2. Pobieramy wszystkie dane z bazy
    all_attendance = db.session.query(Attendance, Users).join(Users).all()
    all_users = Users.query.all()
    
    # 3. Zwijamy dane (Przetwarzanie)
    data = []
    for u in all_users:
        his_atts = [att for att, usr in all_attendance if usr.id == u.id]
        total = len(his_atts)
        morning = len([a for a in his_atts if a.typ_mszy == 'poranna'])
        evening = len([a for a in his_atts if a.typ_mszy == 'wieczorna'])
        other = total - (morning + evening)
        
        data.append({
            'Imię i Nazwisko': f"{u.imie} {u.nazwisko}",
            'Pseudonim (Login)': u.username,
            'Suma Służb': total,
            'Poranne': morning,
            'Wieczorne': evening,
            'Inne': other
        })

    # 4. Magia PANDAS - Tworzymy DataFrame (Tabelę analityczną)
    df = pd.DataFrame(data)
    
    # Sortujemy automatycznie od najlepszego do najgorszego!
    df = df.sort_values(by='Suma Służb', ascending=False)

    # 5. Zapisujemy do pamięci RAM (Zamiast na dysk serwera)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Ranking_Ministrantow')
    
    output.seek(0) # Cofamy "kursor" zapisu na początek pliku

    # 6. Wysyłamy plik bezpośrednio do przeglądarki użytkownika
    nazwa_pliku = f"Raport_Ministranci_{date.today().strftime('%Y-%m-%d')}.xlsx"
    return send_file(output, download_name=nazwa_pliku, as_attachment=True)

with app.app_context(): 
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True)
