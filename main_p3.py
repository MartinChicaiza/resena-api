from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

client = MongoClient(os.environ["MONGO_URI_P3"])
db = client["ISIS2304E05202610"]
resenas = db["resenas"]


def serializar(doc):
    """Convierte ObjectId a string para que FastAPI pueda serializar el JSON."""
    doc["_id"] = str(doc["_id"])
    return doc


# ──────────────────────────────────────────────
# HEALTH CHECK
# ──────────────────────────────────────────────

@app.get("/")
def inicio():
    return {"estado": "API Dann-Alpes funcionando correctamente"}


# ──────────────────────────────────────────────
# RF1 – CREAR RESEÑA
# Body: { hotel_id, cliente_id, reserva_id, calificacion, texto }
# Regla: solo si la reserva está completada y no existe reseña previa.
# La validación de reserva completada se hace desde Oracle/APEX antes de llamar este endpoint.
# ──────────────────────────────────────────────

@app.post("/resenas")
def crear_resena(datos: dict):
    # Verificar que no exista ya una reseña para esa reserva
    existente = resenas.find_one({"reserva_id": datos.get("reserva_id")})
    if existente:
        raise HTTPException(status_code=400, detail="Ya existe una reseña para esta reserva.")

    doc = {
        "hotel_id":            int(datos["hotel_id"]),
        "cliente_id":          int(datos["cliente_id"]),
        "reserva_id":          int(datos["reserva_id"]),
        "calificacion":        int(datos["calificacion"]),
        "texto":               datos["texto"],
        "fecha_creacion":      datetime.utcnow(),
        "fecha_ultima_edicion": datetime.utcnow(),
        "estado":              "publicada",
        "destacada":           False,
        "votos_utilidad":      0,
        "votos_lista":         []
    }
    resultado = resenas.insert_one(doc)
    return {"mensaje": "Reseña creada", "id": str(resultado.inserted_id)}


# ──────────────────────────────────────────────
# RF2 – EDITAR RESEÑA (cliente edita su propia reseña)
# Body: { calificacion, texto }
# ──────────────────────────────────────────────

@app.put("/resenas/{resena_id}")
def editar_resena(resena_id: str, datos: dict):
    resultado = resenas.update_one(
        {"_id": ObjectId(resena_id), "estado": "publicada"},
        {"$set": {
            "calificacion":        int(datos["calificacion"]),
            "texto":               datos["texto"],
            "fecha_ultima_edicion": datetime.utcnow()
        }}
    )
    if resultado.matched_count == 0:
        raise HTTPException(status_code=404, detail="Reseña no encontrada o ya eliminada.")
    return {"mensaje": "Reseña actualizada"}


# ──────────────────────────────────────────────
# RF3 – ELIMINAR RESEÑA (cliente elimina la suya)
# Marca estado como "eliminada" en lugar de borrar físicamente
# ──────────────────────────────────────────────

@app.delete("/resenas/{resena_id}")
def eliminar_resena(resena_id: str):
    resultado = resenas.update_one(
        {"_id": ObjectId(resena_id)},
        {"$set": {"estado": "eliminada", "fecha_ultima_edicion": datetime.utcnow()}}
    )
    if resultado.matched_count == 0:
        raise HTTPException(status_code=404, detail="Reseña no encontrada.")
    return {"mensaje": "Reseña eliminada"}


# ──────────────────────────────────────────────
# RF4 – CONSULTAR RESEÑAS DE UN HOTEL (público, paginado)
# Query params: orden (fecha | utilidad), pagina, por_pagina
# ──────────────────────────────────────────────

@app.get("/hoteles/{hotel_id}/resenas")
def get_resenas_hotel(hotel_id: int, orden: str = "fecha", pagina: int = 1, por_pagina: int = 10):
    campo_orden = "fecha_creacion" if orden == "fecha" else "votos_utilidad"
    skip = (pagina - 1) * por_pagina

    # Las reseñas destacadas van primero siempre
    pipeline = [
        {"$match": {"hotel_id": hotel_id, "estado": "publicada"}},
        {"$sort": {"destacada": -1, campo_orden: -1}},
        {"$skip": skip},
        {"$limit": por_pagina}
    ]
    docs = list(resenas.aggregate(pipeline))
    return [serializar(d) for d in docs]


