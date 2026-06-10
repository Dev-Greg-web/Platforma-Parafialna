from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Users(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    imie = db.Column(db.String(50), nullable=False)
    nazwisko = db.Column(db.String(50), nullable=False)
    username = db.Column(db.String(50), nullable=False, unique=True)
    password = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), default='user')
    uproszczony = db.Column(db.Boolean, default=False)
    
    created_at = db.Column(db.DateTime, default=datetime.now)  
    registration_ip = db.Column(db.String(45), nullable=True)  
    latitude = db.Column(db.String(30), nullable=True)
    longitude = db.Column(db.String(30), nullable=True)

class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    data_sluzby = db.Column(db.Date, nullable=False)
    typ_mszy = db.Column(db.String(20), nullable=False) 
    nazwa_inna = db.Column(db.String(100), nullable=True)
    godzina = db.Column(db.String(5), nullable=False)
    data_wpisu = db.Column(db.DateTime, default=datetime.now)

class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tresc = db.Column(db.String(500), nullable=False)
    data_wystawienia = db.Column(db.DateTime, default=datetime.now)

class Schedule(db.Model):
    __tablename__ = "schedule"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    dzien_tygodnia = db.Column(db.String(20), nullable=False)
    godzina = db.Column(db.String(10), nullable=False)
