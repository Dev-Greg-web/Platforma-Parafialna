from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Users(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    imie = db.Column(db.String(50), nullable=False)
    nazwisko = db.Column(db.String(50), nullable=False)
    username = db.Column(db.String(50), nullable=False, unique=True)
    password = db.Column(db.String(255), nullable=False)  
    role = db.Column(db.String(20), default='user')
    uproszczony = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    # POPRAWKA: Zwiększono limit do 255 znaków, aby baza PostgreSQL na Render nie odrzucała długich wpisów z IPinfo
    registration_ip = db.Column(db.String(255), nullable=True)
    
    latitude = db.Column(db.String(30), nullable=True)
    longitude = db.Column(db.String(30), nullable=True)
    
    two_factor_code = db.Column(db.String(20), nullable=True)
    two_factor_expiry = db.Column(db.DateTime, nullable=True)
    is_approved = db.Column(db.Boolean, default=False)

class Attendance(db.Model):
    __tablename__ = "attendance"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    data_sluzby = db.Column(db.Date, nullable=False, index=True)
    typ_mszy = db.Column(db.String(20), nullable=False) 
    nazwa_inna = db.Column(db.String(100), nullable=True)
    godzina = db.Column(db.String(5), nullable=False)
    data_wpisu = db.Column(db.DateTime, default=datetime.now)
    
    user = db.relationship('Users', backref=db.backref('attendances', lazy=True))

class Announcement(db.Model):
    __tablename__ = "announcement"
    id = db.Column(db.Integer, primary_key=True)
    tytul = db.Column(db.String(100), nullable=False)
    tresc = db.Column(db.Text, nullable=False)
    data_dodania = db.Column(db.DateTime, default=datetime.now)

class Schedule(db.Model):
    __tablename__ = "schedule"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    dzien_tygodnia = db.Column(db.String(20), nullable=False)
    godzina = db.Column(db.String(5), nullable=False)

    user = db.relationship('Users', backref=db.backref('schedules', lazy=True))