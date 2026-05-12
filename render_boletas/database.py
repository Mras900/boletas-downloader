import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

# Configuración: Por defecto usará un archivo SQLite local, 
# pero si pones una variable de entorno DATABASE_URL, se conectará a tu servidor en casa (ej. SQL Server, PostgreSQL)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///boletas_guardadas.db")

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Boleta(Base):
    __tablename__ = "boletas"

    id = Column(Integer, primary_key=True, index=True)
    folio = Column(String(100), index=True, nullable=False)
    fecha_emision = Column(DateTime, index=True, nullable=True)
    ruta_pdf = Column(String(500), nullable=False)
    fecha_descarga = Column(DateTime, default=datetime.utcnow)

# Crear las tablas automáticamente si no existen
Base.metadata.create_all(bind=engine)

def guardar_boleta(folio: str, ruta_pdf: str, fecha_emision=None):
    with SessionLocal() as db:
        # Verificar si ya existe para no duplicar (Opcional, pero recomendado)
        existente = db.query(Boleta).filter(Boleta.folio == folio).first()
        if existente:
            existente.ruta_pdf = ruta_pdf
            if fecha_emision:
                existente.fecha_emision = fecha_emision
            existente.fecha_descarga = datetime.utcnow()
        else:
            nueva = Boleta(
                folio=folio,
                fecha_emision=fecha_emision,
                ruta_pdf=ruta_pdf
            )
            db.add(nueva)
        db.commit()

def buscar_boletas(query_folio: str = "", query_fecha=None):
    with SessionLocal() as db:
        consulta = db.query(Boleta)
        if query_folio:
            consulta = consulta.filter(Boleta.folio.ilike(f"%{query_folio}%"))
        
        # Filtro muy básico por fecha
        if query_fecha:
            # query_fecha puede ser un datetime.date, buscamos desde inicio hasta final del día
            from datetime import timedelta
            inicio_dia = datetime.combine(query_fecha, datetime.min.time())
            fin_dia = inicio_dia + timedelta(days=1, seconds=-1)
            consulta = consulta.filter(Boleta.fecha_emision.between(inicio_dia, fin_dia))
            
        return consulta.order_by(Boleta.fecha_descarga.desc()).all()