# ──────────────────────────────────────────────
# RF5 – MARCAR RESEÑA COMO ÚTIL
# Body: { cliente_id }
# Un usuario solo puede votar una vez por reseña
# ──────────────────────────────────────────────

@app.post("/resenas/{resena_id}/util")
def marcar_util(resena_id: str, datos: dict):
    cliente_id = int(datos["cliente_id"])
    resena = resenas.find_one({"_id": ObjectId(resena_id)})
    if not resena:
        raise HTTPException(status_code=404, detail="Reseña no encontrada.")
    if cliente_id in resena.get("votos_lista", []):
        raise HTTPException(status_code=400, detail="Ya votaste por esta reseña.")

    resenas.update_one(
        {"_id": ObjectId(resena_id)},
        {
            "$inc": {"votos_utilidad": 1},
            "$push": {"votos_lista": cliente_id}
        }
    )
    return {"mensaje": "Voto registrado"}


# ──────────────────────────────────────────────
# RF6 – HISTORIAL DE RESEÑAS PROPIAS DEL CLIENTE
# Query params: orden (fecha | hotel)
# ──────────────────────────────────────────────

@app.get("/clientes/{cliente_id}/resenas")
def get_resenas_cliente(cliente_id: int, orden: str = "fecha"):
    campo_orden = "fecha_creacion" if orden == "fecha" else "hotel_id"
    docs = list(
        resenas.find({"cliente_id": cliente_id})
                .sort(campo_orden, -1)
    )
    return [serializar(d) for d in docs]


# ──────────────────────────────────────────────
# RF7 – RESPONDER RESEÑA (administrador)
# Body: { admin_id, texto }
# ──────────────────────────────────────────────

@app.post("/resenas/{resena_id}/respuesta")
def responder_resena(resena_id: str, datos: dict):
    resena = resenas.find_one({"_id": ObjectId(resena_id)})
    if not resena:
        raise HTTPException(status_code=404, detail="Reseña no encontrada.")

    respuesta = {
        "admin_id":           int(datos["admin_id"]),
        "texto":              datos["texto"],
        "fecha_respuesta":    datetime.utcnow(),
        "fecha_ultima_edicion": datetime.utcnow()
    }
    # Si ya tenía respuesta, solo actualiza el texto y fecha de edición
    if resena.get("respuesta"):
        resenas.update_one(
            {"_id": ObjectId(resena_id)},
            {"$set": {
                "respuesta.texto":               datos["texto"],
                "respuesta.fecha_ultima_edicion": datetime.utcnow()
            }}
        )
    else:
        resenas.update_one(
            {"_id": ObjectId(resena_id)},
            {"$set": {"respuesta": respuesta}}
        )
    return {"mensaje": "Respuesta guardada"}


# ──────────────────────────────────────────────
# RF8 – ELIMINAR RESEÑA POR ADMINISTRADOR
# Igual que RF3 pero lo llama el admin (misma lógica, endpoint separado para claridad)
# ──────────────────────────────────────────────

@app.delete("/admin/resenas/{resena_id}")
def eliminar_resena_admin(resena_id: str):
    resultado = resenas.update_one(
        {"_id": ObjectId(resena_id)},
        {"$set": {"estado": "eliminada", "fecha_ultima_edicion": datetime.utcnow()}}
    )
    if resultado.matched_count == 0:
        raise HTTPException(status_code=404, detail="Reseña no encontrada.")
    return {"mensaje": "Reseña eliminada por administrador"}


# ──────────────────────────────────────────────
# RF9 – DESTACAR RESEÑA
# Body: { hotel_id }
# Solo puede haber UNA reseña destacada por hotel a la vez
# ──────────────────────────────────────────────

@app.put("/resenas/{resena_id}/destacar")
def destacar_resena(resena_id: str, datos: dict):
    hotel_id = int(datos["hotel_id"])
    # Quita el destacado de cualquier reseña actual del hotel
    resenas.update_many(
        {"hotel_id": hotel_id, "destacada": True},
        {"$set": {"destacada": False}}
    )
    # Marca la nueva como destacada
    resultado = resenas.update_one(
        {"_id": ObjectId(resena_id)},
        {"$set": {"destacada": True}}
    )
    if resultado.matched_count == 0:
        raise HTTPException(status_code=404, detail="Reseña no encontrada.")
    return {"mensaje": "Reseña destacada"}


# ──────────────────────────────────────────────
# RFC1 – TOP 10 HOTELES POR CALIFICACIÓN PROMEDIO
# Query params: fecha_inicio, fecha_fin (formato ISO: 2025-01-01)
# ──────────────────────────────────────────────

@app.get("/consultas/top-hoteles")
def top_hoteles(fecha_inicio: str = "2025-01-01", fecha_fin: str = "2025-12-31"):
    pipeline = [
        {"$match": {
            "estado": "publicada",
            "fecha_creacion": {
                "$gte": datetime.fromisoformat(fecha_inicio),
                "$lte": datetime.fromisoformat(fecha_fin)
            }
        }},
        {"$group": {
            "_id": "$hotel_id",
            "calificacion_promedio": {"$avg": "$calificacion"},
            "total_resenas": {"$sum": 1}
        }},
        {"$sort": {"calificacion_promedio": -1}},
        {"$limit": 10},
        {"$project": {
            "hotel_id": "$_id",
            "_id": 0,
            "calificacion_promedio": {"$round": ["$calificacion_promedio", 2]},
            "total_resenas": 1
        }}
    ]
    return list(resenas.aggregate(pipeline))


# ──────────────────────────────────────────────
# RFC2 – EVOLUCIÓN MENSUAL DE CALIFICACIÓN DE UN HOTEL
# Query params: anio (ej: 2025)
# ──────────────────────────────────────────────

@app.get("/hoteles/{hotel_id}/evolucion")
def evolucion_hotel(hotel_id: int, anio: int = 2025):
    pipeline = [
        {"$match": {
            "hotel_id": hotel_id,
            "estado": "publicada",
            "fecha_creacion": {
                "$gte": datetime(anio, 1, 1),
                "$lte": datetime(anio, 12, 31, 23, 59, 59)
            }
        }},
        {"$group": {
            "_id": {"$month": "$fecha_creacion"},
            "calificacion_promedio": {"$avg": "$calificacion"},
            "total_resenas": {"$sum": 1}
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "mes": "$_id",
            "_id": 0,
            "calificacion_promedio": {"$round": ["$calificacion_promedio", 2]},
            "total_resenas": 1
        }}
    ]
    return list(resenas.aggregate(pipeline))


# ──────────────────────────────────────────────
# RFC3 – PERFIL COMPARATIVO DE HOTELES POR CIUDAD
# Recibe lista de hotel_ids de la ciudad (separados por coma)
# Ej: /consultas/ciudad?hotel_ids=1,2,3
# ──────────────────────────────────────────────

@app.get("/consultas/ciudad")
def perfil_ciudad(hotel_ids: str):
    ids = [int(x) for x in hotel_ids.split(",")]
    pipeline = [
        {"$match": {
            "hotel_id": {"$in": ids},
            "estado": "publicada"
        }},
        {"$group": {
            "_id": "$hotel_id",
            "calificacion_promedio": {"$avg": "$calificacion"},
            "total_resenas": {"$sum": 1},
            "con_respuesta": {
                "$sum": {"$cond": [{"$ifNull": ["$respuesta", False]}, 1, 0]}
            },
            "destacadas": {
                "$sum": {"$cond": [{"$eq": ["$destacada", True]}, 1, 0]}
            }
        }},
        {"$project": {
            "hotel_id": "$_id",
            "_id": 0,
            "calificacion_promedio": {"$round": ["$calificacion_promedio", 1]},
            "total_resenas": 1,
            "porcentaje_respuesta": {
                "$round": [
                    {"$multiply": [{"$divide": ["$con_respuesta", "$total_resenas"]}, 100]}, 1
                ]
            },
            "porcentaje_destacadas": {
                "$round": [
                    {"$multiply": [{"$divide": ["$destacadas", "$total_resenas"]}, 100]}, 1
                ]
            }
        }},
        {"$sort": {"calificacion_promedio": -1}}
    ]
    return list(resenas.aggregate(pipeline))
